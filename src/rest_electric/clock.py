"""时间工具。统一使用 Asia/Shanghai；系统缺少 tzdata 时退回固定 +08:00。

树莓派与 Windows 都能跑。固定偏移不处理历史夏令时（中国大陆现行无夏令时），
对本项目「按本地日期聚合」的用途没有影响。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

try:  # 正常路径：系统或 tzdata 包提供时区库
    from zoneinfo import ZoneInfo

    TZ = ZoneInfo("Asia/Shanghai")
    TZ_NAME = "Asia/Shanghai"
except Exception:  # pragma: no cover - 仅在缺少 tzdata 的 Windows 上触发
    TZ = timezone(timedelta(hours=8), name="+08:00")
    TZ_NAME = "+08:00"


def now() -> datetime:
    """当前时间（带时区）。"""
    return datetime.now(TZ)


def iso(moment: datetime | None = None) -> str:
    """ISO 8601 字符串，精确到秒并带偏移，例如 2026-10-21T09:07:12+08:00。"""
    return (moment or now()).isoformat(timespec="seconds")


def today() -> date:
    return now().date()


def parse_day(value: str) -> date:
    """从 ISO 8601 字符串取本地日期。"""
    return datetime.fromisoformat(value).date()


def days_ago(days: int) -> date:
    return today() - timedelta(days=days)
