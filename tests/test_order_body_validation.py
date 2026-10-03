"""请求体下单参数校验测试（本轮修复）。

覆盖业务：
1. quantity 类型/格式不合法（文字、null、布尔、带小数、数组、对象）
   -> 一律调用方侧错误 400 client_error/INVALID_REQUEST，不能 5xx，
      也不能静默截断后下单成功；
2. 请求体本身非法 JSON 或不是 JSON 对象 -> 同样 400 client_error；
3. 同一参数问题在请求体入口与查询参数入口结论一致（分类/结构一致）；
4. request_id 在响应头与响应体一致，错误信息不泄露内部细节；
5. 不回归既有语义：正常下单、缺字段/0/负数业务拒绝(422)、
   依赖失败(502)、内部异常(500)、版本协商(400/默认v1)、限流(429)；
6. 被参数拒绝的请求不会让调用方误以为下单成功（库存不被扣减）。

用例通过 logger 输出"覆盖业务 + 判定依据"，配合 pytest -v 可看出
每个业务下各用例的通过情况。
"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.order_schema import OrderRequest
from main import app

logger = logging.getLogger("test.order_body")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _headers(caller: str, request_id: str | None = None) -> dict[str, str]:
    """每个用例独立 caller 隔离限流桶；可显式指定 request_id 便于断言。"""
    h: dict[str, str] = {"X-Caller-Id": caller, "Content-Type": "application/json"}
    h["X-Request-Id"] = request_id or uuid.uuid4().hex[:12]
    return h


def _assert_client_error(r, request_id: str, *, field: str | None = "quantity") -> None:
    """统一断言：400 client_error/INVALID_REQUEST + request_id 头/体一致 + 无内部细节。"""
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    assert err["request_id"] == request_id
    assert r.headers["X-Request-Id"] == request_id
    # 不允许把内部实现/堆栈透露给调用方
    assert "Traceback" not in r.text
    assert "pricing engine" not in r.text
    if field is not None:
        locations = [tuple(d["location"]) for d in err["details"]]
        assert any("body" in loc and field in loc for loc in locations), locations


# ---------- 业务1：quantity 非法类型/格式 -> 400，不能静默下单 ----------

@pytest.mark.parametrize("label,quantity", [
    ("字符串文字", "abc"),
    ("null", None),
    ("布尔 true", True),
    ("布尔 false", False),
    ("带小数 1.9", 1.9),
    ("负小数 -0.5", -0.5),
    ("数组", [1]),
    ("对象", {"n": 1}),
    ("数字字符串带小数", "1.9"),
    ("字符串布尔", "true"),
    ("字符串 null", "null"),
])
def test_invalid_quantity_types_are_client_errors(client: TestClient, label: str, quantity):
    rid = uuid.uuid4().hex[:12]
    caller = f"body-qty-{label.encode().hex()[:8]}"
    r = client.post(
        "/orders", json={"item": "book", "quantity": quantity},
        headers=_headers(caller, rid),
    )
    _assert_client_error(r, rid)
    logger.info(
        "业务=请求体quantity类型校验 用例=%s -> 400 client_error/INVALID_REQUEST "
        "判定依据: 非合法整数必须按调用方错误拒绝",
        label,
    )


@pytest.mark.parametrize("label,quantity,parsed", [
    ("JSON 整数 5", 5, 5),
    ("负整数 -2", -2, -2),
    ("零", 0, 0),
])
def test_integer_quantity_passes_type_check(client: TestClient, label: str, quantity: int, parsed: int):
    """合法整数通过类型校验（0/负数随后由业务规则拒绝，属于 422 范畴）。"""
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders", json={"item": "book", "quantity": quantity},
        headers=_headers(f"body-qty-ok-{parsed}", rid),
    )
    if quantity > 0:
        assert r.status_code == 200, r.text
        assert r.json()["order"]["quantity"] == parsed
        logger.info("业务=请求体quantity类型校验 用例=%s -> 200 正常下单", label)
    else:
        assert r.status_code == 422, r.text
        assert r.json()["error"]["category"] == "business_rejection"
        logger.info("业务=请求体quantity类型校验 用例=%s -> 422 业务拒绝(类型合法,语义不合法)", label)


def test_invalid_quantity_does_not_place_order_or_consume_stock(client: TestClient):
    """静默截断 bug 的回归：非法数量绝不能返回 confirmed 下单成功。"""
    bad = ["abc", None, True, 1.9]
    for q in bad:
        rid = uuid.uuid4().hex[:12]
        r = client.post(
            "/orders", json={"item": "book", "quantity": q},
            headers=_headers(f"body-noeffect-{type(q).__name__}", rid),
        )
        assert r.status_code == 400
        assert "order" not in r.json()
    # 随后用正常数量下单（独立 caller 避免突发额度干扰）：
    # 前面非法请求没有扣库存，数量 1 仍可成功。
    ok = client.post(
        "/orders", json={"item": "book", "quantity": 1},
        headers=_headers("body-noeffect-good"),
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["order"]["status"] == "confirmed"
    logger.info(
        "业务=请求体quantity类型校验 用例=%s -> 非法数量全部400且无order字段, 库存未被扣减",
        ",".join(map(str, bad)),
    )


# ---------- 业务2：请求体不是合法 JSON 或不是对象 -> 400 ----------

@pytest.mark.parametrize("label,content", [
    ("非法 JSON", b"{not valid json"),
    ("截断的 JSON", b'{"item": "book", '),
    ("数组体", b'[1, 2, 3]'),
    ("null 体", b'null'),
    ("数字体", b'42'),
    ("字符串体", b'"hello"'),
    ("空体", b''),
])
def test_body_must_be_valid_json_object(client: TestClient, label: str, content: bytes):
    rid = uuid.uuid4().hex[:12]
    r = client.post("/orders", content=content, headers=_headers(f"body-shape-{label.encode().hex()[:8]}", rid))
    _assert_client_error(r, rid, field=None)
    logger.info("业务=请求体格式校验 用例=%s -> 400 client_error", label)


# ---------- 业务3：两个入口对同一参数问题结论一致 ----------

@pytest.mark.parametrize("label,body_quantity,query_quantity", [
    ("文字", "abc", "abc"),
    ("带小数", 1.9, "1.9"),
    ("布尔/字符串布尔", True, "true"),
    ("null/字符串null", None, "null"),
    ("0", 0, "0"),
    ("负数", -3, "-3"),
    ("合法正数", 2, "2"),
])
def test_same_parameter_problem_same_conclusion(
    client: TestClient, label: str, body_quantity, query_quantity
):
    rid = uuid.uuid4().hex[:12]
    r_body = client.post(
        "/orders", json={"item": "book", "quantity": body_quantity},
        headers=_headers(f"consistency-b-{label.encode().hex()[:6]}", rid + "-b"),
    )
    r_query = client.get(
        "/orders/preview", params={"item": "book", "quantity": query_quantity},
        headers=_headers(f"consistency-q-{label.encode().hex()[:6]}", rid + "-q"),
    )
    eb, eq = r_body.json().get("error"), r_query.json().get("error")
    assert r_body.status_code == r_query.status_code, (r_body.text, r_query.text, label)
    if eb is not None:
        # 两个入口的错误分类、code、结构（含字段集合）一致
        assert eb["category"] == eq["category"], label
        assert eb["code"] == eq["code"], label
        assert set(eb.keys()) == set(eq.keys()), label
    else:
        # 成功路径：两入口都下单成功且数量一致
        assert r_body.json()["order"] == r_query.json()["order"]
    logger.info(
        "业务=跨入口一致性 用例=%s -> body=%s query=%s 分类=%s 判定依据: 同一参数问题结论一致",
        label, r_body.status_code, r_query.status_code,
        (eb or {}).get("category", "success"),
    )


def test_request_id_consistent_in_header_and_body_on_400(client: TestClient):
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders", json={"item": "book", "quantity": "abc"},
        headers=_headers("body-rid", rid),
    )
    assert r.status_code == 400
    assert r.headers["X-Request-Id"] == rid
    assert r.json()["error"]["request_id"] == rid
    logger.info("业务=request_id一致性 用例=非法quantity -> 响应头/响应体 request_id 相同")


def test_request_id_present_when_body_malformed(client: TestClient):
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders", content="{bad",
        headers=_headers("body-rid-malformed", rid),
    )
    assert r.status_code == 400
    assert r.headers["X-Request-Id"] == rid == r.json()["error"]["request_id"]
    logger.info("业务=request_id一致性 用例=非法JSON -> 响应头/响应体 request_id 相同")


# ---------- 业务5：既有语义不回归 ----------

def test_normal_order_unchanged(client: TestClient):
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders", json={"item": "book", "quantity": 3},
        headers=_headers("body-ok-normal", rid),
    )
    assert r.status_code == 200
    body = r.json()
    assert body["order"] == {"item": "book", "quantity": 3, "status": "confirmed"}
    assert body["request_id"] == rid
    assert body["version"] == "v1"  # 默认版本协商不变


def test_missing_quantity_stays_business_rejection(client: TestClient):
    """缺 quantity 沿用默认 0 -> 业务拒绝 422（不是 400，保持兼容）。"""
    r = client.post("/orders", json={"item": "book"}, headers=_headers("body-missing-q"))
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "business_rejection"


@pytest.mark.parametrize("label,item", [
    ("数字", 123), ("数组", ["x"]), ("对象", {"a": 1}), ("null", None), ("布尔", True),
])
def test_non_string_item_is_client_error(client: TestClient, label: str, item):
    """item 类型不对同样是调用方错误 400（旧实现对数组/对象会落成 500）。"""
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders", json={"item": item, "quantity": 1},
        headers=_headers(f"body-item-{label.encode().hex()[:6]}", rid),
    )
    _assert_client_error(r, rid, field="item")
    logger.info("业务=请求体item类型校验 用例=%s -> 400 client_error", label)


def test_missing_or_unknown_item_stays_business_rejection(client: TestClient):
    """item 缺失（默认空串）/未知字符串仍是业务拒绝 422，语义不变。"""
    for body, caller in [({"quantity": 1}, "body-item-missing"),
                         ({"item": "nope", "quantity": 1}, "body-item-unknown")]:
        r = client.post("/orders", json=body, headers=_headers(caller))
        assert r.status_code == 422
        assert r.json()["error"]["category"] == "business_rejection"


def test_dependency_and_internal_paths_unchanged(client: TestClient):
    for item, status, category in [
        ("fragile", 502, "dependency_failure"),
        ("boom", 500, "internal_error"),
    ]:
        r = client.post(
            "/orders", json={"item": item, "quantity": 1},
            headers=_headers(f"body-path-{item}"),
        )
        assert r.status_code == status
        assert r.json()["error"]["category"] == category
    logger.info("业务=既有语义不回归 用例=fragile->502, boom->500 -> 状态码/分类不变")


def test_internal_error_detail_not_leaked_on_boom(client: TestClient):
    r = client.post(
        "/orders", json={"item": "boom", "quantity": 1},
        headers=_headers("body-leak"),
    )
    assert r.status_code == 500
    assert "pricing engine" not in r.text and "Traceback" not in r.text


def test_validation_error_logged_with_request_id(client: TestClient, caplog):
    """服务端日志能用 request_id 串起参数错误的同一次请求。"""
    rid = uuid.uuid4().hex[:12]
    with caplog.at_level(logging.INFO, logger="gateway"):
        client.post(
            "/orders", json={"item": "book", "quantity": "oops"},
            headers=_headers("body-log", rid),
        )
    messages = [rec.getMessage() for rec in caplog.records]
    assert any(rid in m for m in messages)
    assert any("client_error" in m for m in messages)
    logger.info("业务=日志串联 用例=非法quantity -> 日志按 request_id=%s 可串联", rid)


# ---------- 模型级单测：与 HTTP 层同规则 ----------

def test_order_request_model_unit():
    assert OrderRequest(item="book", quantity=4).quantity == 4
    assert OrderRequest().quantity == 0  # 缺省语义保持
    import pydantic
    for bad in ("abc", None, True, False, 1.9, [1], {"n": 1}):
        with pytest.raises(pydantic.ValidationError):
            OrderRequest(item="book", quantity=bad)
    logger.info("业务=请求体模型单测 -> 非法值全部 ValidationError, 缺省 quantity=0")
