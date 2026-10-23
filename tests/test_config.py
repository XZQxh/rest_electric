"""config 的边界：缺段、缺字段、类型错误、模式校验、fixture 文件缺失。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.errors import ConfigError  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample_reading.json"

VALID = f"""
[room]
room_key = "A-305"
building = "A栋"
room = "305"

[storage]
db_path = "data/test.db"
keep_raw = true

[alert]
balance_floor_cents = 2000
days_left_warn = 3.0
window_days = 7
jitter_minutes = 5

[http]
timeout_seconds = 10
attempts = 2
user_agent = "test-agent"

[api]
mode = "fixture"
base_url = ""
balance_path = ""
method = "GET"
balance_field = "data.balance"
balance_unit = "yuan"
capture_file = "{SAMPLE.as_posix()}"

[notify]
channel = "serverchan"
include_history = true
"""


class ConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("config")
        self.dir = self._tmp.path

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, text: str, name: str = "config.toml") -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    def test_valid_config(self) -> None:
        config = load_config(self._write(VALID))
        self.assertEqual(config.room_key, "A-305")
        self.assertEqual(config.balance_floor_cents, 2000)
        self.assertEqual(config.days_left_warn, 3.0)
        self.assertEqual(config.http_attempts, 2)
        self.assertEqual(config.api_mode, "fixture")
        self.assertEqual(config.token_header, "Authorization")
        self.assertTrue(str(config.db_path).endswith("data\\test.db") or str(config.db_path).endswith("data/test.db"))

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError):
            load_config(self.dir / "nope.toml")

    def test_missing_section(self) -> None:
        text = VALID.replace("[alert]", "[alert_disabled]")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(text))
        self.assertIn("alert", str(ctx.exception))

    def test_missing_room_key(self) -> None:
        text = VALID.replace('room_key = "A-305"', "")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(text))
        self.assertIn("room_key", str(ctx.exception))

    def test_invalid_mode(self) -> None:
        text = VALID.replace('mode = "fixture"', 'mode = "telepathy"')
        with self.assertRaises(ConfigError) as ctx:
            load_config(self._write(text))
        self.assertIn("mode", str(ctx.exception))

    def test_invalid_unit(self) -> None:
        text = VALID.replace('balance_unit = "yuan"', 'balance_unit = "kg"')
        with self.assertRaises(ConfigError):
            load_config(self._write(text))

    def test_bad_type(self) -> None:
        text = VALID.replace("balance_floor_cents = 2000", 'balance_floor_cents = "20"')
        with self.assertRaises(ConfigError):
            load_config(self._write(text))

    def test_http_mode_requires_balance_field(self) -> None:
        text = VALID.replace('mode = "fixture"', 'mode = "http"').replace(
            'balance_field = "data.balance"', ""
        )
        with self.assertRaises(ConfigError):
            load_config(self._write(text))

    def test_fixture_requires_existing_file(self) -> None:
        text = VALID.replace(
            f'capture_file = "{SAMPLE.as_posix()}"', 'capture_file = "capture/nope.json"'
        )
        with self.assertRaises(ConfigError):
            load_config(self._write(text))

    def test_secrets_loaded(self) -> None:
        cfg_path = self._write(VALID)
        secrets = self._write(
            '[api]\ntoken = "abc123"\n[api.extra_headers]\nReferer = "https://x"\n',
            "secrets.toml",
        )
        config = load_config(cfg_path, secrets)
        self.assertEqual(config.secret("api", "token"), "abc123")
        self.assertEqual(config.extra_headers(), {"Referer": "https://x"})

    def test_missing_secrets_is_not_fatal(self) -> None:
        config = load_config(self._write(VALID))
        self.assertEqual(config.secrets, {})
        self.assertTrue(config.secrets_error)

    def test_report_defaults(self) -> None:
        config = load_config(self._write(VALID))
        self.assertTrue(config.report_enabled)
        self.assertEqual(config.report_chart_days, 14)
        self.assertEqual(config.report_to, "")
        self.assertEqual(config.report_recipient(), "")

    def test_report_section(self) -> None:
        text = VALID + '\n[report]\nenabled = false\nchart_days = 7\nto = "me@example.com"\n'
        config = load_config(self._write(text))
        self.assertFalse(config.report_enabled)
        self.assertEqual(config.report_chart_days, 7)
        self.assertEqual(config.report_to, "me@example.com")
        self.assertEqual(config.report_recipient(), "me@example.com")

    def test_report_to_falls_back_to_smtp_to(self) -> None:
        cfg_path = self._write(VALID + '\n[report]\nto = ""\n')
        secrets = self._write(
            '[notify]\nsmtp_to = "fallback@example.com"\n', "secrets.toml"
        )
        config = load_config(cfg_path, secrets)
        self.assertEqual(config.report_recipient(), "fallback@example.com")

    def test_report_chart_days_too_small(self) -> None:
        text = VALID + "\n[report]\nchart_days = 1\n"
        with self.assertRaises(ConfigError):
            load_config(self._write(text))


if __name__ == "__main__":
    unittest.main()
