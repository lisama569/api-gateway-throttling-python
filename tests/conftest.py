"""测试夹具：不同口径的应用、异步 HTTP 客户端、实时网关日志。"""

import logging

import httpx
import pytest
import pytest_asyncio

from gateway.app import create_app
from gateway.config import GatewayConfig


def make_config(*, rate_limit: int = 10, window_seconds: float = 60.0) -> GatewayConfig:
    return GatewayConfig(
        supported_versions=("v1", "v2"),
        default_version="v1",
        rate_limit=rate_limit,
        window_seconds=window_seconds,
        version_header="x-api-version",
        caller_header="x-caller-id",
        request_id_header="x-request-id",
    )


@pytest.fixture()
def app():
    return create_app(make_config())


@pytest.fixture()
def tight_app():
    """小额度应用：每调用方每窗口仅 3 次，便于精确断言。"""

    return create_app(make_config(rate_limit=3, window_seconds=60.0))


@pytest_asyncio.fixture()
async def client(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest_asyncio.fixture()
async def tight_client(tight_app):
    transport = httpx.ASGITransport(app=tight_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as ac:
        yield ac


@pytest.fixture()
def gateway_logs(caplog):
    """捕获 gateway 日志（请求标识 + 判定依据）。

    通过 pyproject.toml 的 log_cli 配置，这些记录在单测运行时会被实时打印；
    同时记录进 caplog 供断言使用。
    """

    caplog.set_level(logging.INFO)
    gateway_logger = logging.getLogger("gateway")
    gateway_logger.setLevel(logging.INFO)
    yield caplog


pytest_plugins: tuple[str, ...] = ("pytest_asyncio",)
