"""异常归类场景：业务拒绝 / 依赖失败 / 未预期异常，归一化且可区分。"""

import pytest


async def test_business_rejection_normalized(client, gateway_logs):
    resp = await client.post(
        "/api/orders",
        json={"item": "frozen", "qty": 1},
        headers={"X-Request-Id": "err-biz-1"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["category"] == "business_rejected"
    assert error["request_id"] == "err-biz-1"
    assert error["details"]["reason"] == "item_not_available"
    assert resp.headers["x-request-id"] == "err-biz-1"
    assert any("err-biz-1" in m and "gateway_error" in m
               for m in (r.getMessage() for r in gateway_logs.records))


async def test_dependency_failure_normalized_without_internals(client, gateway_logs):
    resp = await client.get(
        "/api/remotes/broken", headers={"X-Request-Id": "err-dep-1"}
    )
    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["category"] == "dependency_failure"
    assert error["message"] == "dependency unavailable"
    assert error["details"] == {"dependency": "remote", "name": "broken"}
    # 内部连接细节与敏感字段不得透出。
    raw = resp.text
    assert "s3cr3t" not in raw
    assert "connection refused" not in raw
    assert any("err-dep-1" in m and "gateway_error" in m
               for m in (r.getMessage() for r in gateway_logs.records))


async def test_unexpected_exception_is_generic_internal_error(client, gateway_logs):
    resp = await client.get(
        "/internal/boom", headers={"X-Request-Id": "err-boom-1"}
    )
    assert resp.status_code == 500
    error = resp.json()["error"]
    assert error["category"] == "internal_error"
    assert error["message"] == "internal server error"
    # 异常类型、堆栈、异常文本中的敏感值均不得出现在响应中。
    assert "RuntimeError" not in resp.text
    assert "explosion" not in resp.text
    assert "s3cr3t" not in resp.text
    assert error["request_id"] == "err-boom-1"
    assert any("err-boom-1" in m and "unexpected_exception" in m
               for m in (r.getMessage() for r in gateway_logs.records))


async def test_categories_are_distinguishable(client):
    r1 = await client.post("/api/orders", json={"item": "frozen", "qty": 1})
    r2 = await client.get("/api/remotes/broken")
    r3 = await client.get("/internal/boom")
    categories = {
        r.json()["error"]["category"] for r in (r1, r2, r3)
    }
    assert categories == {
        "business_rejected",
        "dependency_failure",
        "internal_error",
    }


async def test_same_cause_same_verdict_across_entries(client):
    """同一依赖失败原因在 /api 与 /internal 两个入口下结论一致。"""

    bodies = []
    for entry in ("/api/remotes/broken", "/internal/remotes/broken"):
        resp = await client.get(entry, headers={"X-Request-Id": "err-cross"})
        assert resp.status_code == 503
        body = resp.json()["error"]
        # 请求标识随请求不同，但分类与结论必须一致。
        bodies.append((body["category"], body["message"], body["details"]))
    assert bodies[0] == bodies[1]


async def test_validation_error_normalized(client):
    resp = await client.post("/api/orders", json={"item": "x", "qty": 0})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["category"] == "bad_request"
    assert "qty" in error["message"]
    assert "request_id" in error


async def test_unknown_route_normalized(client):
    resp = await client.get("/api/does-not-exist")
    assert resp.status_code == 404
    error = resp.json()["error"]
    assert error["category"] == "bad_request"
    assert error["request_id"]
