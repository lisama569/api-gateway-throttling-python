"""版本协商：调用方按约定位置（请求头）表达期望版本。

- 未携带约定头：使用默认版本；
- 携带但为空或不受支持：明确拒绝；
- 判定集中在 ``decide`` 一处，不同入口复用同一判定，结论天然一致。
"""

from fastapi import Request

from gateway.config import GatewayConfig
from gateway.errors import VersionUnsupportedError


class VersionNegotiator:
    def __init__(self, config: GatewayConfig) -> None:
        self._config = config

    def decide(self, requested: str | None) -> str:
        """与传输层无关的纯判定：给定头值，返回生效版本或抛出拒绝异常。

        所有入口（HTTP 中间件、未来的其他入口）都必须走这里，
        以保证同一版本表达在任何入口下结论一致。
        """

        if requested is None:
            return self._config.default_version

        value = requested.strip()
        normalized = value.lower()
        if value and normalized in self._config.supported_versions:
            return normalized

        # 空值与不在支持列表内的值同等对待：明确拒绝。
        raise VersionUnsupportedError(
            details={
                "requested_version": value,
                "supported_versions": list(self._config.supported_versions),
                "default_version": self._config.default_version,
            },
            internal_detail=f"version negotiation rejected: {requested!r}",
        )

    def negotiate(self, request: Request) -> str:
        """从 HTTP 请求约定位置读取版本并完成协商。"""

        raw = request.headers.get(self._config.version_header)
        return self.decide(raw)
