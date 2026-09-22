"""统一对外能力：版本协商、按调用方限流、错误归一化。"""

from gateway.app import create_app
from gateway.config import GatewayConfig, default_config
from gateway.errors import (
    BusinessRejectionError,
    DependencyFailureError,
    ErrorCategory,
    GatewayError,
    InternalError,
    RateLimitedError,
    VersionUnsupportedError,
)
from gateway.ratelimit import RateLimiter, RateLimitResult
from gateway.versioning import VersionNegotiator

__all__ = [
    "create_app",
    "GatewayConfig",
    "default_config",
    "ErrorCategory",
    "GatewayError",
    "VersionUnsupportedError",
    "RateLimitedError",
    "BusinessRejectionError",
    "DependencyFailureError",
    "InternalError",
    "RateLimiter",
    "RateLimitResult",
    "VersionNegotiator",
]
