"""统一网关中间件。

处理顺序（对所有入口一致）：
1. 解析/生成请求标识，写入 request.state 与响应头；
2. 解析调用方身份（未提供时使用固定匿名身份，保证也被计入限流）；
3. 版本协商，不支持的版本在此被明确拒绝；
4. 按调用方限流，超限返回 429 与可操作的重试信息；
5. 业务执行；业务拒绝 / 依赖失败 / 未预期异常在此归一化。

每次关键判定都会打印请求标识与判定依据，便于在日志中追溯。
"""

import logging
import math
import uuid

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

from gateway.config import GatewayConfig
from gateway.errors import (
    DependencyFailureError,
    ErrorCategory,
    GatewayError,
    InternalError,
    RateLimitedError,
    VersionUnsupportedError,
    build_error_body,
)
from gateway.ratelimit import RateLimiter
from gateway.versioning import VersionNegotiator

logger = logging.getLogger("gateway")

DEFAULT_CALLER = "anonymous"


class GatewayMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app,
        config: GatewayConfig,
        negotiator: VersionNegotiator,
        limiter: RateLimiter,
    ) -> None:
        super().__init__(app)
        self._config = config
        self._negotiator = negotiator
        self._limiter = limiter

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # 1) 请求标识：优先使用调用方约定位置提供的值，否则服务端生成。
        request_id = request.headers.get(self._config.request_id_header) or uuid.uuid4().hex
        request.state.request_id = request_id

        logger.info(
            "gateway decision request_id=%s reason=request_started method=%s path=%s",
            request_id,
            request.method,
            request.url.path,
        )

        # 2) 调用方身份。
        caller = request.headers.get(self._config.caller_header) or DEFAULT_CALLER
        request.state.caller = caller

        # 3) 版本协商（判定逻辑集中于 VersionNegotiator.decide）。
        try:
            version = self._negotiator.negotiate(request)
        except VersionUnsupportedError as exc:
            exc.details["requested_at"] = self._config.version_header
            logger.info(
                "gateway decision request_id=%s reason=version_denied caller=%s detail=%s",
                request_id,
                caller,
                exc.internal_detail,
            )
            return self._error_response(request_id, exc)
        request.state.api_version = version
        logger.info(
            "gateway decision request_id=%s reason=version_accepted caller=%s version=%s",
            request_id,
            caller,
            version,
        )

        # 4) 按调用方限流。
        result = await self._limiter.acquire(caller)
        if not result.allowed:
            exc = RateLimitedError(
                details={
                    "retry_after": round(result.retry_after, 3),
                    "reset_in": round(result.reset_in, 3),
                    "limit": result.limit,
                },
                internal_detail=(
                    f"caller={caller} exceeded limit={result.limit} "
                    f"retry_after={result.retry_after:.3f}s"
                ),
            )
            logger.info(
                "gateway decision request_id=%s reason=rate_limited caller=%s limit=%s retry_after=%.3f",
                request_id,
                caller,
                result.limit,
                result.retry_after,
            )
            return self._error_response(request_id, exc)

        logger.info(
            "gateway decision request_id=%s reason=rate_allowed caller=%s remaining=%s",
            request_id,
            caller,
            result.remaining,
        )

        # 5) 业务执行与异常归一化。
        try:
            response = await call_next(request)
        except GatewayError as exc:
            logger.info(
                "gateway decision request_id=%s reason=gateway_error caller=%s category=%s detail=%s",
                request_id,
                caller,
                exc.category.value,
                exc.internal_detail or exc.message,
            )
            return self._error_response(request_id, exc)
        except Exception as exc:  # noqa: BLE001 - 边界处兜底，防止内部细节外泄
            logger.exception(
                "gateway decision request_id=%s reason=unexpected_exception caller=%s error_type=%s",
                request_id,
                caller,
                type(exc).__name__,
            )
            return self._error_response(
                request_id,
                InternalError(internal_detail=f"{type(exc).__name__}: {exc}"),
            )

        # 正常响应也要带上可解释头。
        response.headers[self._config.request_id_header] = request_id
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining)
        response.headers["X-RateLimit-Reset"] = str(math.ceil(result.reset_in))
        return response

    def _error_response(self, request_id: str, exc: GatewayError) -> JSONResponse:
        headers = {self._config.request_id_header: request_id}
        if exc.category is ErrorCategory.RATE_LIMITED:
            retry_after = float(exc.details.get("retry_after", 0.0))
            reset_in = float(exc.details.get("reset_in", retry_after))
            # 可操作的重试信息：标准头 + 结构化字段。
            headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
            headers["X-RateLimit-Limit"] = str(exc.details.get("limit", ""))
            headers["X-RateLimit-Remaining"] = "0"
            headers["X-RateLimit-Reset"] = str(max(1, math.ceil(reset_in)))

        body = build_error_body(
            request_id=request_id,
            category=exc.category,
            message=exc.message,
            details=exc.details or None,
        )
        return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


def register_exception_handlers(app, config: GatewayConfig) -> None:
    """把框架自身产生的错误（路由不存在、请求体校验失败等）归一化到同一结构。"""

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        if 500 <= exc.status_code < 600:
            category = ErrorCategory.INTERNAL_ERROR
            message = "internal server error"
        else:
            category = ErrorCategory.BAD_REQUEST
            message = str(exc.detail)
        logger.info(
            "gateway decision request_id=%s reason=http_exception status=%s category=%s",
            request_id,
            exc.status_code,
            category.value,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=build_error_body(
                request_id=request_id, category=category, message=message
            ),
            headers={config.request_id_header: request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None) or uuid.uuid4().hex
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(p) for p in first.get("loc", [])) or "request"
        message = f"invalid request: {location}"
        logger.info(
            "gateway decision request_id=%s reason=validation_failed location=%s",
            request_id,
            location,
        )
        return JSONResponse(
            status_code=422,
            content=build_error_body(
                request_id=request_id,
                category=ErrorCategory.BAD_REQUEST,
                message=message,
            ),
            headers={config.request_id_header: request_id},
        )
