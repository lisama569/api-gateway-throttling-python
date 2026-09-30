"""组合配额限流：调用方 × 接口版本分桶 + 调用方总容量上限，两层窗口判定。

判定口径（三者全部通过才放行，任一拒绝都不扣任何一层的额度）：

1. 组合突发窗口：(caller, version) 在 burst.window_seconds 内不超过 burst.capacity；
2. 组合持续窗口：(caller, version) 在 sustained.window_seconds 内不超过 sustained.capacity；
3. 调用方总量：caller 在 caller_total_window_seconds 内、跨所有版本合计不超过
   config.caller_total（总量窗口取持续窗口长度，保证总量随持续窗口一起恢复）。

所有读取-判定-计数在同一把锁内完成，并发下放行数精确等于配置容量；
窗口结束后额度自动恢复，无需重启或人工干预。
retry_after_seconds 取所有被拒维度中"距离最近一次额度恢复"的最大值，
调用方等到该时刻后重试才可能成功。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowSpec:
    """一层窗口的口径：window_seconds 秒内最多 capacity 次。"""

    name: str
    capacity: int
    window_seconds: float


@dataclass(frozen=True)
class QuotaConfig:
    """配额配置：每个 (调用方, 版本) 组合的两层窗口 + 调用方总容量。

    调用方总量窗口与持续窗口等长：总量上限约束的是一个持续周期内该调用方
    跨版本的请求总量，恢复节奏也与持续窗口一致。
    """

    burst: WindowSpec
    sustained: WindowSpec
    caller_total: int

    @property
    def caller_total_window_seconds(self) -> float:
        return self.sustained.window_seconds


@dataclass(frozen=True)
class QuotaDecision:
    """组合配额判定结果。

    - allowed：是否放行；
    - rejected_by：被哪些维度拒绝（"combo_burst" / "combo_sustained" / "caller_total"）；
    - remaining：放行后剩余的最小可用额度（取三层口径最小值）；拒绝时为 0；
    - retry_after_seconds：距离最近可能放行的等待秒数（所有被拒维度的最大值）。
    """

    allowed: bool
    rejected_by: tuple[str, ...]
    remaining: int
    retry_after_seconds: float


@dataclass
class _Counter:
    """单个固定窗口计数。惰性推进：每次访问时若窗口已结束则重置。"""

    window_seconds: float
    start: float | None = None
    count: int = 0

    def advance(self, now: float) -> None:
        # 窗口区间为 [start, start + window)，右边界仍属于旧窗口，故用严格大于
        if self.start is None or now - self.start > self.window_seconds:
            self.start = now
            self.count = 0

    def retry_after(self, now: float) -> float:
        if self.start is None:
            return 0.0
        return max(0.0, self.start + self.window_seconds - now)


@dataclass
class _ComboState:
    """一个 (caller, version) 组合的突发/持续两层计数。"""

    burst: _Counter
    sustained: _Counter


class QuotaLimiter:
    """组合配额限流器。

    分桶 key：
    - 组合桶：(caller, version)，同一调用方同一版本在任何入口下命中同一份额度；
    - 总量桶：caller，跨版本合计。
    """

    def __init__(self, config: QuotaConfig) -> None:
        self._config = config
        self._lock = threading.Lock()
        self._combos: dict[tuple[str, str], _ComboState] = {}
        self._caller_totals: dict[str, _Counter] = {}

    def _combo_state(self, key: tuple[str, str]) -> _ComboState:
        state = self._combos.get(key)
        if state is None:
            state = _ComboState(
                burst=_Counter(self._config.burst.window_seconds),
                sustained=_Counter(self._config.sustained.window_seconds),
            )
            self._combos[key] = state
        return state

    def _caller_total(self, caller: str) -> _Counter:
        counter = self._caller_totals.get(caller)
        if counter is None:
            counter = _Counter(self._config.caller_total_window_seconds)
            self._caller_totals[caller] = counter
        return counter

    def check(self, caller: str, version: str, now: float | None = None) -> QuotaDecision:
        """判定一次请求；只有三层口径全部有余量时才同时在三层各计一次。

        任一层拒绝都直接返回，不修改任何计数器，因此被拒请求不会占用
        其他维度的额度。
        """
        if now is None:
            now = time.monotonic()
        with self._lock:
            state = self._combo_state((caller, version))
            state.burst.advance(now)
            state.sustained.advance(now)
            total = self._caller_total(caller)
            total.advance(now)

            rejected: list[str] = []
            if state.burst.count >= self._config.burst.capacity:
                rejected.append("combo_burst")
            if state.sustained.count >= self._config.sustained.capacity:
                rejected.append("combo_sustained")
            if total.count >= self._config.caller_total:
                rejected.append("caller_total")

            if rejected:
                retry_after = max(
                    {
                        "combo_burst": state.burst.retry_after(now),
                        "combo_sustained": state.sustained.retry_after(now),
                        "caller_total": total.retry_after(now),
                    }[name]
                    for name in rejected
                )
                return QuotaDecision(
                    allowed=False,
                    rejected_by=tuple(rejected),
                    remaining=0,
                    retry_after_seconds=round(retry_after, 3),
                )

            state.burst.count += 1
            state.sustained.count += 1
            total.count += 1
            remaining = min(
                self._config.burst.capacity - state.burst.count,
                self._config.sustained.capacity - state.sustained.count,
                self._config.caller_total - total.count,
            )
            return QuotaDecision(
                allowed=True,
                rejected_by=(),
                remaining=remaining,
                retry_after_seconds=0.0,
            )
