"""store 的边界：幂等写入、按天聚合、告警冷却。"""

from __future__ import annotations

import sys
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric import store  # noqa: E402
from rest_electric.clock import today  # noqa: E402
from rest_electric.models import AlertRecord, Reading  # noqa: E402


def _reading(balance: int, captured_at: str, room: str = "A-305") -> Reading:
    return Reading(room_key=room, balance_cents=balance, captured_at=captured_at, source="test")


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("store")
        self.db_path = self._tmp.path / "electric.db"
        self.conn = store.connect(self.db_path)
        store.init_db(self.conn)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def test_insert_and_last_reading(self) -> None:
        self.assertTrue(store.insert_reading(self.conn, _reading(1234, "2026-10-20T09:00:00+08:00")))
        last = store.last_reading(self.conn, "A-305")
        assert last is not None
        self.assertEqual(last.balance_cents, 1234)
        self.assertEqual(last.balance_yuan, "12.34")

    def test_duplicate_is_ignored(self) -> None:
        reading = _reading(1234, "2026-10-20T09:00:00+08:00")
        self.assertTrue(store.insert_reading(self.conn, reading))
        self.assertFalse(store.insert_reading(self.conn, reading))
        rows = self.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()
        self.assertEqual(rows["n"], 1)

    def test_daily_points_keeps_last_of_day(self) -> None:
        store.insert_reading(self.conn, _reading(5000, "2026-10-20T09:00:00+08:00"))
        store.insert_reading(self.conn, _reading(4800, "2026-10-20T21:00:00+08:00"))
        points = store.daily_points(self.conn, "A-305", days=None)
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0][1], 4800)

    def test_daily_points_sorted_and_filtered(self) -> None:
        for offset, balance in ((3, 9000), (1, 7000), (2, 8000)):
            day = today() - timedelta(days=offset)
            store.insert_reading(
                self.conn, _reading(balance, f"{day.isoformat()}T09:00:00+08:00")
            )
        points = store.daily_points(self.conn, "A-305", days=3)
        self.assertEqual([value for _, value in points], [8000, 7000])

    def test_daily_points_days_window_excludes_older(self) -> None:
        for offset, balance in ((3, 9000), (1, 7000), (2, 8000)):
            day = today() - timedelta(days=offset)
            store.insert_reading(
                self.conn, _reading(balance, f"{day.isoformat()}T09:00:00+08:00")
            )
        points = store.daily_points(self.conn, "A-305", days=2)
        self.assertEqual([value for _, value in points], [7000])

    def test_raw_json_respects_keep_raw(self) -> None:
        reading = Reading(
            room_key="A-305",
            balance_cents=100,
            captured_at="2026-10-20T09:00:00+08:00",
            raw={"secret": "x"},
        )
        store.insert_reading(self.conn, reading, keep_raw=False)
        row = self.conn.execute("SELECT raw_json FROM readings").fetchone()
        self.assertIsNone(row["raw_json"])

    def test_alert_cooldown(self) -> None:
        self.assertFalse(store.already_alerted(self.conn, "A-305", "low_balance", 1))
        store.record_alert(
            self.conn,
            AlertRecord(
                room_key="A-305",
                level="low_balance",
                day=today().isoformat(),
                sent_at="2026-10-20T09:00:00+08:00",
                detail="余额 12.34",
            ),
        )
        self.assertTrue(store.already_alerted(self.conn, "A-305", "low_balance", 1))

    def test_alert_cooldown_expires(self) -> None:
        old_day = (today() - timedelta(days=10)).isoformat()
        store.record_alert(
            self.conn,
            AlertRecord(
                room_key="A-305",
                level="auth_expired",
                day=old_day,
                sent_at=f"{old_day}T09:00:00+08:00",
            ),
        )
        self.assertFalse(store.already_alerted(self.conn, "A-305", "auth_expired", 7))

    def test_meta_roundtrip(self) -> None:
        store.set_meta(self.conn, "last_success_at", "2026-10-21T09:00:00+08:00")
        self.assertEqual(
            store.get_meta(self.conn, "last_success_at"), "2026-10-21T09:00:00+08:00"
        )
        self.assertEqual(store.get_meta(self.conn, "missing", "fallback"), "fallback")


if __name__ == "__main__":
    unittest.main()
