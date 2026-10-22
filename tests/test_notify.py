"""notify：用本地假服务端断言请求形态，不访问外网。"""

from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rest_electric.errors import NotifierError  # noqa: E402
from rest_electric.notify import (  # noqa: E402
    NullNotifier,
    PushPlusNotifier,
    ServerChanNotifier,
)


class _Handler(BaseHTTPRequestHandler):
    received: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        type(self).received.append({"path": self.path, "body": body})
        payload = {"code": 200, "msg": "ok"} if self.path.endswith("/send") else {"code": 0}
        data = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # 静音
        pass


class NotifyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        _Handler.received.clear()

    def test_serverchan_payload(self) -> None:
        ServerChanNotifier("SCTtest", endpoint=self.base, attempts=1).send("余额偏低", "正文")
        self.assertEqual(len(_Handler.received), 1)
        item = _Handler.received[0]
        self.assertEqual(item["path"], "/SCTtest.send")
        form = urllib.parse.parse_qs(item["body"])
        self.assertEqual(form["title"], ["余额偏低"])
        self.assertEqual(form["desp"], ["正文"])

    def test_pushplus_payload(self) -> None:
        PushPlusNotifier("tok123", endpoint=self.base, attempts=1).send("标题", "内容")
        item = _Handler.received[0]
        self.assertEqual(item["path"], "/send")
        payload = json.loads(item["body"])
        self.assertEqual(payload["token"], "tok123")
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["content"], "内容")

    def test_missing_credentials(self) -> None:
        with self.assertRaises(NotifierError):
            ServerChanNotifier("")
        with self.assertRaises(NotifierError):
            PushPlusNotifier("")

    def test_null_notifier_does_not_post(self) -> None:
        NullNotifier().send("标题", "内容")
        self.assertEqual(_Handler.received, [])


if __name__ == "__main__":
    unittest.main()
