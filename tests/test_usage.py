"""充值识别与每日汇总：重点覆盖「小额充值被日终对比吃掉」的场景。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rest_electric.models import Reading  # noqa: E402
from rest_electric.usage import daily_usage  # noqa: E402


def reading(moment: str, cents: int) -> Reading:
    return Reading(room_key="room", balance_cents=cents, captured_at=moment, source="epay")


class DailyUsageTest(unittest.TestCase):
    def test_simple_decline(self) -> None:
        readings = [
            reading("2026-10-01T08:00:00+08:00", 1000),
            reading("2026-10-01T10:00:00+08:00", 950),
            reading("2026-10-01T12:00:00+08:00", 900),
        ]
        days = daily_usage(readings)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].day, date(2026, 10, 1))
        self.assertEqual(days[0].used_cents, 100)
        self.assertEqual(days[0].recharged_cents, 0)
        self.assertEqual(days[0].balance_cents, 900)

    def test_small_recharge_smaller_than_daily_usage(self) -> None:
        """充 1 元、当天用 2 元：日终仍是下降的，但充值必须被识别出来。"""
        readings = [
            reading("2026-10-01T08:00:00+08:00", 1000),
            reading("2026-10-01T10:00:00+08:00", 950),
            reading("2026-10-01T12:00:00+08:00", 1000),  # 10:00 后充了 1 元
            reading("2026-10-01T14:00:00+08:00", 950),
            reading("2026-10-01T16:00:00+08:00", 900),
        ]
        days = daily_usage(readings)
        self.assertEqual(len(days), 1)
        self.assertEqual(days[0].recharged_cents, 100)
        self.assertEqual(days[0].used_cents, 200)  # 4 个区间各 50
        self.assertEqual(days[0].balance_cents, 900)

    def test_recharge_larger_than_daily_usage(self) -> None:
        readings = [
            reading("2026-10-01T08:00:00+08:00", 1000),
            reading("2026-10-01T10:00:00+08:00", 900),
            reading("2026-10-01T12:00:00+08:00", 1800),  # 充 10 元
        ]
        days = daily_usage(readings)
        self.assertEqual(days[0].recharged_cents, 1000)
        self.assertEqual(days[0].used_cents, 200)

    def test_recharge_counted_on_the_later_day(self) -> None:
        readings = [
            reading("2026-10-01T22:00:00+08:00", 1000),
            reading("2026-10-02T08:00:00+08:00", 1500),  # 夜里充的
        ]
        days = daily_usage(readings)
        by_day = {item.day: item for item in days}
        self.assertEqual(by_day[date(2026, 10, 2)].recharged_cents, 500)

    def test_midnight_interval_splits_usage(self) -> None:
        readings = [
            reading("2026-10-01T22:00:00+08:00", 1000),
            reading("2026-10-02T08:00:00+08:00", 980),  # 10 小时用 20 分
        ]
        days = daily_usage(readings)
        by_day = {item.day: item for item in days}
        self.assertEqual(by_day[date(2026, 10, 1)].used_cents, 4)   # 2 小时
        self.assertEqual(by_day[date(2026, 10, 2)].used_cents, 16)  # 8 小时

    def test_empty(self) -> None:
        self.assertEqual(daily_usage([]), [])

    def test_day_without_readings_is_kept_for_usage(self) -> None:
        """断了一天没查，但那段时间的消耗仍应记在那一天。"""
        readings = [
            reading("2026-10-01T22:00:00+08:00", 1000),
            reading("2026-10-03T08:00:00+08:00", 940),
        ]
        days = daily_usage(readings)
        by_day = {item.day: item for item in days}
        self.assertIsNone(by_day[date(2026, 10, 2)].balance_cents)
        self.assertGreater(by_day[date(2026, 10, 2)].used_cents, 0)


if __name__ == "__main__":
    unittest.main()
