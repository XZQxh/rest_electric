"""mitmproxy 插件：自动把「微信换 token」的响应写进 config/secrets.toml。

作用：半自动方案里，你唯一要做的事就是打开一次小程序。这个插件会监听
`/openservice/wechatauth/token` 的响应，把新 token 直接落盘，不需要复制粘贴。

启动方式（二选一，在你自己的终端里）：

    mitmweb -s "D:\\DSHProject\\rest_electric\\scripts\\token_watcher.py"
    mitmdump -s "D:\\DSHProject\\rest_electric\\scripts\\token_watcher.py" -w capture\\raw.flow

启动后：
  1. 打开小程序 → 进入电费查询（会触发一次登录/换 token）
  2. mitmproxy 的控制台打印「token 已更新，有效期到 ...」即成功
  3. 之后直接跑 python -m rest_electric once 即可

注意：插件只写 token，不会打印 token 本身。
"""

from __future__ import annotations

import base64
import datetime
import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRETS = ROOT / "config" / "secrets.toml"
LOG = ROOT / "data" / "token-watcher.log"
TOKEN_PATH = "/openservice/wechatauth/token"
ADDON_VERSION = 2


def _log(message: str) -> None:
    """同时打印到 mitmproxy 控制台并追加到 data/token-watcher.log。"""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[rest_electric] {message}")
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        pass



def _set_toml_string(path: Path, section: str, key: str, value: str) -> None:
    """在 secrets.toml 里就地更新一个字符串键，保留其它内容与注释。"""
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

    tomllib.loads(text)  # 写前校验，破坏结构就放弃
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")


def _expiry_text(token: str) -> str:
    raw = token[len("Bearer ") :] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        return "无法解析有效期"
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception:
        return "无法解析有效期"
    if "exp" not in claims:
        return "token 不含过期时间"
    expires = datetime.datetime.fromtimestamp(
        claims["exp"], datetime.timezone(datetime.timedelta(hours=8))
    )
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    minutes = round((claims["exp"] - now) / 60)
    return f"{expires:%Y-%m-%d %H:%M:%S}（还剩 {minutes} 分钟）"


class TokenWatcher:
    """mitmproxy addon。只关心 token 接口的响应。"""

    def __init__(self) -> None:
        self.hits = 0

    def load(self, loader):  # noqa: ANN001 - mitmproxy 的 loader 对象
        _log(f"插件已加载（v{ADDON_VERSION}），监听 {TOKEN_PATH}")
        _log(f"凭证将写入 {SECRETS}")

    def request(self, flow):  # noqa: ANN001
        """任何与 wechatauth 相关的请求都记一笔，方便确认小程序有没有触发登录。"""
        try:
            if "wechatauth" in flow.request.path or "/sso/" in flow.request.path:
                _log(f"认证请求：{flow.request.method} {flow.request.path.split('?')[0]}")
        except Exception:
            pass

    def response(self, flow) -> None:  # noqa: ANN001 - mitmproxy 的 flow 对象
        try:
            if TOKEN_PATH not in flow.request.path:
                return
            if flow.response is None or flow.response.status_code != 200:
                return

            payload = json.loads(flow.response.get_text())
            data = payload.get("data") or {}
            token = data.get("token")
            retmsg = payload.get("retmsg")

            if not token:
                _log(f"token 接口未返回 token：{retmsg or '未知原因'}")
                return

            raw = token[len("Bearer ") :] if token.startswith("Bearer ") else token
            _set_toml_string(SECRETS, "api", "token", raw)
            self.hits += 1
            _log(f"token 已更新 → {SECRETS}")
            _log(f"有效期到 {_expiry_text(token)}")
            _log("现在可以运行：python -m rest_electric once")
        except Exception as exc:  # 插件出错不能影响抓包
            _log(f"处理 token 失败：{exc}")


addons = [TokenWatcher()]
