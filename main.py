"""统一对外能力层入口：版本协商 + 限流 + 错误归一化。

所有入口共用同一套前置判定（gateway_guard），保证同一版本、
同一调用方、同一异常在不同入口下得到一致结论。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.errors import (
    BusinessRejection,
    DependencyFailure,
    ErrorBody,
    normalize_error,
)
from app.ratelimit import RateLimiter
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

app = FastAPI(title="api-gateway-throttling")
rate_limiter = RateLimiter(limit=5, window_seconds=60.0)
order_service = OrderService()


@dataclass(frozen=True)
class GatewayContext:
    """一次请求通过统一前置判定后的上下文。"""

    request_id: str
    caller: str
    version: str


def gateway_guard(request: Request) -> GatewayContext:
    """统一前置判定：请求标识 -> 版本协商 -> 限流。"""
    request_id = request.headers.get(REQUEST_ID_HEADER) or uuid.uuid4().hex[:12]
    caller = request.headers.get(CALLER_HEADER) or "anonymous"

    decision = negotiate_version(request.headers.get(VERSION_HEADER))
    logger.info(
        "request_id=%s caller=%s version=%s version_source=%s",
        request_id, caller, decision.version, decision.source,
    )

    rl = rate_limiter.check(caller)
    logger.info(
        "request_id=%s caller=%s rate_limit allowed=%s remaining=%s/%s retry_after=%.3f",
        request_id, caller, rl.allowed, rl.remaining, rl.limit, rl.retry_after_seconds,
    )
    if not rl.allowed:
        body = {
            "error": {
                "category": "rate_limited",
                "code": "RATE_LIMITED",
                "message": "rate limit exceeded, retry after the indicated window",
                "request_id": request_id,
                "retry_after_seconds": rl.retry_after_seconds,
            }
        }
        raise RateLimited(body, rl.retry_after_seconds)

    return GatewayContext(request_id=request_id, caller=caller, version=decision.version)


class RateLimited(Exception):
    def __init__(self, body: dict, retry_after: float) -> None:
        self.body = body
        self.retry_after = retry_after


def _error_response(status_code: int, body: ErrorBody | dict) -> JSONResponse:
    payload = body.to_dict() if isinstance(body, ErrorBody) else body
    return JSONResponse(status_code=status_code, content=payload)


@app.exception_handler(RateLimited)
async def on_rate_limited(request: Request, exc: RateLimited) -> JSONResponse:
    resp = _error_response(429, exc.body)
    resp.headers["Retry-After"] = str(exc.retry_after)
    return resp


@app.exception_handler(UnsupportedVersionError)
async def on_unsupported_version(request: Request, exc: UnsupportedVersionError) -> JSONResponse:
    request_id = request.headers.get(REQUEST_ID_HEADER) or "-"
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
    )


@app.exception_handler(BusinessRejection)
async def on_business_rejection(request: Request, exc: BusinessRejection) -> JSONResponse:
    request_id = request.headers.get(REQUEST_ID_HEADER) or "-"
    body = normalize_error(exc, request_id)
    logger.info("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(422, body)


@app.exception_handler(DependencyFailure)
async def on_dependency_failure(request: Request, exc: DependencyFailure) -> JSONResponse:
    request_id = request.headers.get(REQUEST_ID_HEADER) or "-"
    body = normalize_error(exc, request_id)
    logger.warning("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(502, body)


@app.exception_handler(Exception)
async def on_unexpected(request: Request, exc: Exception) -> JSONResponse:
    request_id = request.headers.get(REQUEST_ID_HEADER) or "-"
    body = normalize_error(exc, request_id)
    # 内部细节只进日志，不透出给调用方
    logger.exception("request_id=%s error category=%s code=%s", request_id, body.category, body.code)
    return _error_response(500, body)


@app.post("/orders")
def place_order(payload: dict, ctx: GatewayContext = Depends(gateway_guard)):
    result = order_service.place_order(payload.get("item", ""), int(payload.get("quantity", 0)))
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}


@app.get("/orders/preview")
def preview_order(item: str = "book", quantity: int = 1, ctx: GatewayContext = Depends(gateway_guard)):
    """第二个入口：与 /orders 共用同一套版本/限流/错误判定。"""
    result = order_service.place_order(item, quantity)
    return {"request_id": ctx.request_id, "version": ctx.version, "order": result}
