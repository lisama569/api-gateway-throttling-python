"""组合配额单测：(调用方, 版本) 隔离、总量上限、两层窗口、拒绝不扣额度、恢复。"""

from __future__ import annotations

import threading

from app.quotas import QuotaConfig, QuotaLimiter, WindowSpec


def make_config(burst: int = 3, burst_window: float = 10.0,
                sustained: int = 6, sustained_window: float = 60.0,
                caller_total: int = 10) -> QuotaConfig:
    return QuotaConfig(
        burst=WindowSpec("burst", burst, burst_window),
        sustained=WindowSpec("sustained", sustained, sustained_window),
        caller_total=caller_total,
    )


# ---------- 组合隔离 ----------

def test_same_caller_different_versions_have_independent_quotas():
    rl = QuotaLimiter(make_config(burst=2, sustained=2, caller_total=100))
    # v1 把自己的组合额度用完
    assert all(rl.check("c", "v1", now=0.0).allowed for _ in range(2))
    d = rl.check("c", "v1", now=0.0)
    assert not d.allowed and "combo_burst" in d.rejected_by
    # v2 不受影响，各用各的额度
    assert rl.check("c", "v2", now=0.0).allowed
    assert rl.check("c", "v2", now=0.0).allowed


def test_different_callers_fully_isolated():
    # 总量给到 2，纯粹验证组合桶/调用方桶的 key 隔离，不引入总量已满的干扰
    rl = QuotaLimiter(make_config(burst=1, sustained=1, caller_total=2))
    assert rl.check("a", "v1", now=0.0).allowed
    assert not rl.check("a", "v1", now=0.0).allowed
    assert rl.check("b", "v1", now=0.0).allowed
    assert rl.check("a", "v2", now=0.0).allowed  # a 的 v2 也独立于 a 的 v1


# ---------- 调用方总量上限 ----------

def test_caller_total_cap_binds_across_versions():
    # 单版本组合额度都很宽裕，但 caller 总量只有 3
    rl = QuotaLimiter(make_config(burst=10, sustained=10, caller_total=3))
    decisions = [
        rl.check("c", v, now=0.0)
        for v in ("v1", "v2", "v1", "v2", "v1")
    ]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[3].rejected_by == ("caller_total",)
    assert decisions[4].rejected_by == ("caller_total",)
    # 别的 caller 不受该 caller 总量影响
    assert rl.check("other", "v1", now=0.0).allowed


