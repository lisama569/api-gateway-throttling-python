"""组合配额：调用方 × 接口版本隔离 + 调用方总量上限。

每个 (调用方, 版本) 组合独立计费，互不挤占；同一调用方还受一个
总量窗口约束。组合额度同时包含短时间突发窗口与持续平均窗口，
两层都有余量才放行。所有窗口在同一把锁内推进/计数，并发下不超发，
被拒维度不计费（且被拒请求不占用任何其他维度的额度）。
窗口结束自动恢复，无需重启或人工干预。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

BURST = "burst"
SUSTAINED = "sustained"
CALLER_TOTAL = "caller_total"


@dataclass(frozen=True)
class WindowSpec:
    """一个窗口口径：容量与窗口长度（秒）。"""

    capacity: int
    window_seconds: float


@dataclass(frozen=True)
class ComboQuota:
    """(调用方, 版本) 组合的两层窗口配置。

    caller 为 None 时表示该版本的兜底配置（按版本生效，不限调用方）。
    """

    caller: str | None
    version: str
    burst: WindowSpec
    sustained: WindowSpec


@dataclass(frozen=True)
class DimensionState:
    """单个窗口维度的当前状态（用于响应头/日志/测试断言）。"""

    capacity: int
    remaining: int
    window_seconds: float
    reset_in_seconds: float


@dataclass(frozen=True)
class QuotaDecision:
    """组合配额判定结果。

    reason 为 None 表示放行；拒绝时为首先需要等待的维度，
    retry_after_seconds 取所有拒绝维度中最长的恢复等待时间——
    调用方等到该时刻后重试才可能成功。
    """

    allowed: bool
    reason: str | None  # None | "burst" | "sustained" | "caller_total"
    retry_after_seconds: float
    dimensions: dict[str, DimensionState]


@dataclass
class _Window:
    start: float = 0.0
    count: int = 0


@dataclass(frozen=True)
class _Dim:
    name: str
    spec: WindowSpec


class QuotaManager:
    """组合配额管理器。

    判定维度（存在才参与）：
      - burst      : (caller, version) 突发窗口
      - sustained  : (caller, version) 持续平均窗口
      - caller_total : caller 跨所有版本的总量窗口

    维度键只与 (caller, version) 有关，与入口路径无关，因此同一组合
    经过不同入口时按同一份额度判定。
    """

    def __init__(
        self,
        combo_quotas: tuple[ComboQuota, ...],
        caller_total: WindowSpec | None = None,
    ) -> None:
        self._combo_quotas = combo_quotas
        self._caller_total = caller_total
        self._lock = threading.Lock()
        self._windows: dict[tuple, _Window] = {}

    def _resolve_combo(self, caller: str, version: str) -> ComboQuota | None:
        generic: ComboQuota | None = None
        for q in self._combo_quotas:
            if q.version != version:
                continue
            if q.caller == caller:
                return q
            if q.caller is None:
                generic = q
        return generic

    def _state_at(self, key: tuple, spec: WindowSpec, now: float) -> _Window:
        """取窗口在 now 时刻的有效状态；窗口结束则滚动到新窗口（额度恢复）。"""
        win = self._windows.get(key)
        if win is None:
            win = _Window(start=now, count=0)
            self._windows[key] = win
        elif now - win.start >= spec.window_seconds:
            win.start = now
            win.count = 0
        return win

    def check(self, caller: str, version: str, now: float | None = None) -> QuotaDecision:
        """原子判定 (caller, version) 是否放行。

        先在锁内读取全部维度状态并判定，全部有余量才统一计数 +1；
        任一维度拒绝则不递增任何维度，拒绝不扣额度。
        """
        if now is None:
            now = time.monotonic()

        dims: list[_Dim] = []
        combo = self._resolve_combo(caller, version)
        if combo is not None:
            dims.append(_Dim(BURST, combo.burst))
            dims.append(_Dim(SUSTAINED, combo.sustained))
        if self._caller_total is not None:
            dims.append(_Dim(CALLER_TOTAL, self._caller_total))

        with self._lock:
            states: dict[str, DimensionState] = {}
            blocked: list[tuple[str, _Window, WindowSpec]] = []
            resolved: list[tuple[str, _Window, WindowSpec]] = []

            for dim in dims:
                key = self._dim_key(dim.name, caller, version)
                win = self._state_at(key, dim.spec, now)
                remaining = dim.spec.capacity - win.count
                reset_in = max(0.0, win.start + dim.spec.window_seconds - now)
                states[dim.name] = DimensionState(
                    capacity=dim.spec.capacity,
                    remaining=max(0, remaining),
                    window_seconds=dim.spec.window_seconds,
                    reset_in_seconds=round(reset_in, 3),
                )
                resolved.append((dim.name, win, dim.spec))
                if remaining <= 0:
                    blocked.append((dim.name, win, dim.spec))

            if blocked:
                # 等所有拒绝维度都恢复后，重试才可能成功；reason 取绑定最久的维度
                binding = max(blocked, key=lambda b: states[b[0]].reset_in_seconds)
                return QuotaDecision(
                    allowed=False,
                    reason=binding[0],
                    retry_after_seconds=states[binding[0]].reset_in_seconds,
                    dimensions=states,
                )

            # 全部通过：一次性占用每个维度各 1 个名额
            for name, win, _spec in resolved:
                win.count += 1
                state = states[name]
                states[name] = DimensionState(
                    capacity=state.capacity,
                    remaining=state.remaining - 1,
                    window_seconds=state.window_seconds,
                    reset_in_seconds=state.reset_in_seconds,
                )

            return QuotaDecision(
                allowed=True,
                reason=None,
                retry_after_seconds=0.0,
                dimensions=states,
            )

    @staticmethod
    def _dim_key(name: str, caller: str, version: str) -> tuple:
        if name == CALLER_TOTAL:
            return (CALLER_TOTAL, caller)
        return (name, caller, version)
