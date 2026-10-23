"""把读数序列还原成「每天用了多少、充了多少」。

为什么不直接用「今天最后一条 − 昨天最后一条」：

    充值会让余额上升。如果某天充值金额小于当天消耗，日终余额仍然是下降的，
    这个差值就变成「消耗 − 充值」，把用电量算少了，而且看不出来。

有 2 小时粒度的读数时，可以逐区间判断：**余额上升 = 那个区间里发生了充值**。

    充值金额 ≈ 上升幅度 + 该区间内的消耗

区间内的消耗按整体中位速率估算（2 小时通常只有几分钱，误差很小），
最后按「充值一般是整数元」四舍五入。

已知边界：
  * 充值额小于该区间的消耗时，余额整体仍在下降，检测不到——
    但 2 小时的消耗通常只有 0.1 元上下，现实中不会有人充 0.1 元；
  * 跨午夜的区间按小时数把消耗摊到两天；充值归到后一条读数所在的日期；
  * 同一天多次充值会被合并成一个金额。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from statistics import median

from .models import Reading


@dataclass(frozen=True)
class DayUsage:
    day: date
    balance_cents: int | None  # 当天最后一条读数；当天没有读数则为 None
    used_cents: int            # 当天真实消耗（已把充值加回去）
    recharged_cents: int       # 当天检测到的充值（估算后取整到元）
    readings: int              # 当天读数条数


def _parse(value: str) -> datetime:
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:  # 兜底：老数据可能没带时区
        from .clock import TZ

        moment = moment.replace(tzinfo=TZ)
    return moment


def hourly_rate_cents(readings: list[Reading]) -> float:
    """中位数用电速率（分/小时），只用余额下降的区间，抗住充值和断档。"""
    rates: list[float] = []
    ordered = sorted(readings, key=lambda item: item.captured_at)
    for first, second in zip(ordered, ordered[1:]):
        hours = (_parse(second.captured_at) - _parse(first.captured_at)).total_seconds() / 3600
        drop = first.balance_cents - second.balance_cents
        if hours > 0 and drop > 0:
            rates.append(drop / hours)
    return median(rates) if rates else 0.0


def _split_used(buckets: dict[date, dict], start: datetime, end: datetime, used: int) -> None:
    """把一段区间的消耗按小时比例摊到跨过的每一天。"""
    total = (end - start).total_seconds() / 3600
    if total <= 0 or used <= 0:
        return
    cursor = start
    while cursor < end:
        midnight = datetime.combine(
            cursor.date() + timedelta(days=1), time.min, tzinfo=cursor.tzinfo
        )
        chunk_end = min(end, midnight)
        fraction = (chunk_end - cursor).total_seconds() / 3600 / total
        bucket = buckets.setdefault(cursor.date(), {"last": None, "used": 0.0, "recharged": 0, "n": 0})
        bucket["used"] += used * fraction
        cursor = chunk_end


def daily_usage(readings: list[Reading]) -> list[DayUsage]:
    """按天汇总。返回按日期升序，包含区间跨过的空白日。"""
    if not readings:
        return []

    ordered = sorted(readings, key=lambda item: item.captured_at)
    parsed = [(_parse(item.captured_at), item) for item in ordered]
    rate = hourly_rate_cents(readings)

    buckets: dict[date, dict] = {}
    for moment, item in parsed:
        bucket = buckets.setdefault(moment.date(), {"last": None, "used": 0.0, "recharged": 0, "n": 0})
        bucket["last"] = item.balance_cents
        bucket["n"] += 1

    for (start, first), (end, second) in zip(parsed, parsed[1:]):
        hours = (end - start).total_seconds() / 3600
        if hours <= 0:
            continue
        delta = first.balance_cents - second.balance_cents
        if delta >= 0:
            _split_used(buckets, start, end, delta)
            continue

        # 余额上升 = 充值。加上这段区间里本来也会消耗掉的部分，再取整到元；
        # 这段的消耗仍然要计进当天用电，否则会凭空少一截
        interval_used = rate * hours
        estimate = (-delta) + interval_used
        recharged = int(round(estimate / 100.0)) * 100
        if recharged <= 0:  # 不足 0.5 元的异常波动，忽略
            continue
        _split_used(buckets, start, end, int(round(interval_used)))
        bucket = buckets.setdefault(end.date(), {"last": None, "used": 0.0, "recharged": 0, "n": 0})
        bucket["recharged"] += recharged

    result = [
        DayUsage(
            day=day,
            balance_cents=bucket["last"],
            used_cents=int(round(bucket["used"])),
            recharged_cents=int(bucket["recharged"]),
            readings=int(bucket["n"]),
        )
        for day, bucket in sorted(buckets.items())
    ]
    return result
