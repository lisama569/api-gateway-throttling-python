"""网关配置：版本协商与限流口径。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class GatewayConfig:
    """统一对外能力的静态配置。"""

    supported_versions: tuple[str, ...]
    default_version: str
    rate_limit: int
    window_seconds: float
    version_header: str
    caller_header: str
    request_id_header: str


def default_config() -> GatewayConfig:
    """默认口径：

    - 支持 v1/v2，缺省版本 v1；
    - 每个调用方每 60 秒 10 次请求的固定窗口额度；
    - 约定位置：X-API-Version / X-Caller-Id / X-Request-Id。
    """

    return GatewayConfig(
        supported_versions=("v1", "v2"),
        default_version="v1",
        rate_limit=10,
        window_seconds=60.0,
        version_header="x-api-version",
        caller_header="x-caller-id",
        request_id_header="x-request-id",
    )
