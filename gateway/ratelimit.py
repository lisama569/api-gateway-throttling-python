"""按调用方身份的固定窗口限流器。

并发安全：``检查额度 -> 计数`` 的临界区在同一把 ``asyncio.Lock`` 内完成，
同一事件循环下并发请求不会超发额度。

自动恢复：窗口结束后的下一次请求会开启新窗口，额度自动恢复为满额。

时钟：默认使用 ``time.monotonic``；可通过 ``now`` 注入测试时钟。
"""

import asyncio
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class RateLimitResult:
    """一次限流判定结果。

    allowed: 是否放行；
    limit/remaining: 窗口总额度 / 本次判定后剩余额度；
    retry_after: 距窗口重置的秒数（放行时为 0.0）；
    reset_in: 无论放行与否，距当前窗口重置的秒数。
    """

    allowed: bool
    limit: int
    remaining: int
    retry_after: float
    reset_in: float


class RateLimiter:
    def __init__(self, limit: int, window_seconds: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window = float(window_seconds)
        self._buckets: dict[str, tuple[float, int]] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, caller: str, *, now: float | None = None) -> RateLimitResult:
        current = time.monotonic() if now is None else now
        async with self._lock:
            self._purge_expired(current)

            start, count = self._buckets.get(caller, (current, 0))
            # 窗口已结束（含恰好到达边界）：开新窗口，额度自动恢复。
            if current >= start + self._window or caller not in self._buckets:
                start, count = current, 0

            reset_at = start + self._window
            if count < self._limit:
                count += 1
                self._buckets[caller] = (start, count)
                return RateLimitResult(
                    allowed=True,
                    limit=self._limit,
                    remaining=self._limit - count,
                    retry_after=0.0,
                    reset_in=max(0.0, reset_at - current),
                )

            self._buckets[caller] = (start, count)
            return RateLimitResult(
                allowed=False,
                limit=self._limit,
                remaining=0,
                retry_after=max(0.0, reset_at - current),
                reset_in=max(0.0, reset_at - current),
            )

    def _purge_expired(self, now: float) -> None:
        """顺手清理已过窗口的调用方，避免状态无限增长。"""

        expired = [
            caller
            for caller, (start, _) in self._buckets.items()
            if now >= start + self._window
        ]
        for caller in expired:
            self._buckets.pop(caller, None)

    def reset(self) -> None:
        """清空全部计数（主要供测试使用）。"""

        self._buckets.clear()
