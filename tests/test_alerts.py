"""alerts：两条规则分别触发、可同时触发、阈值边界为开区间。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rest_electric import alerts  # noqa: E402
from rest_electric.forecast import Estimate  # noqa: E402


def _est(days_left: float | None, daily: float | None = 1000.0) -> Estimate:
    return Estimate(daily_cents=daily, days_left=days_left, samples=3, reason="test")


class DecideTest(unittest.TestCase):
    def test_low_balance_only(self) -> None:
        result = alerts.decide(1500, _est(30.0), 2000, 3.0)
        self.assertEqual([item.level for item in result], [alerts.LEVEL_LOW_BALANCE])

    def test_predicted_only(self) -> None:
        result = alerts.decide(50000, _est(1.5), 2000, 3.0)
        self.assertEqual([item.level for item in result], [alerts.LEVEL_PREDICTED_LOW])

    def test_both(self) -> None:
        result = alerts.decide(500, _est(0.4), 2000, 3.0)
        self.assertEqual(
            [item.level for item in result],
            [alerts.LEVEL_LOW_BALANCE, alerts.LEVEL_PREDICTED_LOW],
        )

    def test_none(self) -> None:
        self.assertEqual(alerts.decide(50000, _est(30.0), 2000, 3.0), [])

    def test_boundary_is_exclusive(self) -> None:
        # 余额正好等于阈值、天数正好等于阈值，都不告警
        self.assertEqual(alerts.decide(2000, _est(3.0), 2000, 3.0), [])

    def test_no_prediction_skips_day_rule(self) -> None:
        result = alerts.decide(50000, Estimate(None, None, 0, "数据不足"), 2000, 3.0)
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
