"""手机上报接收器：载荷校验、HTTP 边界、幂等与限流。

不起真网络：服务监听 127.0.0.1:0（随机端口），客户端用 http.client。
"""

from __future__ import annotations

import http.client
import json
import logging
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric import clock, ingest, store  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.errors import ConfigError, ProtocolError  # noqa: E402
from rest_electric.models import Reading  # noqa: E402

KEY = "k" * 32
NOW = datetime(2026, 10, 22, 17, 0, 0, tzinfo=timezone(timedelta(hours=8)))

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
jitter_minutes = 0

[http]
timeout_seconds = 5
attempts = 1
user_agent = "test"

[api]
mode = "fixture"
base_url = ""
balance_path = ""
method = "POST"
balance_field = "data.restElecDegree"
balance_unit = "yuan"
capture_file = "{sample}"

[ingest]
bind = "127.0.0.1"
port = 8787
allow_cidr = ["127.0.0.1/32"]
max_skew_seconds = 900
stale_hours = 30
max_delta_cents = 2000

[notify]
channel = "none"
include_history = false
"""


class IngestTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("ingest")
        self.dir = self._tmp.path
        sample = Path(__file__).resolve().parent / "fixtures" / "sample_reading.json"
        self.config = load_config(
            self._write(
                CONFIG.format(sample=sample.as_posix(), db=(self.dir / "test.db").as_posix()),
                "config.toml",
            )
        )
        self._interval = ingest.MIN_INTERVAL_SECONDS
        ingest.MIN_INTERVAL_SECONDS = 0.0
        self.logger = logging.getLogger("rest_electric.test")
        self.server = None

    def tearDown(self) -> None:
        ingest.MIN_INTERVAL_SECONDS = self._interval
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        self._tmp.cleanup()

    def _write(self, text: str, name: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8")
        return path

    # -- 服务 --------------------------------------------------------------

    def _record(self, reading: Reading) -> dict:
        """与 cli.cmd_serve_ingest 里的回调等价的最小实现。"""
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            if ingest.recent_duplicate(self.config, conn, reading) is not None:
                return {"inserted": False, "duplicate": True}
            reason = ingest.implausible(self.config, conn, reading)
            if reason:
                raise ProtocolError(reason)
            inserted = store.insert_reading(conn, reading, self.config.keep_raw)
            return {"inserted": inserted}
        finally:
            conn.close()

    def _start(self, record=None) -> int:
        self.server = ingest.make_server(
            self.config,
            record or self._record,
            logger=self.logger,
            bind="127.0.0.1",
            port=0,
            key=KEY,
        )
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return int(self.server.server_address[1])

    def _post(self, port: int, payload, *, key: str = KEY, path: str = "/ingest", raw: bytes | None = None):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            body = raw if raw is not None else json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": "application/json"}
            if key is not None:
                headers["X-Ingest-Key"] = key
            conn.request("POST", path, body=body, headers=headers)
            response = conn.getresponse()
            data = response.read()
            try:
                parsed = json.loads(data.decode("utf-8"))
            except Exception:
                parsed = {"_raw": data}
            return response.status, parsed
        finally:
            conn.close()


class ParsePayloadTest(IngestTestBase):
    def test_yuan_and_cent(self) -> None:
        reading = ingest.parse_payload(self.config, {"balance": "24.03"}, now=NOW)
        self.assertEqual(reading.balance_cents, 2403)
        self.assertEqual(reading.source, "phone")

        reading = ingest.parse_payload(self.config, {"balance": 2403, "unit": "cent"}, now=NOW)
        self.assertEqual(reading.balance_cents, 2403)

    def test_room_key_defaults_to_config(self) -> None:
        reading = ingest.parse_payload(self.config, {"balance": 1}, now=NOW)
        self.assertEqual(reading.room_key, "12345678")

    def test_other_room_rejected(self) -> None:
        with self.assertRaises(PermissionError):
            ingest.parse_payload(self.config, {"balance": 1, "room_key": "999"}, now=NOW)

    def test_missing_balance(self) -> None:
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"room_key": "12345678"}, now=NOW)

    def test_bad_unit(self) -> None:
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"balance": 1, "unit": "kg"}, now=NOW)

    def test_out_of_range(self) -> None:
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"balance": "123456"}, now=NOW)
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"balance": "-1"}, now=NOW)

    def test_bad_source(self) -> None:
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"balance": 1, "source": "telepathy"}, now=NOW)

    def test_phone_clock_skew_uses_server_time(self) -> None:
        reading = ingest.parse_payload(
            self.config,
            {"balance": 1, "captured_at": "2026-10-20T01:00:00+08:00"},
            now=NOW,
        )
        self.assertEqual(reading.captured_at, clock.iso(NOW))
        self.assertEqual(reading.raw["phone_captured_at"], "2026-10-20T01:00:00+08:00")

    def test_close_time_is_kept(self) -> None:
        reading = ingest.parse_payload(
            self.config,
            {"balance": 1, "captured_at": "2026-10-22T16:59:00+08:00"},
            now=NOW,
        )
        self.assertEqual(reading.captured_at, "2026-10-22T16:59:00+08:00")

    def test_bad_timestamp(self) -> None:
        with self.assertRaises(ProtocolError):
            ingest.parse_payload(self.config, {"balance": 1, "captured_at": "昨天"}, now=NOW)


class ImplausibleTest(IngestTestBase):
    def test_first_reading_is_fine(self) -> None:
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            reading = ingest.parse_payload(self.config, {"balance": "24.03"}, now=NOW)
            self.assertEqual(ingest.implausible(self.config, conn, reading), "")
        finally:
            conn.close()

    def test_big_jump_is_rejected(self) -> None:
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            store.insert_reading(conn, Reading("12345678", 2403, clock.iso(NOW)), True)
            reading = ingest.parse_payload(self.config, {"balance": "69.41"}, now=NOW)
            self.assertIn("相差", ingest.implausible(self.config, conn, reading))
        finally:
            conn.close()


class StaleTest(IngestTestBase):
    def test_no_reading(self) -> None:
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            self.assertIsNone(ingest.stale_age_hours(self.config, conn))
        finally:
            conn.close()

    def test_age(self) -> None:
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            old = clock.iso(clock.now() - timedelta(hours=40))
            store.insert_reading(conn, Reading("12345678", 100, old), True)
            age = ingest.stale_age_hours(self.config, conn)
            self.assertIsNotNone(age)
            self.assertGreater(age, 39)
        finally:
            conn.close()


class ServerTest(IngestTestBase):
    def test_missing_key_refuses_to_start(self) -> None:
        with self.assertRaises(ConfigError):
            ingest.make_server(self.config, self._record, logger=self.logger, key="short")

    def test_health(self) -> None:
        port = self._start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(json.loads(response.read().decode())["ok"])
        finally:
            conn.close()

    def test_rejects_bad_key(self) -> None:
        port = self._start()
        status, body = self._post(port, {"balance": "24.03"}, key="x" * 32)
        self.assertEqual(status, 401)
        self.assertFalse(body["ok"])

    def test_rejects_other_room(self) -> None:
        port = self._start()
        status, _ = self._post(port, {"balance": "24.03", "room_key": "999"})
        self.assertEqual(status, 403)

    def test_rejects_bad_json(self) -> None:
        port = self._start()
        status, _ = self._post(port, None, raw=b"{not json")
        self.assertEqual(status, 400)

    def test_rejects_oversize(self) -> None:
        port = self._start()
        status, _ = self._post(port, None, raw=b"{" + b" " * ingest.MAX_BODY_BYTES + b"}")
        self.assertEqual(status, 413)

    def test_writes_reading_and_is_idempotent(self) -> None:
        port = self._start()
        payload = {"balance": "24.03", "captured_at": "2026-10-22T16:00:00+08:00", "source": "phone"}

        status, body = self._post(port, payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["inserted"])

        status, body = self._post(port, payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["inserted"])

        conn = store.connect(self.config.db_path)
        try:
            rows = conn.execute("SELECT balance_cents, source FROM readings").fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["balance_cents"], 2403)
        self.assertEqual(rows[0]["source"], "phone")

    def test_retry_with_skewed_clock_is_deduped(self) -> None:
        """手机时钟偏太多时 captured_at 会被服务器替换，重试也不能插出重复行。"""
        port = self._start()
        payload = {"balance": "12.34", "captured_at": "2026-10-20T01:00:00+08:00"}

        status, body = self._post(port, payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["inserted"])

        status, body = self._post(port, payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["inserted"])

        conn = store.connect(self.config.db_path)
        try:
            count = conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(count, 1)

    def test_implausible_returns_422(self) -> None:
        conn = store.connect(self.config.db_path)
        try:
            store.init_db(conn)
            store.insert_reading(conn, Reading("12345678", 100, clock.iso(clock.now())), True)
        finally:
            conn.close()

        port = self._start()
        status, body = self._post(port, {"balance": "80.00"})
        self.assertEqual(status, 422)
        self.assertIn("相差", body["error"])

    def test_rate_limited(self) -> None:
        ingest.MIN_INTERVAL_SECONDS = 5.0
        port = self._start()
        self._post(port, {"balance": "1.00"})
        status, _ = self._post(port, {"balance": "2.00"})
        self.assertEqual(status, 429)


if __name__ == "__main__":
    unittest.main()
