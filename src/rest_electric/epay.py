"""网页版电费查询客户端（`/epay/h5/eleresult`）。

学校网页端（办事大厅 → 一卡通 → 电费）是一条**不依赖微信**的链路：

    1. GET /epay/h5/eleresult?sysid=..&roomid=..&areaid=..&buildid=..
         - 已登录：200，HTML 里有「剩余电量 23.57度」
         - 未登录：302 → https://uis.shou.edu.cn/cas/login?service=<epay 回调>
    2. 未登录时：从 CAS 登录页取 hidden `execution`，用 `/cas/jwt/publicKey`
       做 RSA(PKCS#1 v1.5) 加密密码，POST 学号/密码
    3. CAS 回调 /epay/j_spring_cas_security_check?ticket=... → 302 回 eleresult

本模块**只发 GET 查询**和必要的一次登录 POST；绝不调用充值接口（`doNext` 那条 POST
是有真实扣款副作用的）。

口径说明：页面显示的是「剩余电量 xx 度」，学校 1 度 = 1 元，所以按「元」入库，
报表和阈值口径与小程序来源保持一致。
"""

from __future__ import annotations

import base64
import http.cookiejar
import logging
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request

from . import clock
from .config import Config
from .errors import AuthExpiredError, ConfigError, ProtocolError, TransportError
from .models import Reading, to_cents

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"
)
TIMEOUT = 20.0
COOKIE_FILE = "epay-cookies.txt"

_AMOUNT_RE = re.compile(
    r"剩余电量\s*</label>\s*</div>\s*<div[^>]*>\s*([0-9]+(?:\.[0-9]+)?)\s*度", re.S
)
_LEFT_DEGREE_RE = re.compile(r'left-degree="([0-9]+(?:\.[0-9]+)?)"')
_ROOM_RE = re.compile(r"房间\s*</div>\s*<div[^>]*>\s*([^\s<]+)\s*</div>", re.S)
_EXECUTION_RE = re.compile(r'name="execution"\s+value="([^"]+)"')


# ---------------------------------------------------------------- RSA


def _read_der(data: bytes, offset: int) -> tuple[int, bytes, int]:
    tag = data[offset]
    length = data[offset + 1]
    offset += 2
    if length & 0x80:
        count = length & 0x7F
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    return tag, data[offset : offset + length], offset + length


def parse_public_key(pem: str) -> tuple[int, int]:
    """从 SPKI PEM 里取出 (n, e)。只支持 RSA。"""
    body = "".join(line for line in pem.strip().splitlines() if not line.startswith("-----"))
    try:
        der = base64.b64decode(body)
    except Exception as exc:  # pragma: no cover - 数据损坏
        raise ProtocolError(f"公钥不是合法 base64：{exc}") from exc

    _, spki, _ = _read_der(der, 0)
    _, algorithm, offset = _read_der(spki, 0)
    _, bitstring, _ = _read_der(spki, offset)
    if not bitstring or bitstring[0] != 0:
        raise ProtocolError("公钥 BIT STRING 格式异常")

    _, rsa_pub, _ = _read_der(bitstring[1:], 0)
    _, modulus, next_offset = _read_der(rsa_pub, 0)
    _, exponent, _ = _read_der(rsa_pub, next_offset)

    oid = algorithm[2:11] if len(algorithm) > 2 else b""
    if oid != bytes.fromhex("2a864886f70d010101"):
        raise ProtocolError("公钥不是 RSA（OID 不匹配）")
    return int.from_bytes(modulus, "big"), int.from_bytes(exponent, "big")


def rsa_encrypt(pem: str, message: bytes) -> str:
    """PKCS#1 v1.5 加密，返回 base64（jsencrypt 默认填充）。"""
    n, e = parse_public_key(pem)
    size = (n.bit_length() + 7) // 8
    if len(message) > size - 11:
        raise ConfigError("密码太长，超出 RSA 单块上限")
    padding = bytes(byte or 1 for byte in secrets.token_bytes(size - len(message) - 3))
    block = b"\x00\x02" + padding + b"\x00" + message
    cipher = pow(int.from_bytes(block, "big"), e, n)
    return base64.b64encode(cipher.to_bytes(size, "big")).decode("ascii")


# ---------------------------------------------------------------- HTML


def extract_execution(html: str) -> str:
    """取 CAS 密码登录用的 execution（藏在 password 输入框之后）。"""
    index = html.find('name="password"')
    if index < 0:
        raise ProtocolError("CAS 登录页里找不到密码表单（页面结构可能变了）")
    match = _EXECUTION_RE.search(html, index)
    if not match:
        raise ProtocolError("CAS 登录页里找不到 execution")
    return match.group(1)


def parse_result(html: str) -> tuple[str, str]:
    """返回 (房间号, 剩余电量字符串)。"""
    amount = _AMOUNT_RE.search(html) or _LEFT_DEGREE_RE.search(html)
    if not amount:
        raise ProtocolError("结果页里找不到「剩余电量」")
    room = _ROOM_RE.search(html)
    return (room.group(1) if room else ""), amount.group(1)


def looks_like_login_page(html: str) -> bool:
    return 'name="password"' in html and "剩余电量" not in html


# ---------------------------------------------------------------- 客户端


