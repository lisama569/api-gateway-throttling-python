"""端到端：组合配额隔离/跨入口一致 + 失败诊断（关联标识、错误分类）。"""

from __future__ import annotations

import logging
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from app.quotas import QuotaConfig, QuotaLimiter, WindowSpec
import main

logger = logging.getLogger("test.diagnostics")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(main.app, raise_server_exceptions=False)


def headers(caller: str, version: str | None = None) -> dict[str, str]:
    h = {"X-Caller-Id": caller, "X-Request-Id": uuid.uuid4().hex[:12]}
    if version is not None:
        h["X-Api-Version"] = version
    return h


# ---------- 组合配额：HTTP 层隔离、总量、跨入口 ----------

def _patch_limiter(monkeypatch, burst=2, sustained=2, burst_w=10.0,
                   sustained_w=60.0, caller_total=10) -> None:
    monkeypatch.setattr(
        main,
        "quota_limiter",
        QuotaLimiter(QuotaConfig(
            burst=WindowSpec("burst", burst, burst_w),
            sustained=WindowSpec("sustained", sustained, sustained_w),
            caller_total=caller_total,
        )),
    )


def test_version_quotas_isolated_per_caller_http(client, monkeypatch):
    _patch_limiter(monkeypatch, burst=2, sustained=2, caller_total=10)
    h = headers("diag-ver-iso", "v1")
    codes = [client.post("/orders", json={"item": "book", "quantity": 1}, headers=h).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]  # v1 组合额度耗尽
    # 同 caller 的 v2 各用各的额度，不被 v1 挤占
    hv2 = {**h, "X-Api-Version": "v2", "X-Request-Id": uuid.uuid4().hex[:12]}
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=hv2)
    assert r.status_code == 200


def test_caller_total_cap_binds_across_versions_http(client, monkeypatch):
    _patch_limiter(monkeypatch, burst=10, sustained=10, caller_total=3)
    results = []
    for i, version in enumerate(("v1", "v2", "v1", "v2")):
        h = headers("diag-total", version)
        h["X-Request-Id"] = f"tot-{i}"
        results.append(client.post("/orders", json={"item": "book", "quantity": 1},
                                   headers=h).status_code)
    assert results == [200, 200, 200, 429]  # 各组合没满，但总量超了也拒绝
    r = client.post("/orders", json={"item": "book", "quantity": 1},
                    headers=headers("diag-total-other", "v1"))
    assert r.status_code == 200  # 别的 caller 不受影响


def test_same_caller_version_shares_quota_across_entries(client, monkeypatch):
    _patch_limiter(monkeypatch, burst=2, sustained=2, caller_total=10)
    h = headers("diag-shared", "v1")
    assert client.post("/orders", json={"item": "book", "quantity": 1}, headers=h).status_code == 200
    assert client.get("/orders/preview", headers=h).status_code == 200
    # 第三个请求落在任一入口都应被同一份额度拒绝
    r1 = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    r2 = client.get("/orders/preview", headers=h)
    assert r1.status_code == r2.status_code == 429


# ---------- 关联标识：头/体一致、所有路径覆盖、日志串联 ----------

