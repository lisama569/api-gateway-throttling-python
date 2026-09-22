"""按调用方身份的限流：固定窗口计数，并发安全。"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结果。"""

    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: float


@dataclass
class _Window:
    start: float = 0.0
    count: int = 0


class RateLimiter:
    """固定窗口限流器，按 caller 身份分桶，额度不超发。"""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, _Window] = {}

    def check(self, caller: str, now: float | None = None) -> RateLimitDecision:
        """判定一次请求是否放行；窗口结束后额度自动恢复。

        计数与窗口推进在同一把锁内完成，并发请求下额度不会被超发。
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(caller)
            if bucket is None or now - bucket.start >= self._window_seconds:
                bucket = _Window(start=now, count=0)
                self._buckets[caller] = bucket
            retry_after = max(0.0, bucket.start + self._window_seconds - now)
            if bucket.count >= self._limit:
                return RateLimitDecision(
                    allowed=False,
                    limit=self._limit,
                    remaining=0,
                    retry_after_seconds=round(retry_after, 3),
                )
            bucket.count += 1
            return RateLimitDecision(
                allowed=True,
                limit=self._limit,
                remaining=self._limit - bucket.count,
                retry_after_seconds=0.0,
            )
