"""示例业务服务：演示业务拒绝、依赖失败与未预期异常。"""

from __future__ import annotations

from .errors import BusinessRejection, DependencyFailure


class OrderService:
    """下单服务，item 用于在本地验证时触发不同失败路径。"""

    def __init__(self, inventory: dict[str, int] | None = None) -> None:
        self._inventory = inventory if inventory is not None else {"book": 100}

    def place_order(self, item: str, quantity: int) -> dict:
        if quantity <= 0:
            raise BusinessRejection("quantity must be positive")
        if item == "fragile":
            # 模拟下游库存服务故障
            raise DependencyFailure("inventory service unreachable")
        if item == "boom":
            # 模拟未预期异常
            raise RuntimeError("unexpected null pointer in pricing engine")
        if item not in self._inventory:
            raise BusinessRejection(f"unknown item: {item}")
        if quantity > self._inventory[item]:
            raise BusinessRejection("insufficient stock")
        self._inventory[item] -= quantity
        return {"item": item, "quantity": quantity, "status": "confirmed"}
