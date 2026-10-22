#!/usr/bin/env python3
"""树莓派驱动安卓手机：dump 界面 → 找余额 → 上报（引擎 A，免装 App）。

前提（一次性）：
    手机开「USB 调试」，USB 连一次树莓派后执行：
        adb tcpip 5555
    之后拔掉 USB，手机同一 Wi-Fi 下即可：
        adb connect <手机IP>:5555
    注意：手机重启后 tcpip 模式会失效，需要再插一次 USB（见 docs/phone-automation.md）。

用法：
    # 1. 校准：看当前界面上都有什么文字（G2 验证门）
    python3 scripts/phone/adb_dump.py --calibrate

    # 2. 试跑：只读余额，不上报
    python3 scripts/phone/adb_dump.py --dry-run

    # 3. 正式：读余额并上报（密钥从环境变量取，避免出现在 ps 里）
    REST_ELECTRIC_INGEST_KEY=xxxx python3 scripts/phone/adb_dump.py \
        --url http://192.168.1.10:8787/ingest

退出码：0 成功；2 找不到余额（已截图）；3 参数/环境问题。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone

AMOUNT_RE = re.compile(r"^\s*(\d{1,4}\.\d{2})\s*元?\s*$")
LABEL_WORDS = ("电费", "余额", "剩余", "寝室")
BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


@dataclass
class Node:
    text: str
    desc: str
    left: int
    top: int
    right: int
    bottom: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.left + self.right) // 2, (self.top + self.bottom) // 2

    def __str__(self) -> str:
        return f"{self.text or self.desc!r:<28} bounds=({self.left},{self.top})-({self.right},{self.bottom})"


# ---------------------------------------------------------------- adb 封装


def adb(serial: str | None, *args: str, binary: bool = False, timeout: float = 20.0):
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += list(args)
    if binary:
        return subprocess.run(cmd, capture_output=True, timeout=timeout).stdout
    result = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(
            f"adb {' '.join(args)} 失败：{result.stderr.decode('utf-8', 'replace').strip()}"
        )
    return result.stdout.decode("utf-8", "replace")


def dump_ui(serial: str | None) -> list[Node]:
    """uiautomator dump 后解析出所有带文字的节点。"""
    remote = "/sdcard/rest_electric_dump.xml"
    adb(serial, "shell", "uiautomator", "dump", remote)
    raw = adb(serial, "exec-out", "cat", remote, binary=True)
    text = raw.decode("utf-8", "replace").strip()
    start = text.find("<")
    if start < 0:
        raise RuntimeError("uiautomator dump 返回的不是 XML")
    root = ET.fromstring(text[start:])
    nodes: list[Node] = []
    for element in root.iter("node"):
        content = (element.get("text") or "") + " " + (element.get("content-desc") or "")
        if not content.strip():
            continue
        match = BOUNDS_RE.match(element.get("bounds") or "")
        if not match:
            continue
        left, top, right, bottom = (int(value) for value in match.groups())
        nodes.append(
            Node(
                text=element.get("text") or "",
                desc=element.get("content-desc") or "",
                left=left,
                top=top,
                right=right,
                bottom=bottom,
            )
        )
    return nodes


def tap(serial: str | None, node: Node) -> None:
    x, y = node.center
    adb(serial, "shell", "input", "tap", str(x), str(y))


def screenshot(serial: str | None, path: str) -> None:
    data = adb(serial, "exec-out", "screencap", "-p", binary=True, timeout=30)
    with open(path, "wb") as handle:
        handle.write(data)


# ---------------------------------------------------------------- 业务逻辑


def find_amount(nodes: list[Node]) -> tuple[str, Node] | None:
    """优先取「电费/余额/剩余」标签附近的金额；否则只有在唯一候选时才返回。"""
    candidates = []
    for node in nodes:
        for value in (node.text, node.desc):
            match = AMOUNT_RE.match(value or "")
            if match:
                candidates.append((match.group(1), node))
    if not candidates:
        return None

    labels = [n for n in nodes if any(word in (n.text + n.desc) for word in LABEL_WORDS)]
    if labels:
        def distance(item: tuple[str, Node]) -> int:
            value, node = item
            return min(
                abs(node.top - label.bottom) + abs(node.left - label.left) for label in labels
            )

        best = min(candidates, key=distance)
        second = sorted(candidates, key=distance)[1] if len(candidates) > 1 else None
        # 两个候选离标签一样近时，说明有歧义，宁可失败也不猜
        if second is not None and distance(second) == distance(best):
            return None
        return best

    return candidates[0] if len(candidates) == 1 else None


def open_mini_program(serial: str | None, shortcut_text: str, wait_seconds: float) -> bool:
    """回桌面点快捷方式。找不到就返回 False，交给人工校准。"""
    adb(serial, "shell", "input", "keyevent", "KEYCODE_WAKEUP")
    try:  # 部分机器不支持 dismiss-keyguard，失败就忽略
        adb(serial, "shell", "wm", "dismiss-keyguard")
    except Exception:
        pass
    adb(serial, "shell", "input", "keyevent", "KEYCODE_HOME")
    time.sleep(1.5)

    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        for node in dump_ui(serial):
            if shortcut_text in (node.text + node.desc):
                tap(serial, node)
                return True
        time.sleep(1.0)
    return False


def wait_for_amount(serial: str | None, wait_seconds: float) -> tuple[str, Node] | None:
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        found = find_amount(dump_ui(serial))
        if found:
            return found
        time.sleep(2.0)
    return None


def post(url: str, key: str, payload: dict) -> dict:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "X-Ingest-Key": key},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="用 adb 读小程序里的电费余额并上报")
    parser.add_argument("--serial", default=os.environ.get("ANDROID_SERIAL", ""), help="adb 设备序列号")
    parser.add_argument("--url", default="http://127.0.0.1:8787/ingest", help="ingest 地址")
    parser.add_argument("--shortcut", default="上海海洋大学一卡通", help="桌面快捷方式的文字")
    parser.add_argument("--wait", type=float, default=60.0, help="等页面出现的秒数")
    parser.add_argument("--calibrate", action="store_true", help="只打印当前界面的文字，用于校准")
    parser.add_argument("--dump", default="", help="把当前界面 XML 存到文件")
    parser.add_argument("--dry-run", action="store_true", help="只读余额，不上报")
    parser.add_argument("--skip-open", action="store_true", help="不点快捷方式，直接读当前界面")
    parser.add_argument("--shot-dir", default="/tmp", help="失败截图目录")
    args = parser.parse_args(argv)

    serial = args.serial or None

    try:
        if args.calibrate or args.dump:
            nodes = dump_ui(serial)
            print(f"共 {len(nodes)} 个带文字的节点：")
            for node in nodes:
                print("  " + str(node))
            if args.dump:
                with open(args.dump, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(str(node) for node in nodes))
                print(f"已写入 {args.dump}")
            if args.calibrate:
                return 0
        elif not args.skip_open:
            if not open_mini_program(serial, args.shortcut, args.wait):
                print(f"找不到桌面快捷方式「{args.shortcut}」。请先把它加到桌面，或用 --calibrate 看看有什么。", file=sys.stderr)
                return 2

        found = wait_for_amount(serial, args.wait)
        if not found:
            path = os.path.join(args.shot_dir, f"electric-fail-{int(time.time())}.png")
            try:
                screenshot(serial, path)
                print(f"没找到余额，截图已存：{path}", file=sys.stderr)
            except Exception as exc:  # 截图失败不该掩盖主错误
                print(f"没找到余额，且截图失败：{exc}", file=sys.stderr)
            return 2

        value, node = found
        print(f"读到余额：{value} 元（{node}）")

        if args.dry_run:
            return 0

        key = os.environ.get("REST_ELECTRIC_INGEST_KEY", "")
        if not key:
            print("缺少环境变量 REST_ELECTRIC_INGEST_KEY", file=sys.stderr)
            return 3

        payload = {
            "balance": value,
            "source": "adb",
            "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "device": serial or "adb",
        }
        try:
            result = post(args.url, key, payload)
        except urllib.error.HTTPError as exc:
            print(f"上报被拒绝：HTTP {exc.code} {exc.read()[:200]!r}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"上报失败：{exc}", file=sys.stderr)
            return 2
        print(f"上报成功：{json.dumps(result, ensure_ascii=False)}")
        return 0
    except Exception as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
