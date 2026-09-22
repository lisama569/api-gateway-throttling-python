"""组装 FastAPI 应用：配置、限流器、版本协商、统一中间件、多入口路由。"""

from fastapi import FastAPI

from gateway.config import GatewayConfig, default_config
from gateway.dependencies import HttpRemoteClient
from gateway.middleware import GatewayMiddleware, register_exception_handlers
from gateway.ratelimit import RateLimiter
from gateway.routes import build_routers
from gateway.versioning import VersionNegotiator


def create_app(
    config: GatewayConfig | None = None,
    *,
    remote_client: HttpRemoteClient | None = None,
) -> FastAPI:
    config = config or default_config()

    app = FastAPI(title="api-gateway-throttling", version="0.1.0")
    app.state.gateway_config = config
    app.state.limiter = RateLimiter(config.rate_limit, config.window_seconds)
    app.state.negotiator = VersionNegotiator(config)
    app.state.remote_client = remote_client or HttpRemoteClient(
        fail_names=frozenset({"broken"})
    )

    app.add_middleware(
        GatewayMiddleware,
        config=config,
        negotiator=app.state.negotiator,
        limiter=app.state.limiter,
    )
    register_exception_handlers(app, config)

    for router in build_routers():
        app.include_router(router)

    @app.get("/health", include_in_schema=False)
    async def health() -> dict:
        return {"status": "ok"}

    return app
