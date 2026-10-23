"""mail：MIME 结构、CID、HTML、空数据/图表失败的降级、假 SMTP 发送。"""

from __future__ import annotations

import re
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
from rest_electric import chart, clock, mail, notify, report, usage  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.errors import ConfigError  # noqa: E402
from rest_electric.forecast import estimate  # noqa: E402
from rest_electric.models import yuan  # noqa: E402
from rest_electric.usage import DayUsage  # noqa: E402

CONFIG = """
[room]
room_key = "12345678"
building = "1"
room = "12345678"

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
"""


class MailTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("mail")
        self.dir = self._tmp.path
        FakeSMTP.reset()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # ---- 夹具 ----------------------------------------------------

    def _config(
        self,
        *,
        report_to: str = "receiver@example.com",
        enabled: bool = True,
        chart_days: int = 14,
        smtp_to: str = "fallback@example.com",
        smtp: bool = True,
    ):
        db_path = (self.dir / "data" / "electric.db").as_posix()
        text = CONFIG.format(db=db_path)
        text += (
            "\n[report]\n"
            f'enabled = {"true" if enabled else "false"}\n'
            f"chart_days = {chart_days}\n"
            f'to = "{report_to}"\n'
        )
        cfg_path = self.dir / "config.toml"
        cfg_path.write_text(text, encoding="utf-8")

        if smtp:
            secrets_text = (
                "[notify]\n"
                'smtp_host = "smtp.example.com"\n'
                "smtp_port = 2525\n"
                'smtp_user = "sender@example.com"\n'
                'smtp_password = "auth-code"\n'
                f'smtp_to = "{smtp_to}"\n'
            )
        else:
            secrets_text = '[ingest]\nkey = "0123456789abcdef"\n'
        sec_path = self.dir / "secrets.toml"
        sec_path.write_text(secrets_text, encoding="utf-8")
        return load_config(cfg_path, sec_path)

    @staticmethod
    def _demo(config):
        readings = chart.demo_readings()
        days = usage.daily_usage(readings)
        points = [
            (item.day, item.balance_cents)
            for item in days
            if item.balance_cents is not None
        ]
        return readings, days, points

    @staticmethod
    def _parse(message):
        return BytesParser(policy=policy.default).parsebytes(message.as_bytes())

    @staticmethod
    def _without_time(text: str) -> str:
        return "\n".join(
            line for line in text.splitlines() if not line.startswith("生成时间")
        )

    # ---- 结构 ----------------------------------------------------

    def test_mime_structure_with_inline_images(self) -> None:
        config = self._config()
        readings, days, points = self._demo(config)
        message = mail.build_daily_email(
            config,
            points,
            estimate(points),
            [],
            day_usages=days,
            days=14,
            last_reading=readings[-1],
        )
        parsed = self._parse(message)

        self.assertEqual(parsed.get_content_type(), "multipart/related")
        self.assertEqual(
            [part.get_content_type() for part in parsed.get_payload()],
            ["multipart/alternative", "image/png", "image/png"],
        )
        alternative = parsed.get_payload()[0]
        self.assertEqual(
            [part.get_content_type() for part in alternative.get_payload()],
            ["text/plain", "text/html"],
        )

        images = [part for part in parsed.walk() if part.get_content_type() == "image/png"]
        self.assertEqual(
            {part["Content-ID"] for part in images},
            {"<chart-balance>", "<chart-usage>"},
        )
        for image in images:
            self.assertTrue(image.get_payload(decode=True).startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertEqual(image.get_content_disposition(), "inline")

        html = parsed.get_body(preferencelist=("html",)).get_content()
        self.assertIn("cid:chart-balance", html)
        self.assertIn("cid:chart-usage", html)
        self.assertIn("充值(元)", html)

        plain = parsed.get_body(preferencelist=("plain",)).get_content()
        self.assertIn("宿舍电费报表", plain)
        self.assertIn("日均用电", plain)
        # 纯文本版必须和 report.render() 的输出一致（生成时间那一行会比对上晚一点）
        expected = report.render(
            config, points, estimate(points), [], days=14, last_reading=readings[-1]
        )
        self.assertEqual(self._without_time(plain), self._without_time(expected))

        subject = str(parsed["Subject"])
        self.assertTrue(parsed["Date"])
        self.assertTrue(parsed["Message-ID"])
        self.assertTrue(parsed["MIME-Version"])
        self.assertIn("电费日报", subject)
        self.assertIn("余额", subject)
        self.assertIn("元", subject)
        self.assertRegex(
            subject,
            r"^电费日报 \d{4}-\d{2}-\d{2} · 余额 \d+\.\d{2} 元 · (约 \d+ 天|不足 1 天)$",
        )

    def test_html_marks_recharge_days(self) -> None:
        config = self._config()
        _, days, points = self._demo(config)
        recharged = [item for item in days if item.recharged_cents > 0]
        self.assertTrue(recharged, "演示数据里应当能识别出充值")
        message = mail.build_daily_email(
            config, points, estimate(points), [], day_usages=days, days=14
        )
        html = self._parse(message).get_body(preferencelist=("html",)).get_content()
        self.assertIn(f"+{yuan(recharged[-1].recharged_cents)}", html)
        self.assertIn("#059669", html)

    def test_empty_data_sends_warning_text(self) -> None:
        config = self._config()
        message = mail.build_daily_email(config, [], estimate([]), [])
        parsed = self._parse(message)

        self.assertEqual(parsed.get_content_type(), "multipart/alternative")
        self.assertEqual(
            [part.get_content_type() for part in parsed.get_payload()],
            ["text/plain", "text/html"],
        )
        self.assertFalse(
            any(part.get_content_type() == "image/png" for part in parsed.walk())
        )
        subject = str(parsed["Subject"])
        self.assertTrue(subject.startswith("⚠️"))
        self.assertIn("今日无数据", subject)
        html = parsed.get_body(preferencelist=("html",)).get_content()
        self.assertIn("还没有任何读数", html)
        self.assertIn("数据不足", html)
        self.assertIn("暂无读数", html)

    def test_stale_today_adds_warning_but_keeps_charts(self) -> None:
        config = self._config()
        today = clock.today()
        points = [(today - timedelta(days=2), 2000), (today - timedelta(days=1), 1900)]
        usages = [
            DayUsage(day=today - timedelta(days=2), balance_cents=2000,
                     used_cents=0, recharged_cents=0, readings=8),
            DayUsage(day=today - timedelta(days=1), balance_cents=1900,
                     used_cents=100, recharged_cents=0, readings=8),
        ]
        message = mail.build_daily_email(
            config, points, estimate(points), [], day_usages=usages, days=14
        )
        parsed = self._parse(message)
        self.assertTrue(str(parsed["Subject"]).startswith("⚠️"))
        self.assertEqual(parsed.get_content_type(), "multipart/related")
        html = parsed.get_body(preferencelist=("html",)).get_content()
        self.assertIn("今天还没有读数", html)

    def test_single_point_skips_charts(self) -> None:
        config = self._config()
        today = clock.today()
        points = [(today, 1234)]
        usages = [DayUsage(day=today, balance_cents=1234, used_cents=0,
                           recharged_cents=0, readings=1)]
        message = mail.build_daily_email(
            config, points, estimate(points), [], day_usages=usages, days=14
        )
        parsed = self._parse(message)
        self.assertFalse(
            any(part.get_content_type() == "image/png" for part in parsed.walk())
        )
        self.assertFalse(str(parsed["Subject"]).startswith("⚠️"))
        html = parsed.get_body(preferencelist=("html",)).get_content()
        self.assertIn("数据不足", html)

    def test_chart_failure_falls_back_to_plain_text(self) -> None:
        config = self._config()
        readings, days, points = self._demo(config)
        with self.assertLogs("rest_electric.mail", level="ERROR"):
            with mock.patch(
                "rest_electric.mail.chart.line_chart", side_effect=RuntimeError("boom")
            ):
                message = mail.build_daily_email(
                    config,
                    points,
                    estimate(points),
                    [],
                    day_usages=days,
                    days=14,
                    last_reading=readings[-1],
                )
        parsed = self._parse(message)
        self.assertEqual(parsed.get_content_type(), "text/plain")
        self.assertIn("图表生成失败", parsed.get_content())

    # ---- 发送 ----------------------------------------------------

    def test_send_uses_smtp_and_marks_recipient(self) -> None:
        config = self._config()
        _, days, points = self._demo(config)
        message = mail.build_daily_email(
            config, points, estimate(points), [], day_usages=days, days=14
        )
        with mock.patch("rest_electric.notify.smtplib.SMTP", FakeSMTP):
            mail.send_daily_email(config, message)

        self.assertEqual(len(FakeSMTP.instances), 1)
        smtp = FakeSMTP.instances[0]
        self.assertEqual((smtp.host, smtp.port), ("smtp.example.com", 2525))
        self.assertTrue(smtp.started_tls)
        self.assertEqual(smtp.login_args, ("sender@example.com", "auth-code"))
        wire = self._parse_bytes(smtp.messages[0])
        self.assertEqual(wire["To"], "receiver@example.com")
        self.assertEqual(wire["From"], "sender@example.com")
        self.assertTrue(wire["Message-ID"])
        self.assertTrue(wire["MIME-Version"])
        self.assertEqual(wire.get_content_type(), "multipart/related")

    def test_smtp_notifier_still_sends_plain_text(self) -> None:
        """重构后原有的纯文本 send() 行为不变（report --push 走 smtp 时依赖它）。"""
        config = self._config()
        with mock.patch("rest_electric.notify.smtplib.SMTP", FakeSMTP):
            notify.smtp_notifier(config).send("纯文本标题", "正文")
        wire = self._parse_bytes(FakeSMTP.instances[0].messages[0])
        self.assertEqual(wire.get_content_type(), "text/plain")
        self.assertEqual(str(wire["Subject"]), "纯文本标题")
        self.assertIn("正文", wire.get_content())

    def test_recipient_falls_back_to_smtp_to(self) -> None:
        config = self._config(report_to="", smtp_to="fallback@example.com")
        message = mail.build_daily_email(config, [], estimate([]), [])
        self.assertEqual(message["To"], "fallback@example.com")

    def test_missing_recipient_is_config_error(self) -> None:
        config = self._config(report_to="", smtp_to="")
        message = mail.build_daily_email(config, [], estimate([]), [])
        with self.assertRaises(ConfigError):
            mail.send_daily_email(config, message)

    def test_missing_smtp_is_config_error(self) -> None:
        config = self._config(smtp=False)
        message = mail.build_daily_email(config, [], estimate([]), [])
        with self.assertRaises(ConfigError):
            mail.send_daily_email(config, message)

    @staticmethod
    def _parse_bytes(raw: bytes):
        return BytesParser(policy=policy.default).parsebytes(raw)


if __name__ == "__main__":
    unittest.main()