class EpayClient:
    """网页版查询。`once` 与它对接口一致：fetch() -> Reading。"""

    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._log = logger or logging.getLogger(__name__)
        self._cookie_path = config.db_path.parent / COOKIE_FILE
        self._jar = http.cookiejar.MozillaCookieJar(str(self._cookie_path))
        self._load_cookies()
        self._opener = self._build_opener()

    # -- 基础 ------------------------------------------------------------

    def _build_opener(self) -> urllib.request.OpenerDirector:
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self._jar))
        opener.addheaders = [("User-Agent", UA), ("Accept-Language", "zh-CN,zh;q=0.9")]
        return opener

    def _load_cookies(self) -> None:
        if not self._cookie_path.exists():
            return
        try:
            self._jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as exc:  # 文件损坏不该让查询失败
            self._log.debug("读取 %s 失败：%s", self._cookie_path, exc)

    def _save_cookies(self) -> None:
        try:
            self._cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self._jar.save(ignore_discard=True, ignore_expires=True)
            if os.name == "posix":
                os.chmod(self._cookie_path, 0o600)
        except Exception as exc:
            self._log.warning("保存登录状态失败（不影响本次查询）：%s", exc)

    def _open(
        self, url: str, data: dict[str, str] | None = None, headers: dict[str, str] | None = None
    ) -> tuple[str, str]:
        """返回 (响应正文, 最终 URL)。"""
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        request = urllib.request.Request(url, data=body, method="POST" if body else "GET")
        if body:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self._opener.open(request, timeout=TIMEOUT) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, "replace"), response.geturl()
        except urllib.error.HTTPError as exc:
            raise ProtocolError(f"网页端返回 HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise TransportError(f"访问网页端失败：{exc}") from exc

    def _dump_login_failure(self, html: str, final_url: str) -> str:
        """把登录失败的响应落盘，方便排查（可能含学号，权限 600）。"""
        path = self._config.db_path.parent / "epay-login-failed.html"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(html, encoding="utf-8")
            if os.name == "posix":
                os.chmod(path, 0o600)
            self._log.error("登录失败的响应已保存：%s（最终地址 %s）", path, final_url.split("?")[0])
        except Exception as exc:  # pragma: no cover - 落盘失败不影响报错
            self._log.warning("保存登录失败响应失败：%s", exc)
        return str(path)

    # -- 业务 ------------------------------------------------------------

    def _result_url(self) -> str:
        config = self._config
        query = urllib.parse.urlencode(
            {
                "sysid": config.epay_sysid,
                "roomid": config.room,
                "areaid": config.epay_areaid,
                "buildid": config.epay_buildid,
            }
        )
        return f"{config.epay_base_url.rstrip('/')}{config.epay_result_path}?{query}"

    def _credentials(self) -> tuple[str, str]:
        username = self._config.secret("epay", "username").strip()
        password = self._config.secret("epay", "password")
        if not username or not password:
            raise ConfigError(
                "网页版查询需要登录：请在 config/secrets.toml 里填 [epay].username 与 [epay].password"
            )
        return username, password

    def _login(self, login_page: str, login_url: str) -> None:
        username, password = self._credentials()
        execution = extract_execution(login_page)

        cas_root = "{0.scheme}://{0.netloc}".format(urllib.parse.urlsplit(login_url))
        pem, _ = self._open(f"{cas_root}/cas/jwt/publicKey")
        # 学校的登录页会给密文加 "__RSA__" 前缀（页面 JS: "__RSA__" + encrypt.encrypt(pwd)），
        # 少了这个前缀，服务端会当成明文密码，然后**静默**退回登录页（不报密码错误）。
        encrypted = "__RSA__" + rsa_encrypt(pem, password.encode("utf-8"))

        form = {
            "username": username,
            "password": encrypted,
            "captcha": "",
            "mfaState": "",
            "currentMenu": "1",
            "failN": "-1",
            "execution": execution,
            "_eventId": "submit",
            "geolocation": "",
            "fpVisitorId": secrets.token_hex(16),
            "submit": "Login1_m",
        }
        self._log.info("网页端未登录，正在用 CAS 登录（学号 %s）", username[:3] + "***")
        html, final_url = self._open(
            login_url,
            form,
            headers={
                "Referer": login_url,
                "Origin": cas_root,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        if looks_like_login_page(html):
            path = self._dump_login_failure(html, final_url)
            raise AuthExpiredError(
                "CAS 登录失败：学校返回的仍然是登录页。"
                f"响应已保存到 {path}，里面通常有具体原因（密码错/验证码/风控）。"
            )
        self._log.info("CAS 登录成功，已保存会话")
        self._save_cookies()
        if "剩余电量" not in html:
            self._log.debug("登录后落在 %s，再取一次结果页", final_url.split("?")[0])

    def fetch(self) -> Reading:
        html, final_url = self._open(self._result_url())

        if looks_like_login_page(html):
            self._login(html, final_url)
            html, final_url = self._open(self._result_url())
            if looks_like_login_page(html):
                raise AuthExpiredError("登录后仍然拿不到结果页，可能是权限或页面结构变化")

        room, amount = parse_result(html)
        expected = str(self._config.room).strip()
        if room and room != expected:
            raise ProtocolError(f"结果页的房间是 {room}，不是配置里的 {expected}，拒绝入库")

        cents = to_cents(amount, "yuan")
        self._log.info("网页端读数：%s 度（房间 %s）", amount, room or expected)
        return Reading(
            room_key=self._config.room_key,
            balance_cents=cents,
            captured_at=clock.iso(),
            source="epay",
            raw={"epay": True, "room": room or expected, "degree": amount},
        )
