"""网页版电费查询：RSA、CAS 登录回环、结果解析、会话复用。

用本地假服务器模拟「未登录 302 → CAS → 回调 → 结果页」的整条链路，
不联学校、不碰真实账号。
"""

from __future__ import annotations

import base64
import http.server
import sys
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _tmp import WorkspaceTmpDir  # noqa: E402
from rest_electric import epay  # noqa: E402
from rest_electric.config import load_config  # noqa: E402
from rest_electric.errors import AuthExpiredError, ProtocolError  # noqa: E402

# 学校 CAS 用的公钥（公开信息，2048 位），用来验证 DER 解析
REAL_PEM = """-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAnVU2alU82jLfzPjJfMax0Y4hv2BUT5lDUx/vt4K6
5d8bIN24nBkLRuzyT9hnRRp9HkKXCDlKfDrL3ZxLFj1XYGWw9DXzMBqEKTId7ZI9e0LwCDS4h6bUPgGko8v4E
lQjjqvsyiNjbN6oVijurpwRUO84eNDeXMfBP2weRMyYj+v0aUJUsx1P9eIHBAeMwzEnk+CfqwQs1O2+iJHXLje
Wc+fMp5y4rQFLbvHx99hDVBx0Jak+MUplyNjbnxva4y8sC1yAzgoo8ZPFi4YqvJQio7qqI9rog/tvBM6Z0526W+4
0bvixYu0r+41YvRVl3ldztr5VX3vGZ5B4v10dSOOfbwIDAQAB
-----END PUBLIC KEY-----"""

# 测试专用 1024 位密钥（仅用于单测，不是任何真实凭据）
TEST_N = int(
    "8380981107860883120228251571600172292125845356193000266679498904279938580747070044340712021606"
    "8260038974486787502249080106890383135058284962657901460264416901478263797636602410112035546207"
    "4216675817834156548121892407242727871608352088479245380522207391769904257950769637157442141349"
    "13197084633026767314716497"
)
TEST_E = 65537
TEST_D = int(
    "1107583157226957424116100628067032244718280461876307662995119398354647726442320729573140467508"
    "9906468064727254322855475270546067234275902254628379152957107418370295802576718391243040916687"
    "7730070616114950511791071556111562746639679712570858628247929826062833412875567198562806944678"
    "66095794550045544547465781"
)
TEST_PEM = """-----BEGIN PUBLIC KEY-----
MIGeMA0GCSqGSIb3DQEBAQUAA4GMADCBiAKBgHdZYMFdQ+PUA5kKvND+7OBEwbal
9KRQKGf2PG9T17omUDn6OZPHHmSn7Ij+P3ITfgCInmmGlyJY5vETANXPu90bnI4R
gScoddvLiaoQPnzJ6etK6QXhKbuKNfbl8xly1ZSHGQ0cnaSvHDKT+qUyYeCmHbKo
i44OFmHMSiMOC4dRAgMBAAE=
-----END PUBLIC KEY-----"""

LOGIN_PAGE = """<html><body>
<input type="hidden" name="username" value=""/>
<input type="hidden" name="password" value=""/>
<input type="hidden" name="captcha" value=""/>
<input type="hidden" name="currentMenu" value="1"/>
<input type="hidden" name="failN" value="-1"/>
<input type="hidden" name="execution" value="EXEC-TEST-123"/>
<input type="hidden" name="_eventId" value="submit"/>
</body></html>"""

RESULT_PAGE = """<html><head><title>查询结果</title></head><body>
<div class="weui-cells" style="margin: 0px;">
    <div class="weui-cell">
        <div class="weui-cell__bd">
            房间
        </div>
        <div >12345678</div>
    </div>
</div>
<div class="weui-cells">
    <div class="weui-cell">
        <div class="weui-cell__bd"><label class="weui-label">剩余电量</label></div>
        <div >
            23.57度
        </div>
    </div>
</div>
<input class="weui-input" type="text" id="roomdef" left-degree="23.57"/>
</body></html>"""


def decrypt(cipher_b64: str) -> bytes:
    """服务端收到的密码是 "__RSA__" + base64(RSA(...))，这里还原明文。"""
    if cipher_b64.startswith("__RSA__"):
        cipher_b64 = cipher_b64[len("__RSA__") :]
    size = (TEST_N.bit_length() + 7) // 8
    block = pow(int.from_bytes(base64.b64decode(cipher_b64), "big"), TEST_D, TEST_N).to_bytes(size, "big")
    start = block.find(b"\x00", 2)
    return block[start + 1 :]


class FakeServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, *, password: str, room: str, result_page: str) -> None:
        super().__init__(address, Handler)
        self.password = password
        self.room = room
        self.result_page = result_page
        self.logins = 0
        self.requests: list[str] = []

    @property
    def base(self) -> str:
        host, port = self.server_address[0], self.server_address[1]
        return f"http://{host}:{port}"


