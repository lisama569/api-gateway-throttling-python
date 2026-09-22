"""限流场景：精确额度、并发不超发、窗口自动恢复、调用方隔离、重试信息。"""

import asyncio

import pytest

from gateway.ratelimit import RateLimiter


async def test_quota_is_exact_and_retry_after_present():
    limiter = RateLimiter(limit=3, window_seconds=10.0)
    results = [await limiter.acquire("c1", now=100.0) for _ in range(5)]
    assert [r.allowed for r in results] == [True, True, True, False, False]
    assert [r.remaining for r in results] == [2, 1, 0, 0, 0]
    denied = results[3]
    assert denied.retry_after == pytest.approx(10.0)
    assert denied.reset_in == pytest.approx(10.0)


async def test_quota_recovers_after_window():
    limiter = RateLimiter(limit=2, window_seconds=10.0)
    await limiter.acquire("c1", now=0.0)
    await limiter.acquire("c1", now=0.0)
    denied = await limiter.acquire("c1", now=5.0)
    assert denied.allowed is False
    # 窗口结束（边界之后）额度自动恢复为满额。
    recovered = await limiter.acquire("c1", now=10.0)
    assert recovered.allowed is True
    assert recovered.remaining == 1


async def test_callers_are_isolated():
    limiter = RateLimiter(limit=1, window_seconds=10.0)
    first = await limiter.acquire("a", now=0.0)
    second = await limiter.acquire("b", now=0.0)
    assert first.allowed and second.allowed
    assert (await limiter.acquire("a", now=0.0)).allowed is False
    assert (await limiter.acquire("b", now=0.0)).allowed is False


async def test_concurrent_requests_do_not_oversell():
    """并发 N 个请求，放行数必须恰好等于额度，绝不超发。"""

    limiter = RateLimiter(limit=5, window_seconds=60.0)
    results = await asyncio.gather(*[limiter.acquire("hot") for _ in range(50)])
    allowed = sum(r.allowed for r in results)
    assert allowed == 5
    denied = [r for r in results if not r.allowed]
    assert len(denied) == 45
    assert all(r.retry_after > 0 for r in denied)


async def test_http_concurrent_requests_do_not_oversell(tight_client, gateway_logs):
    """HTTP 层并发：3 额度 + 30 并发，恰好 3 个 200、27 个 429。"""

    async def one(i: int):
        return await tight_client.get(
            "/api/echo",
            headers={"X-Caller-Id": "hot", "X-Request-Id": f"rl-{i:02d}"},
        )

    responses = await asyncio.gather(*[one(i) for i in range(30)])
    statuses = sorted(r.status_code for r in responses)
    assert statuses.count(200) == 3
    assert statuses.count(429) == 27

    denied = [r for r in responses if r.status_code == 429]
    for resp in denied:
        body = resp.json()["error"]
        assert body["category"] == "rate_limited"
        assert body["details"]["retry_after"] > 0
        assert "retry_after" in body["details"]
        # 可操作的重试信息：标准头与结构化字段同时提供。
        assert resp.headers["Retry-After"].isdigit()
        assert int(resp.headers["Retry-After"]) >= 1
        assert resp.headers["X-RateLimit-Remaining"] == "0"

    # 被拒绝与放行的请求在日志中都应能按请求标识检索到判定依据。
    messages = [r.getMessage() for r in gateway_logs.records]
    assert sum("reason=rate_allowed" in m for m in messages) == 3
    assert sum("reason=rate_limited" in m for m in messages) == 27
    denied_ids = {r.json()["error"]["request_id"] for r in denied}
    assert all(
        any(rid in m and "rate_limited" in m for m in messages)
        for rid in denied_ids
    )


async def test_http_quota_recovers_for_new_window(tight_app):
    """恢复窗口结束后额度自动恢复（通过限流器时钟验证，窗口边界后放行）。"""

    limiter = tight_app.state.limiter
    for _ in range(3):
        r = await limiter.acquire("c", now=0.0)
        assert r.allowed
    third = await limiter.acquire("c", now=0.0)
    assert third.allowed is False
    recovered = await limiter.acquire("c", now=60.0)
    assert recovered.allowed is True
    assert recovered.remaining == 2
