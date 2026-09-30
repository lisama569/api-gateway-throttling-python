"""统一对外能力层入口：版本协商 + 组合配额限流 + 错误归一化。

所有入口共用同一套前置判定（gateway_guard），保证同一版本、
同一调用方、同一异常在不同入口下得到一致结论。

配额按 (调用方, 版本) 组合隔离（突发 + 持续两层窗口），同一调用方
另有跨版本总量上限；request_id 由中间件统一分配，响应头/响应体/日志
三处同值，贯穿成功与全部失败路径。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.errors import (
    BusinessRejection,
    DependencyFailure,
    ErrorBody,
    normalize_error,
)
from app.quota import QuotaDecision, QuotaManager
from app.quota_config import build_default_quota_manager
from app.request_context import (
    REQUEST_ID_HEADER,
    assign_request_id,
    get_request_id,
)
from app.service import OrderService
from app.versioning import (
    UnsupportedVersionError,
    VERSION_HEADER,
    negotiate_version,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gateway")

CALLER_HEADER = "X-Caller-Id"

app = FastAPI(title="api-gateway-throttling")
quota_manager: QuotaManager = build_default_quota_manager()
order_service = OrderService()


@dataclass(frozen=True)
class GatewayContext:
    """一次请求通过统一前置判定后的上下文。"""

    request_id: str
    caller: str
    version: str


class RequestContextMiddleware:
    """纯 ASGI 中间件：为每次请求确定 request_id。

    通过包装 send 在响应起始消息上写入 X-Request-Id，因此即使下游抛异常
    （500 等），响应头、响应体与日志里的 request_id 仍然是同一个值。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = assign_request_id(request)
        logger.info(
            "request_id=%s event=request_start method=%s path=%s",
            request_id, scope.get("method"), scope.get("path"),
        )

        header_name = REQUEST_ID_HEADER.encode("latin-1")
        header_value = request_id.encode("latin-1")

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v) for (k, v) in message.get("headers", [])
                    if k.lower() != header_name.lower()
                ]
                headers.append((header_name, header_value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(RequestContextMiddleware)


def gateway_guard(request: Request) -> GatewayContext:
    """统一前置判定：版本协商 -> 组合配额（版本不被接受时不计配额）。"""
    request_id = get_request_id(request)
    caller = request.headers.get(CALLER_HEADER) or "anonymous"

    decision = negotiate_version(request.headers.get(VERSION_HEADER))
    logger.info(
        "request_id=%s caller=%s version=%s version_source=%s",
        request_id, caller, decision.version, decision.source,
    )

    qd = quota_manager.check(caller, decision.version)
    logger.info(
        "request_id=%s caller=%s version=%s quota allowed=%s reason=%s "
        "burst=%s/%s sustained=%s/%s total=%s/%s retry_after=%.3f",
        request_id, caller, decision.version, qd.allowed, qd.reason,
        _rem(qd, "burst"), _cap(qd, "burst"),
        _rem(qd, "sustained"), _cap(qd, "sustained"),
        _rem(qd, "caller_total"), _cap(qd, "caller_total"),
        qd.retry_after_seconds,
    )
    if not qd.allowed:
        body = {
            "error": {
                "category": "rate_limited",
                "code": "RATE_LIMITED",
                "message": "quota exceeded, retry after the indicated window",
                "request_id": request_id,
                "reason": qd.reason,
                "retry_after_seconds": qd.retry_after_seconds,
            }
        }
        raise RateLimited(body, qd.retry_after_seconds)

    return GatewayContext(request_id=request_id, caller=caller, version=decision.version)


def _rem(qd: QuotaDecision, name: str) -> str:
    dim = qd.dimensions.get(name)
    return str(dim.remaining) if dim else "-"


def _cap(qd: QuotaDecision, name: str) -> str:
    dim = qd.dimensions.get(name)
    return str(dim.capacity) if dim else "-"


class RateLimited(Exception):
    def __init__(self, body: dict, retry_after: float) -> None:
        self.body = body
        self.retry_after = retry_after


def _error_response(status_code: int, body: ErrorBody | dict) -> JSONResponse:
    payload = body.to_dict() if isinstance(body, ErrorBody) else body
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(RateLimited)
async def on_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    request_id = get_request_id(request)
    logger.info("request_id=%s event=rate_limited retry_after=%.3f",
                request_id, exc.retry_after)
    resp = _error_response(429, exc.body)
    resp.headers["Retry-After"] = str(exc.retry_after)
    return resp


@app.exception_handler(UnsupportedVersionError)
async def on_unsupported_version(request: Request, exc: UnsupportedVersionError) -> JSONResponse:
    request_id = get_request_id(request)
    logger.info(
        "request_id=%s event=version_rejected requested=%s supported=%s",
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
    )


@app.exception_handler(RequestValidationError)
async def on_request_validation(request: Request, exc: RequestValidationError) -> JSONResponse:
    """调用方输入问题（参数格式/类型不对、请求体非法）：明确按 4xx 客户端错误处理，
    不进入服务端内部异常归类，也不透出内部上下文。"""
    request_id = get_request_id(request)
    details = [
        {"location": list(err.get("loc", [])), "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    logger.info("request_id=%s event=client_error detail_count=%d",
                request_id, len(details))
    return _error_response(
        400,
        {
            "error": {
                "category": "client_error",
                "code": "INVALID_REQUEST",
                "message": "request parameters are invalid",
                "details": details,
                "request_id": request_id,
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def on_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把框架级 HTTP 异常（404/405 等）归一化，同样携带 request_id。"""
    request_id = get_request_id(request)
    if exc.status_code == 404:
        code, message = "NOT_FOUND", "requested location does not exist"
        level = logging.INFO
    elif exc.status_code == 405:
        code, message = "METHOD_NOT_ALLOWED", "http method not allowed for this location"
        level = logging.INFO
    else:
        code, message = "HTTP_ERROR", "request could not be processed"
        level = logging.WARNING
    logger.log(level, "request_id=%s event=http_error status=%s",
               request_id, exc.status_code)
    return _error_response(
        exc.status_code,
        {
            "error": {
                "category": "client_error",
                "code": code,
                "message": message,
                "request_id": request_id,
            }
        },
    )


@app.exception_handler(BusinessRejection)
async def on_business_rejection(request: Request, exc: BusinessRejection) -> JSONResponse:
    request_id = get_request_id(request)
    body = normalize_error(exc, request_id)
    logger.info("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(422, body)


@app.exception_handler(DependencyFailure)
async def on_dependency_failure(request: Request, exc: DependencyFailure) -> JSONResponse:
    request_id = get_request_id(request)
    body = normalize_error(exc, request_id)
    logger.warning("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(502, body)


@app.exception_handler(Exception)
async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
    request_id = get_request_id(request)
    body = normalize_error(exc, request_id)
    # 内部细节只进日志，不透出给调用方
    logger.exception("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    resp = _error_response(500, body)
    # 500 由 ServerErrorMiddleware 走独立响应路径，外层中间件的响应头
    # 不会生效，这里补写同一个 request_id，保证头/体一致
    resp.headers[REQUEST_ID_HEADER] = request_id
    return resp


@app.post("/orders")
def place_order(payload: dict, ctx: GatewayContext = Depends(gateway_guard)):
    result = order_service.place_order(payload.get("item", ""), int(payload.get("quantity", 0)))
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}


@app.get("/orders/preview")
def preview_order(item: str = "book", quantity: int = 1, ctx: GatewayContext = Depends(gateway_guard)):
    """第二个入口：与 /orders 共用同一套版本/配额/错误判定。

    同一 (caller, version) 在两个入口下消费同一份组合额度。
    """
    result = order_service.place_order(item, quantity)
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}
