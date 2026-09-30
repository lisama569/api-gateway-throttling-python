"""统一对外能力层入口：版本协商 + 组合配额限流 + 错误归一化 + 失败诊断。

所有入口共用同一套前置判定（gateway_guard），保证同一版本、
同一调用方、同一异常在不同入口下得到一致结论。

失败诊断：每个请求在最外层中间件获得一个服务端认可的请求关联标识
（request_id），调用方传入 X-Request-Id 时沿用，否则服务端生成；
无论成功、版本不被接受、被限流、参数错误还是路径不存在，响应头
X-Request-Id 与响应体 error.request_id 都是同一个值，服务端日志也以它串联。
"""

from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import (
    BusinessRejection,
    DependencyFailure,
    InvalidRequest,
    normalize_error,
)
from app.quotas import QuotaConfig, QuotaDecision, QuotaLimiter, WindowSpec
from app.service import OrderService
from app.versioning import (
    UnsupportedVersionError,
    VERSION_HEADER,
    negotiate_version,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gateway")

CALLER_HEADER = "X-Caller-Id"
REQUEST_ID_HEADER = "X-Request-Id"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw else default


def default_quota_config() -> QuotaConfig:
    """应用级默认配额，可用环境变量覆盖，便于本地验证。

    默认口径与旧版"每调用方 5 次/60s"语义保持兼容：突发容量与持续容量
    同为 5、调用方总量上限 5，稳态下仍是 5 次/60s；突发窗口（默认 10s）
    只是额外增加一层短窗口判定，调小后即可限制短时突发。两层都通过才放行。
    """
    return QuotaConfig(
        burst=WindowSpec(
            "burst",
            _env_int("GATEWAY_BURST_LIMIT", 5),
            _env_float("GATEWAY_BURST_WINDOW_SECONDS", 10.0),
        ),
        sustained=WindowSpec(
            "sustained",
            _env_int("GATEWAY_SUSTAINED_LIMIT", 5),
            _env_float("GATEWAY_SUSTAINED_WINDOW_SECONDS", 60.0),
        ),
        caller_total=_env_int("GATEWAY_CALLER_TOTAL_LIMIT", 5),
    )


app = FastAPI(title="api-gateway-throttling")
quota_limiter = QuotaLimiter(default_quota_config())
order_service = OrderService()


@dataclass(frozen=True)
class GatewayContext:
    """一次请求通过统一前置判定后的上下文。"""

    request_id: str
    caller: str
    version: str


def resolve_request_id(request: Request) -> str:
    """获取本请求的关联标识：中间件已生成时直接取，保证全链路同一个值。"""
    rid = getattr(request.state, "request_id", None)
    if rid is None:
        # 兜底：理论上中间件一定先执行，这里仅防御极早期异常
        rid = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
        request.state.request_id = rid
    return rid


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    """最外层：确定 request_id 并保证成功响应也回传同一个值。

    错误响应的 request_id 由各异常处理器写入响应体，这里统一兜底
    响应头，因此头与体始终一致。
    """
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


class RateLimited(Exception):
    def __init__(self, body: dict, retry_after: float) -> None:
        self.body = body
        self.retry_after = retry_after


def _error_response(status_code: int, payload: dict, request_id: str) -> JSONResponse:
    resp = JSONResponse(status_code=status_code, content=payload)
    resp.headers[REQUEST_ID_HEADER] = request_id
    return resp


def gateway_guard(request: Request) -> GatewayContext:
    """统一前置判定：请求标识 -> 版本协商 -> 组合配额（突发/持续/总量）。"""
    request_id = resolve_request_id(request)
    caller = request.headers.get(CALLER_HEADER) or "anonymous"

    decision = negotiate_version(request.headers.get(VERSION_HEADER))
    logger.info(
        "request_id=%s caller=%s version=%s version_source=%s",
        request_id, caller, decision.version, decision.source,
    )

    qd: QuotaDecision = quota_limiter.check(caller, decision.version)
    logger.info(
        "request_id=%s caller=%s version=%s quota allowed=%s rejected_by=%s "
        "remaining=%s retry_after=%.3f",
        request_id, caller, decision.version, qd.allowed,
        ",".join(qd.rejected_by) or "-", qd.remaining, qd.retry_after_seconds,
    )
    if not qd.allowed:
        body = {
            "error": {
                "category": "rate_limited",
                "code": "RATE_LIMITED",
                "message": "rate limit exceeded, retry after the indicated window",
                "request_id": request_id,
                "rejected_by": list(qd.rejected_by),
                "retry_after_seconds": qd.retry_after_seconds,
            }
        }
        raise RateLimited(body, qd.retry_after_seconds)

    return GatewayContext(request_id=request_id, caller=caller, version=decision.version)


@app.exception_handler(RateLimited)
async def on_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    request_id = resolve_request_id(request)
    logger.info("request_id=%s rejected rate_limited retry_after=%.3f",
                request_id, exc.retry_after)
    resp = _error_response(429, exc.body, request_id)
    resp.headers["Retry-After"] = str(exc.retry_after)
    return resp


@app.exception_handler(UnsupportedVersionError)
async def on_unsupported_version(request: Request, exc: UnsupportedVersionError) -> JSONResponse:
    request_id = resolve_request_id(request)
    logger.info(
        "request_id=%s version_rejected requested=%s supported=%s",
        request_id, exc.requested, ",".join(exc.supported),
    )
    return _error_response(
        400,
        {
            "error": {
                "category": "unsupported_version",
                "code": "UNSUPPORTED_VERSION",
                "message": f"api version '{exc.requested}' is not supported",
                "supported_versions": list(exc.supported),
                "request_id": request_id,
            }
        },
        request_id,
    )


@app.exception_handler(InvalidRequest)
async def on_invalid_request(request: Request, exc: InvalidRequest) -> JSONResponse:
    request_id = resolve_request_id(request)
    body = normalize_error(exc, request_id)
    logger.info("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(422, body.to_dict(), request_id)


@app.exception_handler(BusinessRejection)
async def on_business_rejection(request: Request, exc: BusinessRejection) -> JSONResponse:
    request_id = resolve_request_id(request)
    body = normalize_error(exc, request_id)
    logger.info("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(422, body.to_dict(), request_id)


@app.exception_handler(DependencyFailure)
async def on_dependency_failure(request: Request, exc: DependencyFailure) -> JSONResponse:
    request_id = resolve_request_id(request)
    body = normalize_error(exc, request_id)
    logger.warning("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(502, body.to_dict(), request_id)


@app.exception_handler(RequestValidationError)
async def on_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    """调用方输入格式问题（JSON 非法、字段类型不对、缺字段）明确归类为调用方侧错误。"""
    request_id = resolve_request_id(request)
    details = [
        {"field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
         "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    logger.info("request_id=%s error category=invalid_request details=%s",
                request_id, details)
    return _error_response(
        422,
        {
            "error": {
                "category": "invalid_request",
                "code": "INVALID_REQUEST",
                "message": "request payload is invalid",
                "request_id": request_id,
                "details": details,
            }
        },
        request_id,
    )


@app.exception_handler(StarletteHTTPException)
async def on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """访问不存在的位置等 HTTP 层错误：归一到统一结构并带同一 request_id。"""
    request_id = resolve_request_id(request)
    if exc.status_code == 404:
        category, code, message = "not_found", "NOT_FOUND", "requested resource not found"
    else:
        category, code, message = "http_error", f"HTTP_{exc.status_code}", str(exc.detail)
    logger.info("request_id=%s error category=%s status=%s path=%s",
                request_id, category, exc.status_code, request.url.path)
    return _error_response(
        exc.status_code,
        {
            "error": {
                "category": category,
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
        request_id,
    )


@app.exception_handler(Exception)
async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
    request_id = resolve_request_id(request)
    body = normalize_error(exc, request_id)
    # 内部细节只进日志，不透出给调用方
    logger.exception("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(500, body.to_dict(), request_id)


def _parse_quantity(raw) -> int:
    # 缺省/显式 null 沿用旧行为：按 0 交给业务规则判定（quantity must be positive）
    if raw is None:
        return 0
    # 类型不对（字符串、浮点、布尔等）明确是调用方输入问题
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise InvalidRequest("quantity must be an integer")
    return raw


@app.post("/orders")
def place_order(payload: dict, ctx=Depends(gateway_guard)):
    quantity = _parse_quantity(payload.get("quantity", 0))
    result = order_service.place_order(payload.get("item", ""), quantity)
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}


@app.get("/orders/preview")
def preview_order(item: str = "book", quantity: int = 1, ctx=Depends(gateway_guard)):
    """第二个入口：与 /orders 共用同一套版本/配额/错误判定。"""
    result = order_service.place_order(item, quantity)
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}