class Handler(http.server.BaseHTTPRequestHandler):
    server: FakeServer
    protocol_version = "HTTP/1.0"

    def log_message(self, *args) -> None:  # 静音
        pass

    def _send(self, status: int, body: str = "", headers: dict[str, str] | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _authed(self) -> bool:
        return "JSESSIONID=ok" in (self.headers.get("Cookie") or "")

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        self.server.requests.append("GET " + path)

        if path == "/epay/h5/eleresult":
            if self._authed():
                self._send(200, self.server.result_page)
            else:
                service = urllib.parse.quote(self.server.base + "/epay/j_spring_cas_security_check", safe="")
                self._send(302, "", {"Location": f"{self.server.base}/cas/login?service={service}"})
            return

        if path == "/cas/login":
            self._send(200, LOGIN_PAGE)
            return

        if path == "/cas/jwt/publicKey":
            self._send(200, TEST_PEM)
            return

        if path == "/epay/j_spring_cas_security_check":
            self._send(
                302,
                "",
                {"Location": f"{self.server.base}/epay/h5/eleresult?sysid=1", "Set-Cookie": "JSESSIONID=ok; Path=/"},
            )
            return

        self._send(404, "not found")

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlsplit(self.path).path
        self.server.requests.append("POST " + path)
        length = int(self.headers.get("Content-Length") or 0)
        form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"))

        if path != "/cas/login":
            self._send(404, "not found")
            return

        password = form.get("password", [""])[0]
        ok = False
        try:
            # 真实服务端要求 "__RSA__" 前缀；少了会静默退回登录页
            ok = password.startswith("__RSA__") and decrypt(password) == self.server.password.encode("utf-8")
        except Exception:
            ok = False

        if not ok or form.get("execution", [""])[0] != "EXEC-TEST-123":
            self._send(200, LOGIN_PAGE)  # 登录失败：留在登录页
            return

        self.server.logins += 1
        service = urllib.parse.quote(self.server.base + "/epay/j_spring_cas_security_check", safe="")
        self._send(
            302,
            "",
            {
                "Location": f"{self.server.base}/epay/j_spring_cas_security_check?ticket=ST-TEST",
                "Set-Cookie": "TGC=test; Path=/cas",
            },
        )


class EpayTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = WorkspaceTmpDir("epay")
        self.dir = self._tmp.path
        self.server = FakeServer(
            ("127.0.0.1", 0), password="secret-pass", room="12345678", result_page=RESULT_PAGE
        )
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.config = self._make_config()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def _make_config(self, password: str = "secret-pass"):
        config_path = self.dir / "config.toml"
        config_path.write_text(
            f"""
[room]
room_key = "12345678"
building = "1"
room = "12345678"

[storage]
db_path = "{(self.dir / 'electric.db').as_posix()}"
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
mode = "epay"
balance_field = "data.restElecDegree"
balance_unit = "yuan"

[epay]
base_url = "{self.server.base}"
result_path = "/epay/h5/eleresult"
sysid = "1"
areaid = "1"
buildid = "1"

[notify]
channel = "none"
""",
            encoding="utf-8",
        )
        secrets_path = self.dir / "secrets.toml"
        secrets_path.write_text(
            f'[epay]\nusername = "20250001"\npassword = "{password}"\n', encoding="utf-8"
        )
        return load_config(config_path, secrets_path)


class RsaTest(EpayTestBase):
    def test_parse_real_public_key(self) -> None:
        n, e = epay.parse_public_key(REAL_PEM)
        self.assertEqual(e, 65537)
        self.assertEqual(n.bit_length(), 2048)

    def test_encrypt_roundtrip(self) -> None:
        encrypted = epay.rsa_encrypt(TEST_PEM, b"secret-pass")
        self.assertEqual(len(base64.b64decode(encrypted)), 128)
        self.assertEqual(decrypt(encrypted), b"secret-pass")

    def test_extract_execution(self) -> None:
        self.assertEqual(epay.extract_execution(LOGIN_PAGE), "EXEC-TEST-123")

    def test_extract_execution_missing(self) -> None:
        with self.assertRaises(ProtocolError):
            epay.extract_execution("<html>no form</html>")


class ParseResultTest(EpayTestBase):
    def test_parse(self) -> None:
        room, amount = epay.parse_result(RESULT_PAGE)
        self.assertEqual(room, "12345678")
        self.assertEqual(amount, "23.57")

    def test_missing_amount(self) -> None:
        with self.assertRaises(ProtocolError):
            epay.parse_result("<html><body>nothing</body></html>")


class FetchTest(EpayTestBase):
    def test_login_then_read(self) -> None:
        client = epay.EpayClient(self.config)
        reading = client.fetch()
        self.assertEqual(reading.balance_cents, 2357)
        self.assertEqual(reading.source, "epay")
        self.assertEqual(reading.room_key, "12345678")
        self.assertEqual(self.server.logins, 1)

    def test_session_reused_without_login(self) -> None:
        epay.EpayClient(self.config).fetch()
        self.assertEqual(self.server.logins, 1)

        second = epay.EpayClient(self.config)  # 新实例，应从 cookie 文件恢复
        reading = second.fetch()
        self.assertEqual(reading.balance_cents, 2357)
        self.assertEqual(self.server.logins, 1, "会话仍然有效时不该再次登录")

    def test_wrong_password(self) -> None:
        config = self._make_config(password="wrong-pass")
        with self.assertRaises(AuthExpiredError):
            epay.EpayClient(config).fetch()

    def test_other_room_rejected(self) -> None:
        self.server.result_page = RESULT_PAGE.replace("12345678", "999999")
        with self.assertRaises(ProtocolError):
            epay.EpayClient(self.config).fetch()

    def test_missing_credentials(self) -> None:
        secrets_path = self.dir / "secrets.toml"
        secrets_path.write_text("[epay]\nusername = \"\"\npassword = \"\"\n", encoding="utf-8")
        config = load_config(self.dir / "config.toml", secrets_path)
        with self.assertRaises(Exception):
            epay.EpayClient(config).fetch()


if __name__ == "__main__":
    unittest.main()
