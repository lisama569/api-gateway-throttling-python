"""请求关联标识（request_id）：生成、传递与贯穿。

- 调用方可通过请求头 X-Request-Id 自行提供；缺省/为空/格式不合法时
  服务端生成（12 位十六进制）。
- 合法字符限定为字母数字及 . _ - :，长度不超过 128，避免日志注入。
- 同一次请求中：响应头、响应体、服务端日志使用同一个值。
"""

from __future__ import annotations

import re
import uuid

from starlette.requests import Request

REQUEST_ID_HEADER = "X-Request-Id"
REQUEST_ID_STATE_KEY = "request_id"

_ACCEPTED = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def ensure_request_id(provided: str | None) -> str:
    """规范化调用方提供的标识，缺省/为空/不合法时服务端生成。"""
    if provided is not None:
        candidate = provided.strip()
        if _ACCEPTED.match(candidate):
            return candidate
    return uuid.uuid4().hex[:12]


def assign_request_id(request: Request) -> str:
    """为请求确定 request_id 并挂到 request.state，返回该值。"""
    request_id = ensure_request_id(request.headers.get(REQUEST_ID_HEADER))
    setattr(request.state, REQUEST_ID_STATE_KEY, request_id)
    return request_id


def get_request_id(request: Request) -> str:
    """读取当前请求的 request_id；中间件未执行时兜底生成。"""
    request_id = getattr(request.state, REQUEST_ID_STATE_KEY, None)
    if request_id is None:
        request_id = ensure_request_id(None)
        setattr(request.state, REQUEST_ID_STATE_KEY, request_id)
    return request_id
