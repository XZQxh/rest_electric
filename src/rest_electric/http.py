"""基于 urllib 的极简 HTTP 封装：超时、指数退避重试、状态码分类。

不引第三方库是刻意选择（见 docs/decisions-dianfei.md D3），
因为目标设备是长期无人值守的树莓派。
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ProtocolError, TransportError

DEFAULT_BACKOFF = (2.0, 8.0, 30.0)
_RETRY_STATUS = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    headers: dict[str, str]
    url: str

    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip()
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            preview = self.text()[:200].replace("\n", " ")
            raise ProtocolError(f"响应不是 JSON：{preview!r}") from exc


def request(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    timeout: float = 15.0,
    attempts: int = 3,
    backoff: tuple[float, ...] = DEFAULT_BACKOFF,
    logger: logging.Logger | None = None,
) -> Response:
    """发请求并返回响应。4xx（除 429）不重试，直接返回给调用方判断。

    网络层失败（超时/连接被拒/DNS）重试 attempts 次后抛 TransportError；
    5xx 与 429 也会重试，耗尽后返回最后一次响应，由调用方决定如何处理。
    """
    log = logger or logging.getLogger(__name__)
    last_error = "未知错误"
    attempts = max(1, attempts)

    for index in range(attempts):
        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers=dict(headers or {}),
                method=method.upper(),
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return Response(
                    status=resp.status,
                    body=resp.read(),
                    headers={k.lower(): v for k, v in resp.headers.items()},
                    url=resp.geturl(),
                )
        except urllib.error.HTTPError as exc:
            payload = b""
            try:
                payload = exc.read()
            except Exception:  # pragma: no cover - 读取失败不应覆盖原始错误
                pass
            response = Response(
                status=exc.code,
                body=payload,
                headers={k.lower(): v for k, v in (exc.headers or {}).items()},
                url=url,
            )
            if exc.code in _RETRY_STATUS and index < attempts - 1:
                delay = backoff[min(index, len(backoff) - 1)]
                log.warning("HTTP %s，%.0f 秒后重试（%d/%d）", exc.code, delay, index + 1, attempts)
                time.sleep(delay)
                last_error = f"HTTP {exc.code}"
                continue
            return response
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if index < attempts - 1:
                delay = backoff[min(index, len(backoff) - 1)]
                log.warning("请求失败（%s），%.0f 秒后重试（%d/%d）", last_error, delay, index + 1, attempts)
                time.sleep(delay)

    raise TransportError(f"请求失败（已尝试 {attempts} 次）：{last_error}")


def get(
    url: str,
    *,
    params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    if params:
        joiner = "&" if urllib.parse.urlparse(url).query else "?"
        url = f"{url}{joiner}{urllib.parse.urlencode(params)}"
    return request(url, method="GET", headers=headers, **kwargs)


def post_form(
    url: str,
    data: Mapping[str, str],
    *,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    merged = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    merged.update(headers or {})
    return request(
        url,
        method="POST",
        body=urllib.parse.urlencode(dict(data)).encode("utf-8"),
        headers=merged,
        **kwargs,
    )


def post_json(
    url: str,
    payload: Any,
    *,
    headers: Mapping[str, str] | None = None,
    **kwargs: Any,
) -> Response:
    merged = {"Content-Type": "application/json; charset=utf-8"}
    merged.update(headers or {})
    return request(
        url,
        method="POST",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=merged,
        **kwargs,
    )
