"""统一错误分类与归一化结构。

设计原则：
- 错误分类（category）稳定、可区分，调用方可以据此编程处理；
- 对外消息固定且不包含内部实现细节与敏感字段；
- 内部诊断信息仅写入服务端日志，不进入响应体。
"""

from enum import Enum
from typing import Any


class ErrorCategory(str, Enum):
    """错误分类（取值即对外契约，不得随意更名）。"""

    BAD_REQUEST = "bad_request"
    VERSION_UNSUPPORTED = "version_unsupported"
    RATE_LIMITED = "rate_limited"
    BUSINESS_REJECTED = "business_rejected"
    DEPENDENCY_FAILURE = "dependency_failure"
    INTERNAL_ERROR = "internal_error"


class GatewayError(Exception):
    """所有归一化网关错误的基类。"""

    status_code: int = 500
    category: ErrorCategory = ErrorCategory.INTERNAL_ERROR
    public_message: str = "internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        details: dict[str, Any] | None = None,
        internal_detail: str | None = None,
    ) -> None:
        super().__init__(message or self.public_message)
        self.message = message or self.public_message
        self.details: dict[str, Any] = details or {}
        # 仅供服务端日志使用，禁止透出给调用方。
        self.internal_detail = internal_detail


class VersionUnsupportedError(GatewayError):
    status_code = 400
    category = ErrorCategory.VERSION_UNSUPPORTED
    public_message = "unsupported api version"


class RateLimitedError(GatewayError):
    status_code = 429
    category = ErrorCategory.RATE_LIMITED
    public_message = "rate limit exceeded"


class BusinessRejectionError(GatewayError):
    """业务规则拒绝，默认 422。"""

    status_code = 422
    category = ErrorCategory.BUSINESS_REJECTED
    public_message = "business rule rejected"


class DependencyFailureError(GatewayError):
    """下游依赖失败，对外只暴露固定文案。"""

    status_code = 503
    category = ErrorCategory.DEPENDENCY_FAILURE
    public_message = "dependency unavailable"


class InternalError(GatewayError):
    status_code = 500
    category = ErrorCategory.INTERNAL_ERROR
    public_message = "internal server error"


def build_error_body(
    *,
    request_id: str,
    category: ErrorCategory,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造稳定结构的错误响应体：

    ``{"error": {"category", "message", "request_id", "details"?}}``
    """

    error: dict[str, Any] = {
        "category": category.value,
        "message": message,
        "request_id": request_id,
    }
    if details:
        error["details"] = details
    return {"error": error}
