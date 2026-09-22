"""多入口路由。

两个前缀（``/api``、``/internal``）代表不同入口；
它们不自带任何版本/限流/错误处理逻辑，全部由统一中间件裁定，
因此同一原因在不同入口下得到相同结论。
"""

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from gateway.dependencies import DependencyUnavailable, RemoteClient
from gateway.errors import BusinessRejectionError, DependencyFailureError


class OrderRequest(BaseModel):
    item: str = Field(min_length=1)
    qty: int = Field(ge=1)


def _client(request: Request) -> RemoteClient:
    return request.app.state.remote_client


def build_api_router() -> APIRouter:
    """对外入口 /api。"""

    router = APIRouter(prefix="/api")

    @router.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "request_id": request.state.request_id,
            "caller": request.state.caller,
            "version": request.state.api_version,
            "entry": "api",
        }

    @router.get("/remotes/{name}")
    async def call_remote(request: Request, name: str) -> dict:
        try:
            data = await _client(request).fetch(name)
        except DependencyUnavailable as exc:
            # 归一化：对外固定文案；原始异常（可能含敏感信息）只进日志。
            raise DependencyFailureError(
                details={"dependency": "remote", "name": name},
                internal_detail=str(exc),
            ) from exc
        return {"request_id": request.state.request_id, "data": data}

    @router.post("/orders")
    async def create_order(request: Request, payload: OrderRequest) -> dict:
        # 示例业务规则：frozen 商品当前不可下单。
        if payload.item == "frozen":
            raise BusinessRejectionError(
                message="item is not orderable",
                details={"reason": "item_not_available", "item": payload.item},
            )
        return {
            "request_id": request.state.request_id,
            "accepted": True,
            "item": payload.item,
            "qty": payload.qty,
        }

    return router


def build_internal_router() -> APIRouter:
    """内部入口 /internal，策略与 /api 完全一致。"""

    router = APIRouter(prefix="/internal")

    @router.get("/echo")
    async def echo(request: Request) -> dict:
        return {
            "request_id": request.state.request_id,
            "caller": request.state.caller,
            "version": request.state.api_version,
            "entry": "internal",
        }

    @router.get("/remotes/{name}")
    async def call_remote(request: Request, name: str) -> dict:
        try:
            data = await _client(request).fetch(name)
        except DependencyUnavailable as exc:
            raise DependencyFailureError(
                details={"dependency": "remote", "name": name},
                internal_detail=str(exc),
            ) from exc
        return {"request_id": request.state.request_id, "data": data}

    @router.get("/boom")
    async def boom(request: Request) -> dict:
        # 未预期异常：由中间件兜底为 INTERNAL_ERROR，细节不透出。
        raise RuntimeError("unexpected explosion with secret=s3cr3t")

    return router


def build_routers() -> tuple[APIRouter, ...]:
    return build_api_router(), build_internal_router()
