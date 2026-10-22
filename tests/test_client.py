"""client / models 的解析逻辑：字段路径、金额换算、坏数据必须报错而不是静默写脏。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric.client import FixtureClient, get_path, parse_reading  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.errors import ProtocolError  # noqa: E402
from rest_electric.models import to_cents, yuan  # noqa: E402
from test_config import VALID  # noqa: E402


def _config(text: str = VALID):
    tmp = WorkspaceTmpDir("client")
    path = tmp.path / "config.toml"
    path.write_text(text, encoding="utf-8")
    config = load_config(path)
    return config, tmp


class GetPathTest(unittest.TestCase):
    def test_nested(self) -> None:
        self.assertEqual(get_path({"data": {"balance": "1.00"}}, "data.balance"), "1.00")

    def test_list_index(self) -> None:
        data = {"data": {"records": [{"balance": 12.5}]}}
        self.assertEqual(get_path(data, "data.records.0.balance"), 12.5)

    def test_missing_key_mentions_available(self) -> None:
        with self.assertRaises(ProtocolError) as ctx:
            get_path({"data": {"amount": 1}}, "data.balance")
        self.assertIn("amount", str(ctx.exception))

    def test_walk_into_scalar(self) -> None:
        with self.assertRaises(ProtocolError):
            get_path({"data": "oops"}, "data.balance")


class MoneyTest(unittest.TestCase):
    def test_yuan_string(self) -> None:
        self.assertEqual(to_cents("25.30"), 2530)
        self.assertEqual(to_cents("0.01"), 1)
        self.assertEqual(to_cents("-3.5"), -350)

    def test_float_does_not_lose_precision(self) -> None:
        self.assertEqual(to_cents(19.99), 1999)

    def test_cent_unit(self) -> None:
        self.assertEqual(to_cents(1234, "cent"), 1234)

    def test_bad_values(self) -> None:
        for bad in (None, "", "abc", True, [1]):
            with self.assertRaises(ProtocolError):
                to_cents(bad)

    def test_yuan_format(self) -> None:
        self.assertEqual(yuan(2530), "25.30")
        self.assertEqual(yuan(5), "0.05")
        self.assertEqual(yuan(-5), "-0.05")


class ParseTest(unittest.TestCase):
    def test_parse_sample(self) -> None:
        config, tmp = _config()
        try:
            data = json.loads((Path(__file__).parent / "fixtures" / "sample_reading.json").read_text("utf-8"))
            reading = parse_reading(data, config, source="fixture", captured_at="2026-10-21T09:00:00+08:00")
            self.assertEqual(reading.balance_cents, 2530)
            self.assertEqual(reading.balance_yuan, "25.30")
            self.assertEqual(reading.room_key, "A-305")
            self.assertEqual(reading.captured_at, "2026-10-21T09:00:00+08:00")
        finally:
            tmp.cleanup()

    def test_fixture_client(self) -> None:
        config, tmp = _config()
        try:
            reading = FixtureClient(config).fetch()
            self.assertEqual(reading.balance_cents, 2530)
            self.assertEqual(reading.source, "fixture")
        finally:
            tmp.cleanup()

    def test_missing_field_raises(self) -> None:
        config, tmp = _config()
        try:
            with self.assertRaises(ProtocolError):
                parse_reading({"code": 0}, config)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
