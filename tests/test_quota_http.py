"""HTTP 层组合配额集成测试：(调用方, 版本) 隔离、跨入口同一份额度、
版本拒绝不扣额度、调用方总量上限、retry_after 指导重试。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from main import app


def _client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def _headers(caller: str, version: str | None = None) -> dict[str, str]:
    h = {"X-Caller-Id": caller, "X-Request-Id": uuid.uuid4().hex[:12]}
    if version is not None:
        h["X-Api-Version"] = version
    return h


def test_v1_v2_quotas_isolated_per_caller():
    """同一 caller 的 v1 耗尽不影响 v2；不同 caller 也互不挤占。"""
    client = _client()
    h_v1 = _headers("iso-v", "v1")
    h_v2 = _headers("iso-v", "v2")
    h_other = _headers("iso-other", "v1")

    # 默认突发 5/60s：把该 caller 的 v1 打到限流
    codes = [
        client.post("/orders", json={"item": "book", "quantity": 1}, headers=h_v1).status_code
        for _ in range(7)
    ]
    assert codes.count(200) == 5
    assert codes.count(429) == 2

    # 同一 caller 的 v2 仍有完整额度
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h_v2)
    assert r.status_code == 200
    assert r.json()["version"] == "v2"

    # 不同 caller 的 v1 也不受影响
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h_other)
    assert r.status_code == 200


def test_same_combo_shares_quota_across_entries():
    """同一 (caller, version) 经过两个入口时按同一份额度判定。"""
    client = _client()
    h = _headers("iso-entry", "v1")
    codes = []
    for _ in range(3):
        codes.append(client.post(
            "/orders", json={"item": "book", "quantity": 1}, headers=h).status_code)
    for _ in range(4):
        codes.append(client.get("/orders/preview", headers=h).status_code)
    assert codes.count(200) == 5
    assert codes.count(429) == 2


def test_caller_total_cap_spans_versions():
    """各版本组合都没满，但跨版本总量超了也要拒绝。

    这里换一个 burst=10、总量=3 的管理器：3 次放行后第 4 次请求
    所属组合仍有充足突发额度，唯一拒绝维度是调用方总量。
    """
    import main
    from app.quota import ComboQuota, QuotaManager, WindowSpec

    original = main.quota_manager
    main.quota_manager = QuotaManager(
        combo_quotas=(
            ComboQuota(None, "v1", WindowSpec(10, 60.0), WindowSpec(100, 300.0)),
            ComboQuota(None, "v2", WindowSpec(10, 60.0), WindowSpec(100, 300.0)),
        ),
        caller_total=WindowSpec(3, 60.0),
    )
    try:
        client = _client()
        hv1 = _headers("iso-total", "v1")
        hv2 = _headers("iso-total", "v2")
        for _ in range(2):
            assert client.post("/orders", json={"item": "book", "quantity": 1},
                               headers=hv1).status_code == 200
        assert client.post("/orders", json={"item": "book", "quantity": 1},
                           headers=hv2).status_code == 200
        r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=hv2)
        assert r.status_code == 429
        err = r.json()["error"]
        assert err["reason"] == "caller_total"
        assert err["retry_after_seconds"] > 0
    finally:
        main.quota_manager = original


def test_unsupported_version_does_not_consume_quota():
    """版本协商先于配额判定：被版本拒绝的请求不占用任何额度。"""
    client = _client()
    h = _headers("iso-ver", "v9")
    for _ in range(8):
        assert client.post("/orders", json={"item": "book", "quantity": 1},
                           headers=h).status_code == 400
    # 切回受支持版本，5 次突发额度原封不动
    ok_headers = _headers("iso-ver", "v1")
    for _ in range(5):
        assert client.post("/orders", json={"item": "book", "quantity": 1},
                           headers=ok_headers).status_code == 200
