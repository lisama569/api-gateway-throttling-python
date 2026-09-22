"""错误归一化：业务拒绝 / 依赖失败 / 未预期异常 -> 稳定错误结构。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ErrorCategory(str, Enum):
    BUSINESS_REJECTION = "business_rejection"
    DEPENDENCY_FAILURE = "dependency_failure"
    INTERNAL_ERROR = "internal_error"


class BusinessRejection(Exception):
    """业务规则拒绝（如参数不合法、状态不允许）。"""


class DependencyFailure(Exception):
    """下游依赖调用失败。"""


@dataclass(frozen=True)
class ErrorBody:
    """对外输出的稳定错误结构，不含内部细节与敏感字段。"""

    category: ErrorCategory
    code: str
    message: str
    request_id: str

    def to_dict(self) -> dict:
        return {
            "error": {
                "category": self.category.value,
                "code": self.code,
                "message": self.message,
                "request_id": self.request_id,
            }
        }


def normalize_error(exc: Exception, request_id: str) -> ErrorBody:
    """把任意异常归类为稳定的对外错误结构。

    内部异常细节（堆栈、敏感字段）不透出：未预期异常只返回通用文案，
    真实原因由调用方日志携带 request_id 记录。
    """
    if isinstance(exc, BusinessRejection):
        return ErrorBody(
            category=ErrorCategory.BUSINESS_REJECTION,
            code="BUSINESS_REJECTION",
            message=str(exc) or "request rejected by business rule",
            request_id=request_id,
        )
    if isinstance(exc, DependencyFailure):
        return ErrorBody(
            category=ErrorCategory.DEPENDENCY_FAILURE,
            code="DEPENDENCY_FAILURE",
            message="upstream dependency failed, please retry later",
            request_id=request_id,
        )
    return ErrorBody(
        category=ErrorCategory.INTERNAL_ERROR,
        code="INTERNAL_ERROR",
        message="unexpected internal error",
        request_id=request_id,
    )
