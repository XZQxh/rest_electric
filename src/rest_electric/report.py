"""文本报表与 CSV 导出。"""

from __future__ import annotations

import csv
from datetime import date, timedelta
from pathlib import Path

from . import clock
from .config import Config
from .forecast import Estimate
from .models import AlertRecord, yuan

LEVEL_LABELS = {
    "low_balance": "余额偏低",
    "predicted_low": "预计欠费",
    "auth_expired": "登录失效",
    "fetch_failed": "查询失败",
    "stale_ingest": "采集端离线",
    "suspect_reading": "读数异常",
}


def _usage_by_day(points: list[tuple[date, int]]) -> dict[date, float | None]:
    """给每天算一个「与前一天相比」的用电量；充值日返回 None。"""
    result: dict[date, float | None] = {}
    for (day0, balance0), (day1, balance1) in zip(points, points[1:]):
        gap = (day1 - day0).days
        delta = balance0 - balance1
        if gap <= 0 or delta < 0:
            result[day1] = None
        else:
            result[day1] = delta / gap
    return result


def render(
    config: Config,
    points: list[tuple[date, int]],
    estimate: Estimate,
    alerts: list[AlertRecord] | None = None,
    days: int = 14,
) -> str:
    lines: list[str] = []
    lines.append(f"宿舍电费报表　房间 {config.room_key}")
    lines.append(f"生成时间　{clock.now().strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append("-" * 52)

    if not points:
        lines.append("还没有任何读数。先跑一次：python -m rest_electric once")
        return "\n".join(lines)

    lines.append(f"最近 {days} 天读数（{len(points)} 个数据点）")
    lines.append(f"{'日期':<12}{'余额(元)':>10}{'日用电(元)':>14}")

    usages = _usage_by_day(points)
    for moment, balance in points:
        usage = usages.get(moment)
        usage_text = "—" if usage is None else f"{usage / 100:.2f}"
        lines.append(f"{moment.isoformat():<12}{yuan(balance):>10}{usage_text:>14}")

    lines.append("-" * 52)
    last_day, last_balance = points[-1]
    lines.append(f"当前余额　{yuan(last_balance)} 元（{last_day.isoformat()}）")

    if estimate.daily_cents is None:
        lines.append(f"日均用电　无法计算（{estimate.reason}）")
    else:
        lines.append(
            f"日均用电　{estimate.daily_cents / 100:.2f} 元/天"
            f"（{estimate.reason}）"
        )
    if estimate.days_left is None:
        lines.append("预计可用　—")
    else:
        due = last_day + timedelta(days=int(estimate.days_left))
        lines.append(f"预计可用　{estimate.days_left:.1f} 天（约 {due.isoformat()} 欠费）")

    lines.append(
        f"告警阈值　余额 < {config.balance_floor_cents / 100:.2f} 元"
        f" 或 剩余 < {config.days_left_warn:.1f} 天"
    )

    if alerts:
        lines.append("-" * 52)
        lines.append("最近告警")
        for record in alerts:
            label = LEVEL_LABELS.get(record.level, record.level)
            lines.append(f"{record.sent_at[:16]}　{label}　{record.detail}")

    return "\n".join(lines)


def export_csv(points: list[tuple[date, int]], path: Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    usages = _usage_by_day(points)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date", "balance_yuan", "daily_usage_yuan"])
        for moment, balance in points:
            usage = usages.get(moment)
            writer.writerow(
                [
                    moment.isoformat(),
                    yuan(balance),
                    "" if usage is None else f"{usage / 100:.2f}",
                ]
            )
