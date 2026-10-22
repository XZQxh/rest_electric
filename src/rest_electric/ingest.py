"""手机采集端上报服务（纯标准库 http.server）。

手机上的自动化脚本（AutoX.js 或 Pi 驱动的 adb）把「小程序页面上读到的余额」
POST 过来。本模块只做四件事：

    来源 IP 白名单 → 共享密钥 → 载荷校验 → 交给回调落库

落库之后的预测与告警完全复用 `cli._record_and_alert`，本模块不碰数据库语义。

设计约束（与项目其他部分一致）：
  * 零第三方依赖，只用标准库；
  * 只监听局域网，默认只允许 127.0.0.1；
  * 请求体上限 4 KB、限流 5 秒 1 次、不打印任何密钥；
  * 幂等交给 `readings` 表的 UNIQUE(room_key, captured_at)，重试必须带同一个 captured_at。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from . import clock, store
from .config import Config
from .errors import ConfigError, ProtocolError
from .models import Reading, to_cents, yuan

MAX_BODY_BYTES = 4096
MIN_INTERVAL_SECONDS = 5.0
#: 余额上限 10000 元，下限 0 元；超出直接拒绝（明显是读到了别的数字）
MIN_CENTS = 0
MAX_CENTS = 1_000_000

RecordFn = Callable[[Reading], dict[str, Any]]


# ---------------------------------------------------------------- 载荷校验


def _parse_captured_at(
    value: Any, config: Config, now: datetime
) -> tuple[str, str | None]:
    """返回 (入库用的时间, 被服务器替换掉的手机原始时间或 None)。

    手机时钟可能不准：偏移超过 max_skew_seconds 时以服务器时间为准，
    并把手机报上来的时间留在 raw 里，方便事后核对。
    """
    if not value:
        return clock.iso(now), None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise ProtocolError(f"captured_at 不是合法的 ISO 8601 时间：{value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.TZ)
    if abs((now - moment).total_seconds()) > config.ingest_max_skew_seconds:
        return clock.iso(now), clock.iso(moment)
    return clock.iso(moment), None


def parse_payload(config: Config, payload: Any, *, now: datetime | None = None) -> Reading:
    """把手机上报的 JSON 变成 Reading。任何不合法都抛 ProtocolError（对应 HTTP 400）。"""
    if not isinstance(payload, dict):
        raise ProtocolError("请求体必须是 JSON 对象")

    room_key = str(payload.get("room_key") or config.room_key).strip()
    if room_key != config.room_key:
        raise PermissionError(f"只接收本房间 {config.room_key} 的读数")

    if "balance" not in payload:
        raise ProtocolError("缺少 balance 字段")

    unit = str(payload.get("unit") or config.balance_unit).lower()
    if unit not in {"yuan", "cent"}:
        raise ProtocolError(f"unit 只能是 yuan 或 cent，当前为 {unit!r}")
    cents = to_cents(payload["balance"], unit)
    if not (MIN_CENTS <= cents <= MAX_CENTS):
        raise ProtocolError(
            f"余额 {yuan(cents)} 元超出合理范围"
            f"（{yuan(MIN_CENTS)}–{yuan(MAX_CENTS)} 元），疑似读到了别的数字"
        )

    source = str(payload.get("source") or "phone").strip().lower()
    if source not in {"phone", "adb"}:
        raise ProtocolError(f"source 只能是 phone 或 adb，当前为 {source!r}")

    captured_at, replaced = _parse_captured_at(
        payload.get("captured_at"), config, now or clock.now()
    )

    raw: dict[str, Any] = {"ingest": True, "source": source}
    for key in ("client_id", "device", "page"):
        if payload.get(key) is not None:
            raw[key] = str(payload[key])[:120]
    if replaced is not None:
        raw["phone_captured_at"] = replaced

    return Reading(
        room_key=room_key,
        balance_cents=cents,
        captured_at=captured_at,
        source=source,
        raw=raw,
    )


def implausible(config: Config, conn: Any, reading: Reading) -> str:
    """与上一条读数差得离谱时返回原因；正常返回空串。"""
    last = store.last_reading(conn, config.room_key)
    if last is None:
        return ""
    delta = abs(reading.balance_cents - last.balance_cents)
    if delta > config.ingest_max_delta_cents:
        return (
            f"与上一条读数（{last.balance_yuan} 元）相差 {yuan(delta)} 元，"
            f"超过上限 {yuan(config.ingest_max_delta_cents)} 元；"
            "若确认是充值或大额用电，用 `ingest --force` 补录"
        )
    return ""


DUPLICATE_WINDOW_MINUTES = 10


def recent_duplicate(
    config: Config, conn: Any, reading: Reading, minutes: int = DUPLICATE_WINDOW_MINUTES
) -> Reading | None:
    """同一房间、同一金额、时间相近的已有读数，视为重试重复。

    为什么不能只靠 UNIQUE(room_key, captured_at)：手机时钟偏差超过阈值时，
    服务端会用服务器时间替换 captured_at，重试就会得到不同的时间戳，
    从而插出重复行。这里按「金额 + 时间窗」兜底。
    """
    row = conn.execute(
        "SELECT captured_at, balance_cents, source FROM readings "
        "WHERE room_key = ? AND balance_cents = ? ORDER BY captured_at DESC LIMIT 1",
        (reading.room_key, reading.balance_cents),
    ).fetchone()
    if row is None:
        return None
    try:
        moment = datetime.fromisoformat(row["captured_at"])
        target = datetime.fromisoformat(reading.captured_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.TZ)
    if target.tzinfo is None:
        target = target.replace(tzinfo=clock.TZ)
    if abs((target - moment).total_seconds()) > minutes * 60:
        return None
    return Reading(
        room_key=reading.room_key,
        balance_cents=int(row["balance_cents"]),
        captured_at=row["captured_at"],
        source=row["source"],
    )


def stale_age_hours(config: Config, conn: Any) -> float | None:
    """距离最后一条读数多少小时；没有任何读数返回 None。"""
    last = store.last_reading(conn, config.room_key)
    if last is None:
        return None
    try:
        moment = datetime.fromisoformat(last.captured_at)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=clock.TZ)
    return (clock.now() - moment).total_seconds() / 3600.0


# ---------------------------------------------------------------- HTTP 服务


class IngestServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        config: Config,
        key: str,
        record: RecordFn,
        logger: logging.Logger,
    ) -> None:
        super().__init__(address, _Handler)
        self.config = config
        self.key = key
        self.record = record
        self.log = logger
        self.networks = tuple(
            ipaddress.ip_network(item, strict=False) for item in config.ingest_allow_cidr
        )
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def allowed(self, ip_text: str) -> bool:
        try:
            address = ipaddress.ip_address(ip_text)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        return any(address in network for network in self.networks)

    def rate_limited(self, ip_text: str) -> bool:
        now = time.monotonic()
        with self._lock:
            previous = self._last_seen.get(ip_text)
            if previous is not None and now - previous < MIN_INTERVAL_SECONDS:
                return True
            self._last_seen[ip_text] = now
            return False


class _Handler(BaseHTTPRequestHandler):
    server: IngestServer  # type: ignore[assignment]
    server_version = "rest-electric-ingest"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 15

    # -- 工具 ------------------------------------------------------------

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 基类签名
        self.server.log.debug("ingest %s - %s", self.client_address[0], format % args)

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        provided = self.headers.get("x-ingest-key", "")
        expected = self.server.key
        return bool(provided) and hmac.compare_digest(
            provided.encode("utf-8"), expected.encode("utf-8")
        )

    # -- 路由 ------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - 基类约定
        if self.path.split("?")[0] == "/health":
            self._send(200, {"ok": True, "service": "rest-electric-ingest"})
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - 基类约定
        server = self.server
        ip_text = self.client_address[0]

        if not server.allowed(ip_text):
            server.log.warning("拒绝来源 IP：%s（不在 allow_cidr 内）", ip_text)
            self._send(403, {"ok": False, "error": "来源 IP 不在白名单内"})
            return
        if not self._auth_ok():
            server.log.warning("拒绝来源 IP：%s（密钥错误）", ip_text)
            self._send(401, {"ok": False, "error": "密钥错误"})
            return
        if server.rate_limited(ip_text):
            self._send(429, {"ok": False, "error": "请求过于频繁"})
            return
        if self.path.split("?")[0] != "/ingest":
            self._send(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            self._send(400, {"ok": False, "error": "Content-Length 非法"})
            return
        if length <= 0:
            self._send(400, {"ok": False, "error": "请求体为空"})
            return
        if length > MAX_BODY_BYTES:
            self._send(413, {"ok": False, "error": f"请求体超过 {MAX_BODY_BYTES} 字节"})
            return

        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
            return

        try:
            reading = parse_payload(server.config, payload)
        except PermissionError as exc:
            self._send(403, {"ok": False, "error": str(exc)})
            return
        except ProtocolError as exc:
            self._send(400, {"ok": False, "error": str(exc)})
            return

        try:
            result = server.record(reading)
        except ProtocolError as exc:
            server.log.warning("读数未入库：%s", exc)
            self._send(422, {"ok": False, "error": str(exc)})
            return
        except Exception as exc:  # pragma: no cover - 兜底，避免把堆栈回给客户端
            server.log.exception("处理上报失败")
            self._send(500, {"ok": False, "error": f"服务端错误：{type(exc).__name__}"})
            return

        payload_out = {"ok": True, "balance": reading.balance_yuan}
        payload_out.update(result)
        self._send(200, payload_out)


def make_server(
    config: Config,
    record: RecordFn,
    *,
    logger: logging.Logger,
    bind: str | None = None,
    port: int | None = None,
    key: str | None = None,
) -> IngestServer:
    """构造服务但不启动（测试用）。"""
    secret = key or config.secret("ingest", "key")
    if not secret or len(secret) < 16:
        raise ConfigError(
            "ingest 需要至少 16 位的共享密钥：请在 config/secrets.toml 的 [ingest].key 填入"
        )
    address = (bind or config.ingest_bind, port if port is not None else config.ingest_port)
    return IngestServer(address, config=config, key=secret, record=record, logger=logger)


def serve(
    config: Config,
    record: RecordFn,
    *,
    logger: logging.Logger,
    bind: str | None = None,
    port: int | None = None,
    key: str | None = None,
) -> None:
    """阻塞运行，直到 Ctrl-C / SIGTERM。"""
    server = make_server(config, record, logger=logger, bind=bind, port=port, key=key)
    host, actual_port = server.server_address[0], server.server_address[1]
    logger.info(
        "ingest 已启动：http://%s:%s/ingest（允许来源：%s）",
        host,
        actual_port,
        "、".join(config.ingest_allow_cidr),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("ingest 收到中断，退出")
    finally:
        server.server_close()
