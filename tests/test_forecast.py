"""forecast 的边界：充值跳变、数据不足、零消耗、缺天归一化。"""

from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rest_electric.forecast import daily_samples, estimate  # noqa: E402

D = date.fromisoformat


class DailySamplesTest(unittest.TestCase):
    def test_empty(self) -> None:
        self.assertEqual(daily_samples([]), [])

    def test_single_point(self) -> None:
        self.assertEqual(daily_samples([(D("2026-10-20"), 5000)]), [])

    def test_simple_usage(self) -> None:
        points = [(D("2026-10-20"), 10000), (D("2026-10-21"), 9000)]
        self.assertEqual(daily_samples(points), [1000.0])

    def test_recharge_is_skipped_not_negative(self) -> None:
        points = [
            (D("2026-10-19"), 5000),
            (D("2026-10-20"), 20000),  # 充值 150 元
            (D("2026-10-21"), 19000),
        ]
        self.assertEqual(daily_samples(points), [1000.0])

    def test_gap_is_normalised(self) -> None:
        # 三天只用电 30 元 → 日均 10 元
        points = [(D("2026-10-18"), 5000), (D("2026-10-21"), 2000)]
        self.assertEqual(daily_samples(points), [1000.0])

    def test_zero_change_counts_as_zero(self) -> None:
        points = [(D("2026-10-20"), 5000), (D("2026-10-21"), 5000)]
        self.assertEqual(daily_samples(points), [0.0])

    def test_duplicate_day_ignored(self) -> None:
        points = [(D("2026-10-20"), 5000), (D("2026-10-20"), 4000)]
        self.assertEqual(daily_samples(points), [])


class EstimateTest(unittest.TestCase):
    def test_insufficient_data(self) -> None:
        result = estimate([(D("2026-10-21"), 5000)])
        self.assertIsNone(result.daily_cents)
        self.assertIsNone(result.days_left)
        self.assertEqual(result.samples, 0)
        self.assertIn("数据不足", result.reason)

    def test_median_is_robust_to_outlier(self) -> None:
        points = [
            (D("2026-10-18"), 100000),
            (D("2026-10-19"), 99000),   # 1000
            (D("2026-10-20"), 98000),   # 1000
            (D("2026-10-21"), 8000),    # 90000（例如误开大功率）
        ]
        result = estimate(points)
        self.assertEqual(result.daily_cents, 1000.0)
        self.assertEqual(result.samples, 3)
        self.assertAlmostEqual(result.days_left or 0, 8.0)

    def test_zero_consumption_has_no_prediction(self) -> None:
        points = [(D("2026-10-20"), 5000), (D("2026-10-21"), 5000)]
        result = estimate(points)
        self.assertEqual(result.daily_cents, 0.0)
        self.assertIsNone(result.days_left)
        self.assertIn("没有下降", result.reason)

    def test_already_negative_balance(self) -> None:
        points = [(D("2026-10-20"), 2000), (D("2026-10-21"), 1000)]
        result = estimate(points, last_balance_cents=-500)
        self.assertEqual(result.days_left, 0.0)

    def test_explicit_balance_overrides_series(self) -> None:
        points = [(D("2026-10-20"), 10000), (D("2026-10-21"), 9000)]
        result = estimate(points, last_balance_cents=4500)
        self.assertAlmostEqual(result.days_left or 0, 4.5)


if __name__ == "__main__":
    unittest.main()
