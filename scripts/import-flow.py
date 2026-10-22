#!/usr/bin/env python3
"""从 mitmproxy 的 flow 文件里提取电费查询请求，并把 token 写进 config/secrets.toml。

背景：本接口的 token 是 JWT，有效期只有 2 小时。每次重新抓包后跑一遍这个脚本，
就不用手工复制 token（脚本不会把 token 打印到屏幕上）。

用法：

    # 列出这次抓包里的所有请求，先确认有没有登录请求
    python scripts/import-flow.py --flow "D:\\EdgeDownload\\flows (1)" --list

    # 提取 queryroominfo 的 token 并写入 secrets.toml
    python scripts/import-flow.py --flow "D:\\EdgeDownload\\flows (1)"

    # 顺带生成 capture/request.json 与 capture/sample_response.json（脱敏）
    python scripts/import-flow.py --flow "..." --write-capture

只依赖标准库。flow 文件是 mitmproxy 的 tnetstring 串流格式。
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SECRETS = ROOT / "config" / "secrets.toml"
DEFAULT_MATCH = "queryroominfo"


# ---------------------------------------------------------------- tnetstring

def _parse(data: bytes, index: int) -> tuple[Any, int]:
    colon = data.index(b":", index)
    length = int(data[index:colon])
    start = colon + 1
    payload = data[start : start + length]
    type_byte = data[start + length : start + length + 1]
    next_index = start + length + 1

    if type_byte in (b",", b";"):
        return payload, next_index
    if type_byte == b"#":
        return int(payload), next_index
    if type_byte == b"^":
        return float(payload), next_index
    if type_byte == b"!":
        return payload == b"true", next_index
    if type_byte == b"~":
        return None, next_index
    if type_byte == b"}":
        result: dict[Any, Any] = {}
        cursor = 0
        while cursor < len(payload):
            key, cursor = _parse(payload, cursor)
            value, cursor = _parse(payload, cursor)
            result[key] = value
        return result, next_index
    if type_byte == b"]":
        items: list[Any] = []
        cursor = 0
        while cursor < len(payload):
            value, cursor = _parse(payload, cursor)
            items.append(value)
        return items, next_index
    raise ValueError(f"未知的 tnetstring 类型字节：{type_byte!r}（偏移 {index}）")


def read_flows(path: Path) -> list[dict[Any, Any]]:
    data = path.read_bytes()
    flows: list[dict[Any, Any]] = []
    index = 0
    while index < len(data):
        flow, index = _parse(data, index)
        if isinstance(flow, dict):
            flows.append(flow)
    return flows


# ---------------------------------------------------------------- 小工具

def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return "" if value is None else str(value)


def _headers(request: dict[Any, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in request.get(b"headers", []) or []:
        if isinstance(item, (list, tuple)) and len(item) == 2:
            result[_text(item[0])] = _text(item[1])
    return result


def _jwt_expiry(token: str) -> str:
    """尽量解析 JWT 的 exp；不是 JWT 就返回空串。"""
    raw = token[len("Bearer ") :] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        return ""
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return ""
    if "exp" not in claims:
        return ""
    zone = datetime.timezone(datetime.timedelta(hours=8))
    expires = datetime.datetime.fromtimestamp(claims["exp"], zone)
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    minutes = round((claims["exp"] - now) / 60, 1)
    return f"{expires.isoformat()}（还剩 {minutes} 分钟）"


def _set_toml_string(path: Path, section: str, key: str, value: str) -> None:
    line_new = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    header = f"[{section}]"
    section_index = next((i for i, line in enumerate(lines) if line.strip() == header), None)
    if section_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([header, line_new])
    else:
        end_index = next(
            (
                i
                for i in range(section_index + 1, len(lines))
                if lines[i].strip().startswith("[")
            ),
            len(lines),
        )
        key_index = next(
            (
                i
                for i in range(section_index + 1, end_index)
                if lines[i].strip().startswith(f"{key} ")
                or lines[i].strip().startswith(f"{key}=")
            ),
            None,
        )
        if key_index is None:
            lines.insert(section_index + 1, line_new)
        else:
            lines[key_index] = line_new
    text = "\n".join(lines) + "\n"
    import tomllib

    tomllib.loads(text)  # 写前校验，宁可失败也不破坏密钥文件
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- 主流程

def _mask_headers(headers: dict[str, str]) -> dict[str, str]:
    sensitive = ("authorization", "cookie", "token", "openid", "session")
    return {
        key: ("***" if any(word in key.lower() for word in sensitive) else value)
        for key, value in headers.items()
    }


def cmd_list(flows: list[dict[Any, Any]]) -> int:
    print(f"共 {len(flows)} 条流量")
    print(f"{'#':<3}{'方法':<6}{'主机':<30}{'路径':<45}{'状态'}")
    for index, flow in enumerate(flows):
        request = flow.get(b"request") or {}
        response = flow.get(b"response") or {}
        method = _text(request.get(b"method"))
        host = _text(request.get(b"host"))
        path = _text(request.get(b"path"))
        if "?" in path:
            path = path.split("?")[0] + "?<masked>"
        status = response.get(b"status_code") if isinstance(response, dict) else ""
        print(f"{index:<3}{method:<6}{host:<30}{path[:44]:<45}{status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从 mitmproxy flow 文件提取 token 与样本")
    parser.add_argument("--flow", required=True, type=Path, help="mitmproxy 保存的 flow 文件")
    parser.add_argument("--secrets", type=Path, default=DEFAULT_SECRETS, help="secrets.toml 路径")
    parser.add_argument("--match", default=DEFAULT_MATCH, help="目标请求路径包含的关键字")
    parser.add_argument("--token-header", default="sw-Authorization", help="携带 token 的请求头名")
    parser.add_argument("--list", action="store_true", help="只列出所有请求，不提取")
    parser.add_argument("--write-capture", action="store_true", help="生成脱敏的 capture 样本")
    args = parser.parse_args(argv)

    if not args.flow.exists():
        print(f"flow 文件不存在：{args.flow}", file=sys.stderr)
        return 3

    flows = read_flows(args.flow)
    if not flows:
        print("没解析出任何流量，flow 文件可能不是 mitmproxy 格式", file=sys.stderr)
        return 2

    if args.list:
        return cmd_list(flows)

    target = None
    for flow in flows:
        request = flow.get(b"request") or {}
        if args.match in _text(request.get(b"path")):
            target = flow
    if target is None:
        print(f"没找到路径包含 {args.match!r} 的请求。用 --list 看看都有什么。", file=sys.stderr)
        return 2

    request = target[b"request"]
    response = target[b"response"]
    headers = _headers(request)
    token_full = headers.get(args.token_header, "")
    if not token_full:
        print(f"目标请求里没有 {args.token_header} 头", file=sys.stderr)
        return 2

    raw_token = token_full[len("Bearer ") :] if token_full.startswith("Bearer ") else token_full
    _set_toml_string(args.secrets, "api", "token", raw_token)

    print(f"已提取：{_text(request.get(b'method'))} https://{_text(request.get(b'host'))}{_text(request.get(b'path'))}")
    print(f"token 已写入 {args.secrets}（长度 {len(raw_token)}，未打印）")
    expiry = _jwt_expiry(token_full)
    if expiry:
        print(f"token 过期时间：{expiry}")
    else:
        print("token 不是 JWT 或没有 exp 字段，无法判断有效期")

    body = request.get(b"content")
    if body:
        try:
            print("请求体：", json.loads(body.decode("utf-8")))
        except Exception:
            print("请求体：", repr(body[:200]))

    content = response.get(b"content") or b""
    try:
        payload = json.loads(content.decode("utf-8"))
        print("响应 retcode：", payload.get("retcode"), payload.get("retmsg"))
        data = payload.get("data") or {}
        if isinstance(data, dict) and "restElecDegree" in data:
            print("响应余额：", data["restElecDegree"])
    except Exception:
        print("响应不是 JSON，长度", len(content))

    if args.write_capture:
        capture = ROOT / "capture"
        capture.mkdir(exist_ok=True)
        try:
            request_dump = {
                "method": _text(request.get(b"method")),
                "url": f"https://{_text(request.get(b'host'))}{_text(request.get(b'path'))}",
                "headers": _mask_headers(headers),
                "body": json.loads(body.decode("utf-8")) if body else None,
            }
            (capture / "request.json").write_text(
                json.dumps(request_dump, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception as exc:
            print("写 request.json 失败：", exc)
        try:
            sample = json.loads(content.decode("utf-8"))
            if isinstance(sample.get("data"), dict) and "roomName" in sample["data"]:
                sample["data"]["roomName"] = "***"
            (capture / "sample_response.json").write_text(
                json.dumps(sample, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("已写 capture/sample_response.json（roomName 已打码）")
        except Exception as exc:
            print("写 sample_response.json 失败：", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
