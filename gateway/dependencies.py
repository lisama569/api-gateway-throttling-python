"""下游依赖模拟。

真实系统中这里是数据库 / RPC / 外部 HTTP 客户端。
网关只关心：依赖以异常方式失败时，必须被归一化为 DEPENDENCY_FAILURE，
且内部细节（连接串、密钥、堆栈）不得透出。
"""

import os
from typing import Protocol


class DependencyUnavailable(Exception):
    """下游不可用（连接失败 / 超时等）。异常文本仅出现在服务端日志中。"""


class RemoteClient(Protocol):
    async def fetch(self, name: str) -> dict: ...


class HttpRemoteClient:
    """一个可被配置为失败的演示客户端。"""

    def __init__(self, *, fail_names: frozenset[str] = frozenset()) -> None:
        self._fail_names = fail_names
        # 故意携带敏感值，验证它不会出现在对外响应中。
        self._secret = os.environ.get("GATEWAY_DEMO_SECRET", "s3cr3t-token")

    async def fetch(self, name: str) -> dict:
        if name in self._fail_names:
            raise DependencyUnavailable(
                f"connection refused to remote={name} using token={self._secret}"
            )
        return {"remote": name, "payload": f"hello from {name}"}
