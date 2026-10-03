"""下单请求体参数校验：非法数量一律按调用方错误拒绝，不静默转换、不落 5xx。

覆盖边界：
- quantity 为文字/数字字符串、null、布尔、带小数的数、数组、对象 -> 400 client_error
- item 类型不对（非字符串） -> 400 client_error
- 请求体不是合法 JSON、不是 JSON 对象 -> 400 client_error
- 与查询参数入口（GET /orders/preview）的分类与错误结构一致
- 响应头与响应体 request_id 一致，错误信息不泄露内部细节
- 既有语义不回归：合法下单 200、quantity<=0 业务拒绝 422、
  依赖失败 502、未预期异常 500
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app

logger = logging.getLogger("test.order_body_validation")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def fresh_headers(caller: str) -> dict[str, str]:
    """每个用例使用独立 caller，避免共享限流额度互相干扰。"""
    request_id = uuid.uuid4().hex[:12]
    h = {"X-Caller-Id": caller, "X-Request-Id": request_id}
    logger.info("request_id=%s caller=%s 判定依据: 独立 caller 隔离限流桶", request_id, caller)
    return h


# ---------- 非法 quantity：一律 400 client_error ----------

INVALID_QUANTITIES = [
    "abc",      # 一串文字
    "3",        # 数字字符串：JSON 层面不是整数，与查询参数入口口径一致
    None,       # null
    True,       # 布尔：不得被当成 1
    False,      # 布尔：不得被当成 0 走业务拒绝
    1.9,        # 带小数：不得被截断成 1
    1.0,        # 浮点写法：JSON 层面不是整数
    [1],        # 数组
    {"n": 1},   # 对象
]


@pytest.mark.parametrize("bad_quantity", INVALID_QUANTITIES)
def test_invalid_quantity_is_client_error(client: TestClient, bad_quantity):
    h = fresh_headers(f"qty-{uuid.uuid4().hex[:8]}")
    r = client.post("/orders", json={"item": "book", "quantity": bad_quantity}, headers=h)
    body = r.json()
    assert r.status_code == 400, r.text
    err = body["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    assert "order" not in body  # 不得让调用方误以为下单成功
    logger.info(
        "request_id=%s 判定依据: quantity=%r 不是合法 JSON 整数 -> 400 client_error",
        h["X-Request-Id"], bad_quantity,
    )


def test_rejected_quantity_does_not_place_order(client: TestClient):
    """true/1.9 这类值被明确拒绝，而不是静默当成 1 下单成功。"""
    h = fresh_headers("qty-no-silent")
    for bad in (True, 1.9):
        r = client.post("/orders", json={"item": "book", "quantity": bad}, headers=h)
        assert r.status_code == 400
        assert "order" not in r.json()
    # 拒绝后库存未被扣减：随后正常下单仍按真实数量成交
    ok = client.post("/orders", json={"item": "book", "quantity": 3}, headers=h)
    assert ok.status_code == 200
    assert ok.json()["order"] == {"item": "book", "quantity": 3, "status": "confirmed"}
    logger.info(
        "request_id=%s 判定依据: true/1.9 被拒且未扣库存, 后续 quantity=3 正常成交",
        h["X-Request-Id"],
    )


def test_item_with_wrong_type_is_client_error(client: TestClient):
    h = fresh_headers("item-type")
    r = client.post("/orders", json={"item": 123, "quantity": 1}, headers=h)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_REQUEST"
    logger.info("request_id=%s 判定依据: item=123 非字符串 -> 400 client_error", h["X-Request-Id"])


# ---------- 请求体本身非法 ----------

def test_body_not_valid_json_is_client_error(client: TestClient):
    h = fresh_headers("body-json")
    r = client.post("/orders", content="{bad-json",
                    headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    logger.info("request_id=%s 判定依据: 请求体不是合法 JSON -> 400 client_error", h["X-Request-Id"])


@pytest.mark.parametrize("raw", ["[1, 2]", "\"just-a-string\"", "42", "null"])
def test_body_not_object_is_client_error(client: TestClient, raw: str):
    h = fresh_headers(f"body-shape-{uuid.uuid4().hex[:8]}")
    r = client.post("/orders", content=raw,
                    headers={**h, "Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    logger.info("request_id=%s 判定依据: 请求体 %s 不是 JSON 对象 -> 400 client_error",
                h["X-Request-Id"], raw)


# ---------- 与查询参数入口一致 ----------

def test_body_and_query_entries_reject_consistently(client: TestClient):
    """同一个参数问题，走请求体入口与查询参数入口得到相同分类与结构。"""
    h1, h2 = fresh_headers("cons-body"), fresh_headers("cons-query")
    r1 = client.post("/orders", json={"item": "book", "quantity": "abc"}, headers=h1)
    r2 = client.get("/orders/preview", params={"item": "book", "quantity": "abc"}, headers=h2)
    e1, e2 = r1.json()["error"], r2.json()["error"]
    assert r1.status_code == r2.status_code == 400
    assert e1["category"] == e2["category"] == "client_error"
    assert e1["code"] == e2["code"] == "INVALID_REQUEST"
    assert set(e1) == set(e2)  # 错误结构字段一致
    logger.info("判定依据: quantity=abc 在两个入口均为 400 client_error/INVALID_REQUEST")


# ---------- request_id 一致性与不泄露内部细节 ----------

def test_request_id_consistent_between_header_and_body(client: TestClient):
    h = fresh_headers("rid-consistency")
    r = client.post("/orders", json={"item": "book", "quantity": None}, headers=h)
    assert r.status_code == 400
    assert r.headers["X-Request-Id"] == h["X-Request-Id"]
    assert r.json()["error"]["request_id"] == h["X-Request-Id"]
    logger.info("request_id=%s 判定依据: 响应头与响应体 request_id 同值", h["X-Request-Id"])


def test_error_message_does_not_leak_internals(client: TestClient):
    h = fresh_headers("no-leak")
    r = client.post("/orders", json={"item": "book", "quantity": "abc"}, headers=h)
    assert r.status_code == 400
    for token in ("Traceback", "ValueError", "int()", "place_order", "main.py"):
        assert token not in r.text
    logger.info("request_id=%s 判定依据: 400 响应不含堆栈/内部实现细节", h["X-Request-Id"])


# ---------- 既有对外语义不回归 ----------

def test_valid_order_still_succeeds(client: TestClient):
    h = fresh_headers("sem-ok")
    r = client.post("/orders", json={"item": "book", "quantity": 2}, headers=h)
    assert r.status_code == 200
    assert r.json()["order"] == {"item": "book", "quantity": 2, "status": "confirmed"}
    assert r.json()["request_id"] == h["X-Request-Id"]


@pytest.mark.parametrize("quantity", [0, -3])
def test_non_positive_quantity_remains_business_rejection(client: TestClient, quantity: int):
    h = fresh_headers(f"sem-biz-{uuid.uuid4().hex[:8]}")
    r = client.post("/orders", json={"item": "book", "quantity": quantity}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "business_rejection"
    logger.info("request_id=%s 判定依据: quantity=%d 是合法整数但违反业务规则 -> 422",
                h["X-Request-Id"], quantity)


def test_missing_quantity_keeps_legacy_default(client: TestClient):
    """缺省 quantity 维持既有语义：按 0 处理，走业务拒绝而非参数错误。"""
    h = fresh_headers("sem-default")
    r = client.post("/orders", json={"item": "book"}, headers=h)
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "business_rejection"
    logger.info("request_id=%s 判定依据: 缺省 quantity 维持既有默认 0 -> 422 业务拒绝",
                h["X-Request-Id"])


def test_dependency_failure_and_internal_error_unchanged(client: TestClient):
    h = fresh_headers("sem-failures")
    r1 = client.post("/orders", json={"item": "fragile", "quantity": 1}, headers=h)
    assert r1.status_code == 502
    assert r1.json()["error"]["category"] == "dependency_failure"
    r2 = client.post("/orders", json={"item": "boom", "quantity": 1}, headers=h)
    assert r2.status_code == 500
    assert r2.json()["error"]["category"] == "internal_error"
    logger.info("request_id=%s 判定依据: 依赖失败 502 / 未预期异常 500 语义不变",
                h["X-Request-Id"])
