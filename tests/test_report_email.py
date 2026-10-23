"""report --email：CLI 去重、--force、按配置天数、退出码。"""

from __future__ import annotations

import argparse
import contextlib
import io
import logging
import smtplib
import sys
import unittest
from datetime import timedelta
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fake_smtp import FakeSMTP  # noqa: E402
from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric import chart, cli, clock, store  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.models import Reading  # noqa: E402

CONFIG = """
[room]
room_key = "11010610"
building = "1"
room = "11010610"

[storage]
db_path = "{db}"
keep_raw = true

[alert]
balance_floor_cents = 500
days_left_warn = 1.0
window_days = 7
jitter_minutes = 5

[http]
timeout_seconds = 15
attempts = 3
user_agent = "test-agent"

[api]
mode = "epay"
base_url = "https://shoucard.shou.edu.cn"
balance_path = "/epay/h5/eleresult"
method = "GET"
balance_field = "data.balance"
balance_unit = "yuan"

[notify]
channel = "serverchan"
include_history = true

[report]
enabled = {enabled}
chart_days = {chart_days}
to = "receiver@example.com"
"""

SECRETS = """
[notify]
smtp_host = "smtp.example.com"
smtp_port = 2525
smtp_user = "sender@example.com"
smtp_password = "auth-code"
smtp_to = "receiver@example.com"
"""


class _FailingSMTP(FakeSMTP):
    instances: list = []

    def login(self, user: str, password: str) -> None:
        raise smtplib.SMTPAuthenticationError(535, b"bad auth")


class ReportEmailTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("report-email")
        self.dir = self._tmp.path
        FakeSMTP.reset()
        self._logger = logging.getLogger("rest_electric.test")
        self._logger.addHandler(logging.NullHandler())
        self._logger.propagate = False

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _setup(self, *, enabled: bool = True, chart_days: int = 14, seed: bool = True):
        db_path = (self.dir / "data" / "electric.db").as_posix()
        cfg_path = self.dir / "config.toml"
        cfg_path.write_text(
            CONFIG.format(enabled=str(enabled).lower(), chart_days=chart_days, db=db_path),
            encoding="utf-8",
        )
        sec_path = self.dir / "secrets.toml"
        sec_path.write_text(SECRETS, encoding="utf-8")
        config = load_config(cfg_path, sec_path)
        if seed:
            conn = store.connect(config.db_path)
            store.init_db(conn)
            for item in chart.demo_readings():
                store.insert_reading(
                    conn,
                    Reading(
                        room_key=config.room_key,
                        balance_cents=item.balance_cents,
                        captured_at=item.captured_at,
                        source="test",
                    ),
                )
            conn.close()
        return config, cfg_path, sec_path

    def _namespace(self, cfg_path: Path, sec_path: Path, *, force: bool = False):
        return argparse.Namespace(
            config=cfg_path,
            secrets=sec_path,
            verbose=False,
            days=None,
            email=True,
            force=force,
            csv=None,
            push=False,
        )

    def _run(self, namespace) -> tuple[int, str]:
        buffer = io.StringIO()
        with mock.patch("rest_electric.cli._logger", return_value=self._logger):
            with mock.patch("rest_electric.notify.smtplib.SMTP", FakeSMTP):
                with contextlib.redirect_stdout(buffer):
                    code = cli.cmd_report(namespace)
        return code, buffer.getvalue()

    def _meta(self, config) -> tuple[str, str]:
        conn = store.connect(config.db_path)
        try:
            return (
                store.get_meta(conn, "daily_report_sent"),
                store.get_meta(conn, "daily_report_sent_at"),
            )
        finally:
            conn.close()

    def test_sends_once_then_dedupes_and_force_resends(self) -> None:
        config, cfg_path, sec_path = self._setup()
        namespace = self._namespace(cfg_path, sec_path)

        code, output = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertIn("已发送日报邮件", output)
        self.assertEqual(len(FakeSMTP.instances), 1)
        sent_day, sent_at = self._meta(config)
        self.assertEqual(sent_day, clock.today().isoformat())
        self.assertTrue(sent_at)

        code, output = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertIn("已经发过", output)
        self.assertEqual(len(FakeSMTP.instances), 1)

        namespace.force = True
        code, _ = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeSMTP.instances), 2)

    def test_dedupe_window_expires_after_three_hours(self) -> None:
        config, cfg_path, sec_path = self._setup()
        namespace = self._namespace(cfg_path, sec_path)
        code, _ = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeSMTP.instances), 1)

        conn = store.connect(config.db_path)
        try:
            store.set_meta(
                conn,
                "daily_report_sent_at",
                (clock.now() - timedelta(hours=4)).isoformat(timespec="seconds"),
            )
        finally:
            conn.close()

        code, output = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertIn("已发送日报邮件", output)
        self.assertEqual(len(FakeSMTP.instances), 2)

    def test_smtp_failure_returns_2_without_marking_sent(self) -> None:
        config, cfg_path, sec_path = self._setup()
        namespace = self._namespace(cfg_path, sec_path)
        buffer = io.StringIO()
        with mock.patch("rest_electric.cli._logger", return_value=self._logger):
            with mock.patch("rest_electric.notify.smtplib.SMTP", _FailingSMTP):
                with contextlib.redirect_stdout(buffer):
                    with contextlib.redirect_stderr(buffer):
                        code = cli.cmd_report(namespace)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("发送失败", buffer.getvalue())
        self.assertEqual(self._meta(config), ("", ""))

    def test_uses_report_chart_days(self) -> None:
        _, cfg_path, sec_path = self._setup(chart_days=7)
        config = load_config(cfg_path, sec_path)
        namespace = self._namespace(cfg_path, sec_path)
        code, _ = self._run(namespace)
        self.assertEqual(code, 0)
        raw = FakeSMTP.instances[0].messages[0]
        parsed = BytesParser(policy=policy.default).parsebytes(raw)
        html = parsed.get_body(preferencelist=("html",)).get_content()
        self.assertIn("最近 7 天", html)
        self.assertEqual(
            self._meta(config)[0], clock.today().isoformat()
        )

    def test_disabled_skips_without_force(self) -> None:
        _, cfg_path, sec_path = self._setup(enabled=False)
        namespace = self._namespace(cfg_path, sec_path)
        code, output = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertIn("禁用", output)
        self.assertEqual(FakeSMTP.instances, [])

        namespace.force = True
        code, _ = self._run(namespace)
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeSMTP.instances), 1)

    def test_missing_smtp_exits_with_config_error(self) -> None:
        db_path = (self.dir / "data" / "electric.db").as_posix()
        cfg_path = self.dir / "config.toml"
        cfg_path.write_text(
            CONFIG.format(enabled="true", chart_days=14, db=db_path), encoding="utf-8"
        )
        sec_path = self.dir / "secrets.toml"
        sec_path.write_text('[ingest]\nkey = "0123456789abcdef"\n', encoding="utf-8")

        buffer = io.StringIO()
        with mock.patch("rest_electric.cli._logger", return_value=self._logger):
            with contextlib.redirect_stdout(buffer):
                with contextlib.redirect_stderr(buffer):
                    code = cli.main(
                        [
                            "report",
                            "--email",
                            "--force",
                            "--config",
                            str(cfg_path),
                            "--secrets",
                            str(sec_path),
                        ]
                    )
        self.assertEqual(code, cli.EXIT_CONFIG)
        self.assertIn("SMTP", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
