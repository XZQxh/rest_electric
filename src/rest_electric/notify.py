"""推送通道：Server酱 / PushPlus / SMTP，以及用于干跑的空通道。

三者都实现同一个 send(title, body) 接口。配置缺失或发送失败抛 NotifierError，
由 CLI 决定是否影响退出码（推送失败不应让当天的数据白查）。
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from . import http
from .config import Config
from .errors import NotifierError

SERVERCHAN_ENDPOINT = "https://sctapi.ftqq.com"
PUSHPLUS_ENDPOINT = "https://www.pushplus.plus"


class Notifier(Protocol):
    name: str

    def send(self, title: str, body: str) -> None: ...


class NullNotifier:
    """干跑用：只写日志，不联网。"""

    name = "none"

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._log = logger or logging.getLogger(__name__)

    def send(self, title: str, body: str) -> None:
        self._log.info("[干跑] 不发送推送：%s", title)


class ServerChanNotifier:
    name = "serverchan"

    def __init__(
        self,
        sendkey: str,
        *,
        endpoint: str = SERVERCHAN_ENDPOINT,
        timeout: float = 15.0,
        attempts: int = 2,
    ) -> None:
        if not sendkey:
            raise NotifierError(
                "serverchan 需要 SendKey：请在 config/secrets.toml 的 [notify].sendkey 填入"
            )
        self._sendkey = sendkey
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._attempts = attempts

    def send(self, title: str, body: str) -> None:
        url = f"{self._endpoint}/{self._sendkey}.send"
        response = http.post_form(
            url,
            {"title": title, "desp": body},
            timeout=self._timeout,
            attempts=self._attempts,
            backoff=(2.0, 6.0),
        )
        if response.status != 200:
            raise NotifierError(f"Server酱返回 HTTP {response.status}: {response.text()[:200]}")
        try:
            payload = response.json()
        except Exception as exc:
            raise NotifierError(f"Server酱响应无法解析：{response.text()[:200]}") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (0, "0", None):
            message = payload.get("message", "") if isinstance(payload, dict) else ""
            raise NotifierError(f"Server酱拒绝：code={code} {message}")


class PushPlusNotifier:
    name = "pushplus"

    def __init__(
        self,
        token: str,
        *,
        endpoint: str = PUSHPLUS_ENDPOINT,
        timeout: float = 15.0,
        attempts: int = 2,
    ) -> None:
        if not token:
            raise NotifierError(
                "pushplus 需要 token：请在 config/secrets.toml 的 [notify].pushplus_token 填入"
            )
        self._token = token
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout
        self._attempts = attempts

    def send(self, title: str, body: str) -> None:
        url = f"{self._endpoint}/send"
        response = http.post_json(
            url,
            {
                "token": self._token,
                "title": title,
                "content": body,
                "template": "markdown",
            },
            timeout=self._timeout,
            attempts=self._attempts,
            backoff=(2.0, 6.0),
        )
        if response.status != 200:
            raise NotifierError(f"PushPlus 返回 HTTP {response.status}: {response.text()[:200]}")
        payload = response.json() if response.body else {}
        code = payload.get("code") if isinstance(payload, dict) else None
        if code not in (200, "200", None):
            raise NotifierError(f"PushPlus 拒绝：code={code} {payload.get('msg', '')}")


class SmtpNotifier:
    name = "smtp"

    def __init__(
        self,
        *,
        host: str,
        port: int,
        user: str,
        password: str,
        to: str,
        sender: str = "",
        timeout: float = 20.0,
    ) -> None:
        missing = [
            label
            for label, value in (
                ("smtp_host", host),
                ("smtp_user", user),
                ("smtp_password", password),
                ("smtp_to", to),
            )
            if not value
        ]
        if missing:
            raise NotifierError(f"smtp 通道缺少配置：{'、'.join(missing)}")
        self._host = host
        self._port = int(port)
        self._user = user
        self._password = password
        self._to = to
        self._sender = sender or user
        self._timeout = timeout

    def send(self, title: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = title
        message["From"] = self._sender
        message["To"] = self._to
        message.set_content(body)
        self.send_message(message)

    def send_message(self, message: EmailMessage) -> None:
        """发送一条已经组装好的邮件（日报要用 HTML + 内嵌图，不能走纯文本的 send）。"""
        try:
            if self._port == 465:
                with smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout) as smtp:
                    smtp.login(self._user, self._password)
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as smtp:
                    smtp.starttls()
                    smtp.login(self._user, self._password)
                    smtp.send_message(message)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifierError(f"SMTP 发送失败：{exc}") from exc


def smtp_notifier(config: Config, *, to: str = "") -> SmtpNotifier:
    """按 [notify] 里的 SMTP 配置构造发送器；`to` 可覆盖收件人（日报用）。"""
    raw_port = config.secret("notify", "smtp_port") or "465"
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise NotifierError(f"[notify].smtp_port 不是整数：{raw_port!r}") from exc
    return SmtpNotifier(
        host=config.secret("notify", "smtp_host"),
        port=port,
        user=config.secret("notify", "smtp_user"),
        password=config.secret("notify", "smtp_password"),
        to=to or config.secret("notify", "smtp_to"),
    )


def make_notifier(config: Config, logger: logging.Logger | None = None) -> Notifier:
    channel = config.notify_channel.lower()
    if channel == "none":
        return NullNotifier(logger)
    if channel == "serverchan":
        return ServerChanNotifier(
            config.secret("notify", "sendkey"),
            timeout=config.http_timeout,
            attempts=config.http_attempts,
        )
    if channel == "pushplus":
        return PushPlusNotifier(
            config.secret("notify", "pushplus_token"),
            timeout=config.http_timeout,
            attempts=config.http_attempts,
        )
    if channel == "smtp":
        return smtp_notifier(config)
    raise NotifierError(f"未知推送通道：{channel}")
