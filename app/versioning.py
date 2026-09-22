"""接口版本协商：解析、校验与默认版本。"""

from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_VERSIONS: tuple[str, ...] = ("v1", "v2")
DEFAULT_VERSION: str = "v1"
VERSION_HEADER: str = "X-Api-Version"


class UnsupportedVersionError(Exception):
    """请求的版本不在支持集合内。"""

    def __init__(self, requested: str, supported: tuple[str, ...]) -> None:
        self.requested = requested
        self.supported = supported
        super().__init__(requested)


@dataclass(frozen=True)
class VersionDecision:
    """版本协商结果。"""

    version: str
    source: str  # "header" | "default"


def negotiate_version(raw: str | None) -> VersionDecision:
    """按约定位置（请求头）解析期望版本；未指定时用默认版本。

    不受支持的版本抛出 UnsupportedVersionError，所有入口共用本函数，
    保证同一版本在不同入口下判定一致。
    """
    if raw is None or not raw.strip():
        return VersionDecision(version=DEFAULT_VERSION, source="default")
    version = raw.strip().lower()
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedVersionError(version, SUPPORTED_VERSIONS)
    return VersionDecision(version=version, source="header")
