"""默认组合配额配置：调用方 × 版本的两层窗口 + 调用方总量上限。

修改本文件即可调整配额；需要按调用方单独给额时追加 ComboQuota
（指定 caller 的配置优先于同版本的兜底配置，caller=None 为版本兜底）。

叠加规则：
  burst + sustained 属于 (caller, version) 组合维度，两层都有余量才放行；
  caller_total 属于调用方维度，跨该调用方所有版本统一计数。
  任一维度拒绝，整次请求拒绝，且不占用任何维度的额度。
"""

from __future__ import annotations

from .quota import ComboQuota, QuotaManager, WindowSpec

# 组合配额：caller=None 表示版本兜底，所有调用方共享该默认配置，
# 但实际记账仍按 (caller, version) 各自独立，互不挤占。
DEFAULT_COMBO_QUOTAS: tuple[ComboQuota, ...] = (
    # 短时间突发：5 次/60 秒（与旧版按调用方 5/60s 的口径兼容）
    # 持续平均：20 次/300 秒
    ComboQuota(caller=None, version="v1",
               burst=WindowSpec(capacity=5, window_seconds=60.0),
               sustained=WindowSpec(capacity=20, window_seconds=300.0)),
    ComboQuota(caller=None, version="v2",
               burst=WindowSpec(capacity=5, window_seconds=60.0),
               sustained=WindowSpec(capacity=20, window_seconds=300.0)),
)

# 调用方总量上限：跨版本合计 10 次/60 秒。
DEFAULT_CALLER_TOTAL = WindowSpec(capacity=10, window_seconds=60.0)


def build_default_quota_manager() -> QuotaManager:
    """构造使用默认配置的配额管理器。"""
    return QuotaManager(
        combo_quotas=DEFAULT_COMBO_QUOTAS,
        caller_total=DEFAULT_CALLER_TOTAL,
    )
