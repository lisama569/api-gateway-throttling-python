"""下单请求体模型：请求体参数的类型/格式判定规则集中在这里。

判定规则（与查询参数入口的 int 解析口径一致）：
- quantity 必须是 JSON 整数：字符串（含 "3"）、null、布尔、带小数的数
  （含 1.0 这类浮点写法）一律拒绝，不做 int() 截断或真值转换；
- 缺省 quantity 按既有对外语义视为 0，交由业务层按业务拒绝处理；
- item 必须是字符串，缺省为空串（既有语义）；
- 未识别字段忽略，保持向前兼容。

校验失败抛出 FastAPI RequestValidationError，由 main.py 的统一处理器
归一化为 400 client_error / INVALID_REQUEST，与查询参数入口同结构。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr


class OrderPayload(BaseModel):
    """POST /orders 请求体。"""

    model_config = ConfigDict(extra="ignore")

    item: StrictStr = ""
    quantity: StrictInt = 0
