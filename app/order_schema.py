"""下单请求体模型：对 JSON 请求体做显式类型校验。

quantity 使用 int（与 GET /orders/preview 的查询参数同一套整数规则），
并额外拒绝布尔值——JSON 布尔在 lax int 下会被当作 0/1，而查询参数入口
里 "true" 本来就无法解析为整数，为保证两个入口结论一致，这里显式拒绝。
任何类型/格式不合法都会由 FastAPI 归一为 RequestValidationError ->
400 client_error/INVALID_REQUEST，不进入业务与内部异常路径。
"""

from __future__ import annotations

from pydantic import BaseModel, field_validator


class OrderRequest(BaseModel):
    """POST /orders 的 JSON 请求体。

    - item 缺省时为空串，沿用既有"未知商品 -> 业务拒绝(422)"的语义；
    - quantity 缺省时为 0，沿用既有"数量非正 -> 业务拒绝(422)"的语义；
    - quantity 类型不合法（文字、null、布尔、非整数值的小数、数组、对象）
      在进入业务逻辑前即按调用方错误(400)拒绝，不会被静默截断。
    """

    item: str = ""
    quantity: int = 0

    @field_validator("quantity", mode="before")
    @classmethod
    def _reject_boolean_quantity(cls, value: object) -> object:
        # bool 是 int 的子类型，必须先于整数判定拦截，
        # 否则 True/False 会被静默当成 1/0 放行下单。
        if isinstance(value, bool):
            raise ValueError("quantity must be an integer; boolean is not accepted")
        return value
