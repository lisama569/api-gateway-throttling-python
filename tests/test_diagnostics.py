"""失败诊断测试：request_id 在响应头/响应体/日志中贯穿同一次请求，
客户端输入错误明确归类为 client_error，不存在的位置同样可追踪。"""

from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app

client = TestClient(app, raise_server_exceptions=False)


def _headers(request_id: str | None = uuid.uuid4().hex[:12]) -> dict[str, str]:
    h: dict[str, str] = {"X-Caller-Id": "diag-" + uuid.uuid4().hex[:8]}
    if request_id is not None:
        h["X-Request-Id"] = request_id
    return h


def _assert_request_id_consistent(r, request_id: str) -> None:
    assert r.headers["X-Request-Id"] == request_id
    assert r.json()["error"]["request_id"] == request_id


def test_success_carries_request_id_in_header_and_body():
    rid = uuid.uuid4().hex[:12]
    r = client.post("/orders", json={"item": "book", "quantity": 1},
                    headers=_headers(rid))
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == rid
    assert r.json()["request_id"] == rid


def test_rate_limited_request_id_consistency():
    rid = uuid.uuid4().hex[:12]
    h = _headers(rid)
    for _ in range(6):
        client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 429
    _assert_request_id_consistent(r, rid)


def test_unsupported_version_request_id_consistency():
    rid = uuid.uuid4().hex[:12]
    h = {**_headers(rid), "X-Api-Version": "v9"}
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 400
    _assert_request_id_consistent(r, rid)


def test_malformed_json_is_client_error_not_internal():
    """请求体不是合法 JSON：属于调用方侧错误（400 client_error），
    不能混进服务端内部异常（500 internal_error）。"""
    rid = uuid.uuid4().hex[:12]
    r = client.post(
        "/orders",
        content="{not valid json",
        headers={**_headers(rid), "Content-Type": "application/json"},
    )
    assert r.status_code == 400, r.text
    err = r.json()["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    _assert_request_id_consistent(r, rid)


def test_bad_query_param_type_is_client_error():
    rid = uuid.uuid4().hex[:12]
    r = client.get("/orders/preview", params={"quantity": "abc"},
                   headers=_headers(rid))
    assert r.status_code == 400
    err = r.json()["error"]
    assert err["category"] == "client_error"
    assert err["code"] == "INVALID_REQUEST"
    assert err["details"]
    _assert_request_id_consistent(r, rid)


def test_unknown_location_is_trackable():
    """访问不存在的位置：404 归一化且带同一个 request_id。"""
    rid = uuid.uuid4().hex[:12]
    r = client.get("/no/such/place", headers=_headers(rid))
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "NOT_FOUND"
    assert err["category"] == "client_error"
    _assert_request_id_consistent(r, rid)


def test_server_generated_request_id_when_absent():
    r = client.post("/orders", json={"item": "book", "quantity": 1},
                    headers={"X-Caller-Id": "diag-noid"})
    assert r.status_code == 200
    rid_header = r.headers["X-Request-Id"]
    rid_body = r.json()["request_id"]
    assert rid_header and rid_header == rid_body
    assert len(rid_header) == 12


def test_invalid_provided_request_id_replaced():
    """调用方提供的标识含非法字符时，服务端生成合规标识并保持三处一致。"""
    r = client.post(
        "/orders", json={"item": "book", "quantity": 1},
        headers={"X-Caller-Id": "diag-bad", "X-Request-Id": "bad id\nINJECT"},
    )
    assert r.status_code == 200
    rid = r.headers["X-Request-Id"]
    assert "\n" not in rid
    assert r.json()["request_id"] == rid


@pytest.mark.parametrize("payload,status,category", [
    ({"item": "book", "quantity": 0}, 422, "business_rejection"),
    ({"item": "fragile", "quantity": 1}, 502, "dependency_failure"),
    ({"item": "boom", "quantity": 1}, 500, "internal_error"),
])
def test_all_failure_paths_keep_request_id(payload, status, category):
    rid = uuid.uuid4().hex[:12]
    r = client.post("/orders", json=payload, headers=_headers(rid))
    assert r.status_code == status
    err = r.json()["error"]
    assert err["category"] == category
    _assert_request_id_consistent(r, rid)


def test_request_id_appears_in_server_logs(caplog):
    """服务端日志能用 request_id 串起同一次请求。"""
    rid = uuid.uuid4().hex[:12]
    with caplog.at_level(logging.INFO, logger="gateway"):
        client.post("/orders", json={"item": "book", "quantity": 1},
                    headers=_headers(rid))
    assert any(rid in record.getMessage() for record in caplog.records), [
        rec.getMessage() for rec in caplog.records
    ]
