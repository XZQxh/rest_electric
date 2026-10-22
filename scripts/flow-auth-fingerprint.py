"""只读分析工具：从 mitmproxy flow 里提取「认证链路的结构指纹」。

用途：验证「token 是怎么换来的、有没有续期接口」。只输出长度、哈希前 10 位、
枚举值（retcode/retmsg）和字段名，**不输出** token / code / cookie / ticket 原值。

用法（不需要启动代理，只读文件）：

    mitmdump -n -r "D:\\EdgeDownload\\flows (2)" -s scripts/flow-auth-fingerprint.py

输出：追加写入 data/flow-auth-fingerprint.txt

判定要点：
  * [TOKEN-REQ] 的 code 指纹两两不同 → 每次换 token 都用了新的 code
  * 响应 data 里只有 token/expiretime，没有 refresh_token → 不是刷新接口
  * 请求头里没有 Cookie/Authorization → 无会话态，身份只能来自 code
  * 业务请求的 swauth 指纹与某个 token 相同 → 全局只有一个 token，没有二次续期
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "data" / "flow-auth-fingerprint.txt"
rows: list[str] = []


def fp(value: str) -> str:
    if not value:
        return "<empty>"
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]


def jwt_claims(token: str, stuempno: str = "") -> dict:
    """只取声明名与时间字段；aud 可能是学号，只输出长度与是否等于 stuempno。"""
    raw = token[7:] if token.lower().startswith("bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        return {"_not_jwt_parts": len(parts)}
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception as exc:  # pragma: no cover - 异常数据
        return {"_decode_error": type(exc).__name__}
    out: dict = {"_claim_keys": sorted(payload)}
    for key in ("exp", "iat", "nbf"):
        if key in payload:
            out[key] = payload[key]
    if "exp" in payload and "iat" in payload:
        out["ttl_seconds"] = payload["exp"] - payload["iat"]
    if "iss" in payload and isinstance(payload["iss"], str):
        out["iss"] = payload["iss"]
    if "aud" in payload:
        aud = payload["aud"]
        out["aud_len"] = len(aud) if isinstance(aud, str) else None
        out["aud_is_stuempno"] = bool(stuempno) and aud == stuempno
    return out


def response(flow) -> None:  # noqa: ANN001 - mitmproxy flow
    req, resp = flow.request, flow.response
    if req.host != "shoucard.shou.edu.cn" or resp is None:
        return
    path = req.path.split("?")[0]
    tag = f"{req.method} {path} -> {resp.status_code}"

    if path.endswith("/wechatauth/token") and req.method == "POST":
        try:
            body = json.loads(req.get_text(strict=False) or "{}")
        except Exception:
            body = {}
        code = body.get("code") or ""
        rows.append(
            f"[TOKEN-REQ] {tag} code={fp(code)} code_len={len(code)} "
            f"stuempno_len={len(body.get('stuempno') or '')}"
        )
        try:
            payload = json.loads(resp.get_text(strict=False) or "{}")
        except Exception:
            payload = {}
        data = payload.get("data") or {}
        token = data.get("token") or ""
        rows.append(
            f"           retcode={payload.get('retcode')!r} retmsg={payload.get('retmsg')!r} "
            f"expiretime={data.get('expiretime')!r} token_len={len(token)} data_keys={sorted(data)}"
        )
        if token:
            rows.append(
                f"           token={fp(token)} "
                f"claims={json.dumps(jwt_claims(token, body.get('stuempno') or ''), ensure_ascii=False)}"
            )
        return

    if path.endswith("/sso/login"):
        code = dict(req.query).get("code", "")
        rows.append(f"[SSO-LOGIN] {tag} arg_code={fp(code)} len={len(code)}")
        location = resp.headers.get("location", "")
        rows.append(f"           Location_host={location.split('?')[0]}")
        return

    if path.endswith("/sso/auth"):
        rows.append(f"[SSO-AUTH] {tag} ticket_present={'yes' if dict(req.query).get('ticket') else 'no'}")
        location = resp.headers.get("location", "")
        rows.append(f"           Location_host={location.split('?')[0]}")
        return

    if path.startswith("/openservice/miniprogram/"):
        auth = req.headers.get("sw-authorization", "")
        rows.append(f"[BIZ] {tag} swauth_len={len(auth)} swauth={fp(auth)}")


def done() -> None:
    if not rows:
        print("[auth-fingerprint] 没有命中任何 shoucard 请求")
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(rows) + "\n")
    print(f"[auth-fingerprint] 追加 {len(rows)} 行到 {OUT}")
