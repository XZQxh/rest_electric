"""告警判定。纯函数，方便测试：输入余额与估算结果，输出需要推送的告警列表。"""

from __future__ import annotations

from dataclasses import dataclass

from .forecast import Estimate

LEVEL_LOW_BALANCE = "low_balance"
LEVEL_PREDICTED_LOW = "predicted_low"
LEVEL_AUTH_EXPIRED = "auth_expired"
LEVEL_FETCH_FAILED = "fetch_failed"

# 各类告警的冷却天数：失败类每天最多一次；登录失效每天提醒一次（token 只有 2 小时）
COOLDOWNS = {
    LEVEL_LOW_BALANCE: 1,
    LEVEL_PREDICTED_LOW: 1,
    LEVEL_AUTH_EXPIRED: 1,
    LEVEL_FETCH_FAILED: 1,
}


@dataclass(frozen=True)
class AlertDecision:
    level: str
    reason: str


def decide(
    balance_cents: int,
    estimate: Estimate,
    floor_cents: int,
    days_warn: float,
) -> list[AlertDecision]:
    """按「固定阈值 + 预测欠费天数」两条规则判定，两者可同时触发。"""
    decisions: list[AlertDecision] = []

    if balance_cents < floor_cents:
        decisions.append(
            AlertDecision(
                level=LEVEL_LOW_BALANCE,
                reason=f"当前余额低于设定阈值 {floor_cents / 100:.2f} 元",
            )
        )

    if estimate.days_left is not None and estimate.days_left < days_warn:
        decisions.append(
            AlertDecision(
                level=LEVEL_PREDICTED_LOW,
                reason=f"按当前用电速度预计 {estimate.days_left:.1f} 天内欠费",
            )
        )

    return decisions
