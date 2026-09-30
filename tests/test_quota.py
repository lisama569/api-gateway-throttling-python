"""组合配额测试：组合隔离、两层窗口精确判定、拒绝不扣额度、
窗口自动恢复、调用方总量并发不超发。"""

from __future__ import annotations

import threading

import pytest

from app.quota import (
    BURST,
    CALLER_TOTAL,
    SUSTAINED,
    ComboQuota,
    QuotaManager,
    WindowSpec,
)

BURST_SPEC = WindowSpec(capacity=3, window_seconds=1.0)
SUSTAINED_SPEC = WindowSpec(capacity=5, window_seconds=10.0)
TOTAL_SPEC = WindowSpec(capacity=4, window_seconds=60.0)


def make_manager(total: WindowSpec | None = TOTAL_SPEC) -> QuotaManager:
    return QuotaManager(
        combo_quotas=(
            ComboQuota(caller=None, version="v1", burst=BURST_SPEC, sustained=SUSTAINED_SPEC),
            ComboQuota(caller=None, version="v2", burst=BURST_SPEC, sustained=SUSTAINED_SPEC),
        ),
        caller_total=total,
    )


# ---------- 组合配额隔离 ----------

def test_combo_quota_isolated_by_version():
    """同一调用方不同版本各用各的额度，互不挤占。"""
    qm = make_manager(total=None)
    # 耗尽 caller-a 的 v1 突发额度
    for _ in range(3):
        assert qm.check("caller-a", "v1", now=0.0).allowed
    denied_v1 = qm.check("caller-a", "v1", now=0.1)
    assert not denied_v1.allowed
    # v2 额度完全不受影响
    ok_v2 = qm.check("caller-a", "v2", now=0.1)
    assert ok_v2.allowed
    assert ok_v2.dimensions[BURST].remaining == 2


def test_combo_quota_isolated_by_caller():
    """不同调用方同版本也各自独立。"""
    qm = make_manager(total=None)
    for _ in range(3):
        qm.check("caller-a", "v1", now=0.0)
    assert not qm.check("caller-a", "v1", now=0.1).allowed
    assert qm.check("caller-b", "v1", now=0.1).allowed


# ---------- 突发与持续两层判定 ----------

def test_burst_window_blocks_before_sustained():
    qm = make_manager(total=None)
    for t in (0.0, 0.1, 0.2):
        assert qm.check("c", "v1", now=t).allowed
    d = qm.check("c", "v1", now=0.3)
    assert not d.allowed
    assert d.reason == BURST
    # 突发窗口 1s 后恢复；第 4、5 次请求继续占用持续额度
    assert qm.check("c", "v1", now=1.0).allowed
    assert qm.check("c", "v1", now=1.1).allowed
    # 持续窗口 5 次已满（0.0~0.2 三次 + 1.0/1.1 两次）
    blocked = qm.check("c", "v1", now=1.2)
    assert not blocked.allowed
    assert blocked.reason == SUSTAINED
    assert blocked.retry_after_seconds == pytest.approx(8.8)


def test_sustained_window_blocks_even_if_burst_recovered():
    """突发窗口已恢复，但持续窗口仍满时依旧拒绝。"""
    qm = QuotaManager(
        combo_quotas=(ComboQuota(
            caller=None, version="v1",
            burst=WindowSpec(capacity=10, window_seconds=1.0),
            sustained=WindowSpec(capacity=2, window_seconds=10.0),
        ),),
        caller_total=None,
    )
    assert qm.check("c", "v1", now=0.0).allowed
    assert qm.check("c", "v1", now=0.5).allowed
    d = qm.check("c", "v1", now=5.0)  # 突发早已恢复，持续已满
    assert not d.allowed
    assert d.reason == SUSTAINED
    # 持续窗口结束后两层都恢复
    assert qm.check("c", "v1", now=10.0).allowed


def test_retry_after_waits_for_all_blocked_dimensions():
    """突发与总量同时满时，retry_after 取最久维度，指导有效重试时机。"""
    qm = QuotaManager(
        combo_quotas=(ComboQuota(
            caller=None, version="v1",
            burst=WindowSpec(capacity=4, window_seconds=1.0),
            sustained=WindowSpec(capacity=10, window_seconds=10.0),
        ),),
        caller_total=WindowSpec(capacity=4, window_seconds=60.0),
    )
    for t in (0.0, 0.1, 0.2, 0.3):
        assert qm.check("c", "v1", now=t).allowed  # 同时耗尽 burst(4) 与 total(4)
    d = qm.check("c", "v1", now=0.4)
    assert not d.allowed
    # burst 0.6s 后恢复，但总量要 59.6s；必须等到总量恢复才可能成功
    assert d.dimensions[BURST].reset_in_seconds == pytest.approx(0.6)
    assert d.dimensions[CALLER_TOTAL].reset_in_seconds == pytest.approx(59.6)
    assert d.reason == CALLER_TOTAL
    assert d.retry_after_seconds == pytest.approx(59.6)
    # 只等突发恢复仍被总量拒绝
    assert not qm.check("c", "v1", now=1.0).allowed


# ---------- 拒绝不扣额度 ----------

def test_rejection_consumes_nothing():
    """被任一维度拒绝时，不占用任何维度的额度。"""
    qm = make_manager()
    for t in (0.0, 0.1, 0.2):
        qm.check("c", "v1", now=t)
    before = qm.check("c", "v1", now=0.3)
    assert not before.allowed
    # 再来一批被拒请求，各维度余量不应变化
    for t in (0.4, 0.5, 0.6):
        again = qm.check("c", "v1", now=t)
        assert not again.allowed
        assert again.dimensions[BURST].remaining == 0
        assert again.dimensions[SUSTAINED].remaining == 2  # 5 - 3
        assert again.dimensions[CALLER_TOTAL].remaining == 1  # 4 - 3


# ---------- 窗口自动恢复 ----------

def test_windows_recover_without_intervention():
    qm = make_manager()
    for t in (0.0, 0.1, 0.2):
        qm.check("c", "v1", now=t)
    assert not qm.check("c", "v1", now=0.3).allowed
    # 所有窗口结束后自动恢复，无需重启
    recovered = qm.check("c", "v1", now=60.0)
    assert recovered.allowed
    assert recovered.dimensions[BURST].remaining == 2
    assert recovered.dimensions[SUSTAINED].remaining == 4
    assert recovered.dimensions[CALLER_TOTAL].remaining == 3


# ---------- 调用方总量：并发不超发 ----------

def test_caller_total_never_oversells_under_concurrency():
    """组合都没满、但总量到顶时拒绝；并发下实际放行数精确等于总量容量。"""
    qm = QuotaManager(combo_quotas=(), caller_total=WindowSpec(capacity=5, window_seconds=60.0))
    results: list[bool] = []
    lock = threading.Lock()

    def hit(version: str) -> None:
        d = qm.check("busy-caller", version)
        with lock:
            results.append(d.allowed)

    threads = [
        threading.Thread(target=hit, args=("v1" if i % 2 == 0 else "v2",))
        for i in range(100)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sum(results) == 5  # 精确等于总量容量，不多不少