def test_request_id_consistent_across_all_outcomes(client):
    caller = "diag-rid"
    rid_success = uuid.uuid4().hex[:12]

    # 1) 成功：响应头带，响应体也带
    r = client.post("/orders", json={"item": "book", "quantity": 1},
                    headers={"X-Caller-Id": caller, "X-Request-Id": rid_success})
    assert r.status_code == 200
    assert r.headers["X-Request-Id"] == rid_success
    assert r.json()["request_id"] == rid_success

    # 2) 版本不被接受
    rid_ver = uuid.uuid4().hex[:12]
    r = client.post("/orders", json={"item": "book", "quantity": 1},
                    headers={"X-Caller-Id": caller, "X-Request-Id": rid_ver,
                             "X-Api-Version": "v9"})
    assert r.status_code == 400
    assert r.headers["X-Request-Id"] == rid_ver
    assert r.json()["error"]["request_id"] == rid_ver

    # 3) 被限流：默认每 caller 5 个名额，打满后再请求
    for i in range(6):
        r = client.post("/orders", json={"item": "book", "quantity": 1},
                        headers={"X-Caller-Id": caller,
                                 "X-Request-Id": f"rid-429-{i}"})
    assert r.status_code == 429
    assert r.headers["X-Request-Id"] == "rid-429-5"
    assert r.json()["error"]["request_id"] == "rid-429-5"

    # 4) 参数格式不对
    rid_bad = uuid.uuid4().hex[:12]
    r = client.post("/orders", content=b'{"item": "book", "quantity": "two"}',
                    headers={"X-Caller-Id": "diag-rid-bad", "X-Request-Id": rid_bad,
                             "Content-Type": "application/json"})
    assert r.status_code == 422
    assert r.headers["X-Request-Id"] == rid_bad
    assert r.json()["error"]["request_id"] == rid_bad

    # 5) 访问不存在的位置
    rid_404 = uuid.uuid4().hex[:12]
    r = client.get("/no/such/path",
                   headers={"X-Caller-Id": caller, "X-Request-Id": rid_404})
    assert r.status_code == 404
    assert r.headers["X-Request-Id"] == rid_404
    assert r.json()["error"]["request_id"] == rid_404


def test_server_generates_request_id_when_absent(client):
    r = client.get("/no/such/path", headers={"X-Caller-Id": "diag-gen"})
    rid = r.headers.get("X-Request-Id")
    assert rid and re.fullmatch(r"[0-9a-f]{12}", rid)
    assert r.json()["error"]["request_id"] == rid  # 头体同一个值


def test_request_id_threads_server_logs(client, caplog):
    rid = uuid.uuid4().hex[:12]
    with caplog.at_level(logging.INFO, logger="gateway"):
        client.post("/orders", json={"item": "book", "quantity": 1},
                    headers={"X-Caller-Id": "diag-log", "X-Request-Id": rid})
    assert rid in " ".join(rec.getMessage() for rec in caplog.records)


# ---------- 调用方侧错误分类 ----------

def test_malformed_json_is_invalid_request(client):
    r = client.post("/orders", content=b"{not-json",
                    headers={"X-Caller-Id": "diag-json",
                             "X-Request-Id": uuid.uuid4().hex[:12],
                             "Content-Type": "application/json"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["category"] == "invalid_request"
    assert err["code"] == "INVALID_REQUEST"


def test_wrong_type_is_invalid_request_not_internal(client):
    r = client.post("/orders", json={"item": "book", "quantity": "x"},
                    headers={"X-Caller-Id": "diag-type",
                             "X-Request-Id": uuid.uuid4().hex[:12]})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["category"] == "invalid_request"
    assert "integer" in err["message"]


def test_missing_quantity_still_business_rejection(client):
    # 兼容旧行为：缺 quantity 仍是业务规则拒绝，而不是格式错误/内部异常
    r = client.post("/orders", json={"item": "book"},
                    headers={"X-Caller-Id": "diag-missing",
                             "X-Request-Id": uuid.uuid4().hex[:12]})
    assert r.status_code == 422
    assert r.json()["error"]["category"] == "business_rejection"


def test_unknown_path_is_not_found(client):
    r = client.get("/no/such/path",
                   headers={"X-Caller-Id": "diag-404",
                            "X-Request-Id": uuid.uuid4().hex[:12]})
    err = r.json()["error"]
    assert r.status_code == 404
    assert err["category"] == "not_found"
    assert err["code"] == "NOT_FOUND"


# ---------- 429 重试语义 ----------

def test_429_carries_rejected_by_and_retry_after(client, monkeypatch):
    _patch_limiter(monkeypatch, burst=1, sustained=10, caller_total=10)
    h = headers("diag-429", "v1")
    assert client.post("/orders", json={"item": "book", "quantity": 1}, headers=h).status_code == 200
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 429
    err = r.json()["error"]
    assert "combo_burst" in err["rejected_by"]
    assert float(r.headers["Retry-After"]) == pytest.approx(err["retry_after_seconds"])
    assert err["retry_after_seconds"] > 0
