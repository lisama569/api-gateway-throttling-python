"""统一对外能力层测试：版本拒绝、并发超发、异常归类。"""

from __future__ import annotations

import logging
import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from app.errors import (
    BusinessRejection,
    DependencyFailure,
    ErrorCategory,
    normalize_error,
)
from app.ratelimit import RateLimiter
from app.versioning import UnsupportedVersionError, negotiate_version
from main import app

logger = logging.getLogger("test.gateway")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


def fresh_headers(caller: str) -> dict[str, str]:
    """每个用例使用独立 caller，避免共享限流额度互相干扰。"""
    request_id = uuid.uuid4().hex[:12]
    h = {"X-Caller-Id": caller, "X-Request-Id": request_id}
    logger.info("request_id=%s caller=%s 判定依据: 独立 caller 隔离限流桶", request_id, caller)
    return h


# ---------- 版本协商 ----------

def test_default_version_when_header_absent(client: TestClient):
    h = fresh_headers("ver-default")
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 200
    assert r.json()["version"] == "v1"
    logger.info("request_id=%s 判定依据: 未带版本头 -> 默认版本 v1", h["X-Request-Id"])


def test_supported_version_accepted(client: TestClient):
    h = {**fresh_headers("ver-v2"), "X-Api-Version": "v2"}
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 200
    assert r.json()["version"] == "v2"


def test_unsupported_version_rejected_consistently(client: TestClient):
    h = {**fresh_headers("ver-reject"), "X-Api-Version": "v9"}
    r1 = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    r2 = client.get("/orders/preview", headers=h)
    for r in (r1, r2):
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "UNSUPPORTED_VERSION"
        assert err["category"] == "unsupported_version"
        assert err["supported_versions"] == ["v1", "v2"]
    logger.info(
        "request_id=%s 判定依据: v9 不在支持集合, 两个入口均拒绝且结构一致",
        h["X-Request-Id"],
    )


def test_negotiate_version_unit():
    assert negotiate_version(None).version == "v1"
    assert negotiate_version("").source == "default"
    assert negotiate_version(" V2 ").version == "v2"
    with pytest.raises(UnsupportedVersionError):
        negotiate_version("v9")


# ---------- 限流：并发不超发、额度精确、窗口恢复 ----------

def test_concurrent_requests_never_oversell():
    limit, threads_n = 20, 100
    rl = RateLimiter(limit=limit, window_seconds=60.0)
    results: list[bool] = []
    lock = threading.Lock()

    def hit():
        d = rl.check("same-caller")
        with lock:
            results.append(d.allowed)

    threads = [threading.Thread(target=hit) for _ in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(results)
    logger.info(
        "判定依据: limit=%d 并发=%d 放行=%d, 额度不得超发", limit, threads_n, allowed
    )
    assert allowed == limit  # 精确等于额度，不多不少


def test_quota_precise_and_window_recovers():
    rl = RateLimiter(limit=3, window_seconds=10.0)
    assert [rl.check("c", now=t).allowed for t in (0.0, 1.0, 2.0)] == [True, True, True]
    denied = rl.check("c", now=3.0)
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.retry_after_seconds == pytest.approx(7.0)
    # 窗口结束后额度自动恢复
    recovered = rl.check("c", now=10.0)
    assert recovered.allowed and recovered.remaining == 2
    # 不同 caller 互不影响
    assert rl.check("other", now=3.0).allowed
    logger.info("判定依据: 固定窗口 limit=3, 第4次拒绝 retry_after=7s, 窗口结束自动恢复")


def test_rate_limited_response_has_retry_info(client: TestClient):
    h = fresh_headers("rl-caller")
    codes = [
        client.post("/orders", json={"item": "book", "quantity": 1}, headers=h).status_code
        for _ in range(8)
    ]
    assert codes.count(200) == 5  # 应用级 limit=5
    assert codes.count(429) == 3
    r = client.post("/orders", json={"item": "book", "quantity": 1}, headers=h)
    assert r.status_code == 429
    err = r.json()["error"]
    assert err["category"] == "rate_limited"
    assert err["retry_after_seconds"] > 0
    assert "Retry-After" in r.headers
    logger.info(
        "request_id=%s 判定依据: 限额 5, 超限返回 429 + Retry-After=%s",
        h["X-Request-Id"], r.headers["Retry-After"],
    )


# ---------- 错误归一化 ----------

def test_error_categories_are_distinguishable(client: TestClient):
    h = fresh_headers("err-caller")
    cases = [
        ({"item": "book", "quantity": 0}, 422, "business_rejection"),
        ({"item": "fragile", "quantity": 1}, 502, "dependency_failure"),
        ({"item": "boom", "quantity": 1}, 500, "internal_error"),
    ]
    seen = set()
    for payload, status, category in cases:
        r = client.post("/orders", json=payload, headers=h)
        assert r.status_code == status, r.text
        err = r.json()["error"]
        assert err["category"] == category
        assert err["request_id"] == h["X-Request-Id"]
        seen.add(category)
        logger.info(
            "request_id=%s 判定依据: item=%s -> %s/%s",
            h["X-Request-Id"], payload["item"], status, category,
        )
    assert seen == {"business_rejection", "dependency_failure", "internal_error"}


def test_internal_details_not_leaked(client: TestClient):
    h = fresh_headers("err-leak")
    r = client.post("/orders", json={"item": "boom", "quantity": 1}, headers=h)
    assert r.status_code == 500
    assert "pricing engine" not in r.text  # 内部异常细节不透出
    assert "Traceback" not in r.text


def test_same_cause_same_conclusion_across_entries(client: TestClient):
    h = fresh_headers("err-consistent")
    r1 = client.post("/orders", json={"item": "fragile", "quantity": 1}, headers=h)
    r2 = client.get("/orders/preview", params={"item": "fragile"}, headers=h)
    e1, e2 = r1.json()["error"], r2.json()["error"]
    assert r1.status_code == r2.status_code == 502
    assert e1["category"] == e2["category"] == "dependency_failure"
    assert e1["code"] == e2["code"]
    logger.info(
        "request_id=%s 判定依据: 同一依赖失败在两个入口得到相同分类",
        h["X-Request-Id"],
    )


def test_normalize_error_unit():
    b = normalize_error(BusinessRejection("bad qty"), "r1")
    assert b.category is ErrorCategory.BUSINESS_REJECTION
    d = normalize_error(DependencyFailure("db at 10.0.0.1 down"), "r2")
    assert d.category is ErrorCategory.DEPENDENCY_FAILURE
    assert "10.0.0.1" not in d.message  # 敏感字段不透出
    u = normalize_error(ValueError("secret-token-xyz"), "r3")
    assert u.category is ErrorCategory.INTERNAL_ERROR
    assert "secret-token-xyz" not in u.message
