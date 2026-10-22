"""★ 唯一与学校接口打交道的文件。

抓包（docs/recon-sop.md）拿到真实请求后，需要改动的地方几乎全在这里：

  1. HttpBalanceClient._build_request —— 请求地址、方法、参数名、请求体形态
  2. HttpBalanceClient._auth_headers —— token 放在哪个请求头、是否要 Cookie/Referer
  3. _looks_like_auth_failure      —— 接口用哪种业务码表示「未登录」
  4. parse_reading                 —— 金额字段路径（已由配置驱动，通常不用改）

其余模块（存储、预测、告警、调度）与协议无关，不要因为抓包结果去改它们。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from . import clock, http
from .config import Config
from .errors import AuthExpiredError, ConfigError, ProtocolError
from .models import Reading, to_cents

# 接口出现这些业务关键词时，判定为登录态失效而不是普通错误
_AUTH_HINTS = ("未登录", "登录失效", "登录已过期", "token失效", "token 失效", "请重新登录", "会话过期")


class BalanceClient(Protocol):
    def fetch(self) -> Reading: ...


def get_path(data: Any, dotted: str) -> Any:
    """按点号路径取值，支持列表下标：'data.records.0.balance'。缺失抛 ProtocolError。"""
    if not dotted:
        raise ProtocolError("未配置金额字段路径（[api].balance_field）")
    current = data
    walked: list[str] = []
    for part in dotted.split("."):
        walked.append(part)
        if isinstance(current, dict):
            if part not in current:
                available = "、".join(list(current)[:8]) or "（空对象）"
                raise ProtocolError(
                    f"响应里找不到字段 {'.'.join(walked)}；该层可用字段：{available}。"
                    f"请对照抓包结果修改 [api].balance_field"
                )
            current = current[part]
        elif isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise ProtocolError(f"字段路径 {'.'.join(walked)} 列表下标无效") from exc
        else:
            raise ProtocolError(f"字段路径 {'.'.join(walked)} 走到了非容器值：{current!r}")
    return current


def parse_reading(
    data: dict[str, Any],
    config: Config,
    source: str = "api",
    captured_at: str | None = None,
) -> Reading:
    """从响应 JSON 解析出读数。字段路径与单位都由配置决定。"""
    value = get_path(data, config.balance_field)
    balance_cents = to_cents(value, config.balance_unit)
    return Reading(
        room_key=config.room_key,
        balance_cents=balance_cents,
        captured_at=captured_at or clock.iso(),
        source=source,
        raw=data,
    )


class FixtureClient:
    """从本地样本读取，用于开发期与干跑。不联网。"""

    def __init__(self, config: Config) -> None:
        self._config = config

    def fetch(self) -> Reading:
        path = self._config.capture_file
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProtocolError(f"读取样本文件失败：{path}：{exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"样本文件不是合法 JSON：{path}：{exc}") from exc
        if not isinstance(data, dict):
            raise ProtocolError(f"样本文件顶层必须是 JSON 对象：{path}")
        return parse_reading(data, self._config, source="fixture")


class HttpBalanceClient:
    """真实接口客户端。抓包后按 TODO 调整。"""

    def __init__(self, config: Config, logger: logging.Logger | None = None) -> None:
        self._config = config
        self._log = logger or logging.getLogger(__name__)

    # ---- 以下三处是抓包后最可能需要修改的地方 ----

    def _build_request(self) -> tuple[str, str, bytes | None]:
        config = self._config
        if not config.base_url or not config.balance_path:
            raise ConfigError(
                "[api].mode=http 需要先填 base_url 与 balance_path（见 docs/recon-sop.md）"
            )
        url = f"{config.base_url}{config.balance_path}"
        params = self._query_params()
        if config.method == "GET":
            if params:
                import urllib.parse

                url = f"{url}?{urllib.parse.urlencode(params)}"
            return url, "GET", None
        return url, "POST", json.dumps(params, ensure_ascii=False).encode("utf-8")

    def _query_params(self) -> dict[str, str]:
        config = self._config
        # 先铺固定字段（如 elcsysid/areaid），再用楼栋/房间覆盖
        params: dict[str, str] = dict(config.extra_body)
        if config.building:
            params[config.building_param] = config.building
        if config.room:
            params[config.room_param] = config.room
        return params

    def _auth_headers(self) -> dict[str, str]:
        config = self._config
        headers = {
            "User-Agent": config.user_agent,
            "Accept": "application/json, text/plain, */*",
        }
        token = config.secret("api", "token")
        if token:
            header_name = config.secret("api", "token_header") or config.token_header
            # 密钥文件里的前缀为空时回退到配置；两者都空则直接拼裸 token
            prefix = config.secret("api", "token_prefix") or config.token_prefix
            headers[header_name] = f"{prefix}{token}".strip()
        cookie = config.secret("api", "cookie")
        if cookie:
            headers["Cookie"] = cookie
        # 配置里的静态头，再叠加密钥文件里的额外头（后者覆盖前者）
        headers.update(config.api_headers)
        headers.update(config.extra_headers())
        return headers

    # ---- 通用部分 ----

    def fetch(self) -> Reading:
        url, method, body = self._build_request()
        headers = self._auth_headers()
        if body is not None:
            headers.setdefault("Content-Type", "application/json; charset=utf-8")

        self._log.debug("请求 %s %s", method, url)
        response = http.request(
            url,
            method=method,
            body=body,
            headers=headers,
            timeout=self._config.http_timeout,
            attempts=self._config.http_attempts,
            logger=self._log,
        )

        if response.status in (401, 403):
            raise AuthExpiredError(
                f"接口返回 HTTP {response.status}，登录态已失效，需要重新抓 token 后执行 refresh-token"
            )
        if response.status >= 400:
            raise ProtocolError(
                f"接口返回 HTTP {response.status}：{response.text()[:200]}"
            )

        data = response.json()
        if isinstance(data, dict):
            _looks_like_auth_failure(data, self._log)
        if not isinstance(data, dict):
            raise ProtocolError(f"响应顶层不是 JSON 对象：{type(data).__name__}")

        return parse_reading(data, self._config, source="api")


def _looks_like_auth_failure(payload: dict[str, Any], log: logging.Logger) -> None:
    """识别「HTTP 200 但业务上是未登录」的常见形态。

    只在响应里出现明确的未登录字样或 401/403 业务码时才判定，
    避免把正常响应当成登录失效。
    """
    code = payload.get("code", payload.get("errcode", payload.get("status")))
    if code in (401, 403, "401", "403"):
        raise AuthExpiredError(f"接口业务码 {code} 表示登录态失效，需要续期")

    text = json.dumps(payload, ensure_ascii=False)[:500]
    for hint in _AUTH_HINTS:
        if hint in text:
            log.warning("响应中出现登录失效关键字：%s", hint)
            raise AuthExpiredError(
                f"接口返回包含「{hint}」，判定为登录态失效，需要重新抓 token"
            )


def make_client(config: Config, logger: logging.Logger | None = None) -> BalanceClient:
    if config.api_mode == "fixture":
        return FixtureClient(config)
    if config.api_mode == "http":
        return HttpBalanceClient(config, logger)
    raise ConfigError(f"未知的 [api].mode：{config.api_mode}")
