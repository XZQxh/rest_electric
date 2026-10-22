"""用电量与剩余天数估算。

规则（详见 docs/data-model.md）：
- 相邻两天余额下降 = 用电；余额上升 = 充值/退费，整段丢弃，绝不当成负消耗；
- 日均取样本中位数，抗单日异常；
- 样本不足或日均为 0 时不给预测，宁可不说也不给错的数。
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import date
from typing import Sequence


@dataclass(frozen=True)
class Estimate:
    daily_cents: float | None
    days_left: float | None
    samples: int
    reason: str

    @property
    def has_prediction(self) -> bool:
        return self.daily_cents is not None and self.days_left is not None


def daily_samples(points: Sequence[tuple[date, int]]) -> list[float]:
    """把「按天最后读数」序列转成每日用电量样本（分/天）。"""
    samples: list[float] = []
    for (day0, balance0), (day1, balance1) in zip(points, points[1:]):
        gap = (day1 - day0).days
        if gap <= 0:
            continue  # 同一天重复，忽略
        delta = balance0 - balance1
        if delta < 0:
            continue  # 余额上升：充值或退费，不计入用电
        samples.append(delta / gap)
    return samples


def estimate(
    points: Sequence[tuple[date, int]],
    last_balance_cents: int | None = None,
) -> Estimate:
    points = list(points)
    samples = daily_samples(points)
    if not samples:
        return Estimate(
            daily_cents=None,
            days_left=None,
            samples=0,
            reason="数据不足：至少需要两天的读数才能计算日均用电",
        )

    daily = statistics.median(samples)
    if last_balance_cents is None:
        last_balance_cents = points[-1][1]

    if daily <= 0:
        return Estimate(
            daily_cents=0.0,
            days_left=None,
            samples=len(samples),
            reason="近期余额没有下降，暂无法预测欠费时间",
        )

    if last_balance_cents <= 0:
        return Estimate(daily_cents=daily, days_left=0.0, samples=len(samples), reason="余额已为零或欠费")

    return Estimate(
        daily_cents=daily,
        days_left=last_balance_cents / daily,
        samples=len(samples),
        reason=f"基于最近 {len(samples)} 个有效区间的中位日耗",
    )