def test_caller_total_concurrent_never_oversells():
    total, threads_n = 25, 200
    rl = QuotaLimiter(make_config(burst=10_000, sustained=10_000, caller_total=total))

    def hit(results, lock, version):
        d = rl.check("hot-caller", version)
        with lock:
            results.append(d.allowed)

    results: list[bool] = []
    lock = threading.Lock()
    versions = ["v1", "v2"]
    threads = [
        threading.Thread(target=hit, args=(results, lock, versions[i % 2]))
        for i in range(threads_n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == total  # 跨版本合计精确等于总量，不超发


# ---------- 突发 + 持续两层判定 ----------

def test_burst_and_sustained_must_both_pass():
    rl = QuotaLimiter(make_config(burst=2, burst_window=10.0,
                                  sustained=4, sustained_window=60.0,
                                  caller_total=100))
    # t=0 突发窗口内 2 次后，突发先满
    assert rl.check("c", "v1", now=0.0).allowed
    assert rl.check("c", "v1", now=0.0).allowed
    d = rl.check("c", "v1", now=0.0)
    assert not d.allowed and d.rejected_by == ("combo_burst",)
    # t=10.1 突发窗口已恢复；持续仍在同一窗口累计
    assert rl.check("c", "v1", now=10.1).allowed  # sustained 3
    assert rl.check("c", "v1", now=10.1).allowed  # sustained 4
    # 新的突发窗口内第 3 次：突发与持续同时满
    d = rl.check("c", "v1", now=10.1)
    assert not d.allowed
    assert set(d.rejected_by) == {"combo_burst", "combo_sustained"}
    # 下一个突发窗口：突发恢复了，但持续仍满，单独被持续维度拒绝
    d = rl.check("c", "v1", now=20.2)
    assert not d.allowed and d.rejected_by == ("combo_sustained",)


def test_burst_rejection_does_not_consume_sustained_or_total_quota():
    # 突发容量 1，持续/总量宽裕：被突发拒后，余量保持不变
    rl = QuotaLimiter(make_config(burst=1, burst_window=10.0,
                                  sustained=5, sustained_window=60.0,
                                  caller_total=10))
    assert rl.check("c", "v1", now=0.0).allowed
    # 同一突发窗口内被拒 5 次：任何一层的额度都不应被扣
    for _ in range(5):
        assert rl.check("c", "v1", now=0.0).rejected_by == ("combo_burst",)
    # 跨 4 个后续突发窗口各放 1 次：持续额度累计 1+4=5 恰好到顶，
    # 说明之前 5 次拒绝一次都没有占用持续/总量额度
    for t in (10.1, 20.2, 30.3, 40.4):
        assert rl.check("c", "v1", now=t).allowed
    d = rl.check("c", "v1", now=40.4)
    # 此刻突发窗口也已用掉唯一名额 -> 突发、持续同时满
    assert set(d.rejected_by) == {"combo_burst", "combo_sustained"}


def test_total_rejection_does_not_consume_combo_quota():
    # 组合容量宽裕，总量只有 2；总量满后换更宽的时间点验证组合没被扣
    rl = QuotaLimiter(make_config(burst=10, burst_window=10.0,
                                  sustained=10, sustained_window=60.0,
                                  caller_total=2))
    assert rl.check("c", "v1", now=0.0).allowed
    assert rl.check("c", "v2", now=0.0).allowed
    # 总量已满，下面的拒绝不得占用组合额度
    for _ in range(3):
        d = rl.check("c", "v1", now=0.0)
        assert d.rejected_by == ("caller_total",)
    # 总量窗口恢复后：v1 组合此前只被真正计过 1 次，容量依旧宽裕
    assert rl.check("c", "v1", now=60.1).allowed
    assert rl.check("c", "v2", now=60.1).allowed


# ---------- 窗口恢复 ----------

def test_quotas_recover_automatically_after_windows():
    rl = QuotaLimiter(make_config(burst=1, burst_window=10.0,
                                  sustained=1, sustained_window=60.0,
                                  caller_total=1))
    assert rl.check("c", "v1", now=0.0).allowed
    assert not rl.check("c", "v1", now=5.0).allowed
    # 突发窗口结束但持续/总量仍在：继续拒绝
    assert not rl.check("c", "v1", now=10.1).allowed
    # 持续窗口结束，三层额度全部恢复
    d = rl.check("c", "v1", now=60.1)
    assert d.allowed and d.remaining == 0  # 每层只剩 0（容量均为 1，计后余 0）


def test_window_boundary_still_belongs_to_old_window():
    # [start, start+w) 右边界属于旧窗口，不能提前恢复
    rl = QuotaLimiter(make_config(burst=1, burst_window=10.0,
                                  sustained=10, sustained_window=60.0,
                                  caller_total=10))
    rl.check("c", "v1", now=0.0)
    assert not rl.check("c", "v1", now=10.0).allowed
    assert rl.check("c", "v1", now=10.001).allowed


# ---------- retry-after ----------

def test_retry_after_points_to_next_recovery_moment():
    rl = QuotaLimiter(make_config(burst=2, burst_window=10.0,
                                  sustained=10, sustained_window=60.0,
                                  caller_total=100))
    rl.check("c", "v1", now=0.0)
    rl.check("c", "v1", now=0.0)
    d = rl.check("c", "v1", now=0.0)
    assert not d.allowed and d.retry_after_seconds == 10.0  # 仅突发满

    # 持续窗口内持续累计到 10（跨多个突发窗口），使两层同时满
    for t in (10.1, 10.1, 20.2, 20.2, 30.3, 30.3, 40.4, 40.4):
        assert rl.check("c", "v1", now=t).allowed
    d = rl.check("c", "v1", now=40.4)
    assert set(d.rejected_by) == {"combo_burst", "combo_sustained"}
    assert d.retry_after_seconds > 10.0  # 取恢复更晚的持续窗口
