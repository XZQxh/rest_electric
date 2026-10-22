"""命令行入口。所有子命令都在这里，退出码见 README。"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import __version__, alerts, clock, log, notify, report, store
from .client import get_path, make_client
from .config import DEFAULT_SECRETS, Config, load_config
from .errors import (
    AuthExpiredError,
    ConfigError,
    NotifierError,
    ProtocolError,
    RestElectricError,
    TransportError,
)
from .forecast import Estimate, estimate
from .models import AlertRecord, Reading, to_cents, yuan

EXIT_OK = 0
EXIT_FAIL = 2
EXIT_CONFIG = 3
EXIT_AUTH = 4


# ---------------------------------------------------------------- 通用工具


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    # SUPPRESS 是必须的：否则子命令的默认值会覆盖子命令之前传入的全局参数
    parser.add_argument("--config", type=Path, default=argparse.SUPPRESS, help="配置文件路径")
    parser.add_argument("--secrets", type=Path, default=argparse.SUPPRESS, help="密钥文件路径")
    parser.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出调试日志"
    )
    return parser


def _load(args: argparse.Namespace) -> Config:
    return load_config(
        getattr(args, "config", None),
        getattr(args, "secrets", None),
    )


def _logger(config: Config, args: argparse.Namespace):
    return log.setup(
        config.db_path.parent / "rest_electric.log",
        verbose=getattr(args, "verbose", False),
    )


def _format_alert(
    config: Config,
    decision: alerts.AlertDecision,
    reading: Reading,
    estimate_result: Estimate,
) -> tuple[str, str]:
    titles = {
        alerts.LEVEL_LOW_BALANCE: f"电费余额偏低：{reading.balance_yuan} 元",
        alerts.LEVEL_PREDICTED_LOW: (
            f"电费预计 {estimate_result.days_left:.1f} 天后欠费"
            if estimate_result.days_left is not None
            else "电费即将欠费"
        ),
        alerts.LEVEL_AUTH_EXPIRED: "电费查询登录态失效，需要续期",
        alerts.LEVEL_FETCH_FAILED: "电费查询失败",
    }
    title = titles.get(decision.level, decision.level)

    lines = [
        f"**房间**：{config.room_key}",
        f"**当前余额**：{reading.balance_yuan} 元",
        f"**原因**：{decision.reason}",
    ]
    if estimate_result.daily_cents is not None:
        lines.append(f"**日均用电**：{estimate_result.daily_cents / 100:.2f} 元/天")
    if estimate_result.days_left is not None:
        lines.append(f"**预计可用**：{estimate_result.days_left:.1f} 天")
    lines.append(
        f"**阈值**：余额 < {config.balance_floor_cents / 100:.2f} 元"
        f" 或 剩余 < {config.days_left_warn:.1f} 天"
    )
    lines.append(f"**查询时间**：{reading.captured_at}")
    return title, "\n".join(lines)


def _send_once(
    config: Config,
    conn: Any,
    logger: Any,
    level: str,
    title: str,
    body: str,
    enabled: bool,
    dry_run: bool,
) -> bool:
    if not enabled:
        logger.info("推送已禁用，跳过：%s", title)
        return False
    conn_ref = conn
    if conn_ref is not None and store.already_alerted(
        conn_ref, config.room_key, level, alerts.COOLDOWNS.get(level, 1)
    ):
        logger.info("冷却期内，不重复推送：%s", title)
        return False
    try:
        notify.make_notifier(config, logger).send(title, body)
    except NotifierError as exc:
        logger.error("推送失败：%s", exc)
        return False
    except (TransportError, ProtocolError) as exc:
        logger.error("推送请求失败：%s", exc)
        return False

    if conn_ref is not None:
        store.record_alert(
            conn_ref,
            AlertRecord(
                room_key=config.room_key,
                level=level,
                day=clock.today().isoformat(),
                sent_at=clock.iso(),
                detail=title,
            ),
        )
    logger.info("已推送：%s", title)
    return True


def _print_summary(
    config: Config,
    reading: Reading,
    estimate_result: Estimate,
    inserted: bool,
    dry_run: bool,
) -> None:
    print("-" * 52)
    print(f"房间　　　{config.room_key}")
    print(f"当前余额　{reading.balance_yuan} 元")
    if estimate_result.daily_cents is not None:
        print(f"日均用电　{estimate_result.daily_cents / 100:.2f} 元/天")
    if estimate_result.days_left is not None:
        print(f"预计可用　{estimate_result.days_left:.1f} 天")
    if estimate_result.daily_cents is None:
        print(f"预测　　　{estimate_result.reason}")
    print(f"数据来源　{reading.source}")
    if dry_run:
        print("模式　　　干跑（未写入数据库、未推送）")
    else:
        print(f"入库　　　{'新增 1 条' if inserted else '已存在，未重复写入'}")
    print("-" * 52)


# ---------------------------------------------------------------- 子命令


def cmd_once(args: argparse.Namespace) -> int:
    config = _load(args)
    logger = _logger(config, args)
    logger.info("开始查询：房间 %s，模式 %s", config.room_key, config.api_mode)

    dry_run = bool(getattr(args, "dry_run", False))
    enabled = not dry_run and not getattr(args, "no_notify", False)

    conn = None
    if not dry_run:
        conn = store.connect(config.db_path)
        store.init_db(conn)

    client = make_client(config, logger)
    try:
        reading = client.fetch()
    except AuthExpiredError as exc:
        logger.error("登录态失效：%s", exc)
        title = "电费查询登录态失效，需要续期"
        body = (
            f"**房间**：{config.room_key}\n\n"
            f"**原因**：{exc}\n\n"
            "请在 PC 微信里重新打开「上海海洋大学一卡通」小程序查一次电费，"
            "按 docs/recon-sop.md 抓出新 token 后执行：\n"
            "`python -m rest_electric refresh-token --token <新值>`"
        )
        _send_once(
            config, conn, logger, alerts.LEVEL_AUTH_EXPIRED, title, body, enabled, dry_run
        )
        if dry_run:
            print(f"[干跑] 登录态失效：{exc}")
        return EXIT_AUTH
    except (TransportError, ProtocolError) as exc:
        logger.error("查询失败：%s", exc)
        title = "电费查询失败"
        body = (
            f"**房间**：{config.room_key}\n\n"
            f"**错误**：{exc}\n\n"
            f"**时间**：{clock.iso()}\n\n"
            "数据未更新，下次调度会自动重试。若连续失败，请检查 token 或网络。"
        )
        _send_once(
            config, conn, logger, alerts.LEVEL_FETCH_FAILED, title, body, enabled, dry_run
        )
        if dry_run:
            print(f"[干跑] 查询失败：{exc}")
        return EXIT_FAIL

    estimate_result = estimate(
        [] if dry_run else store.daily_points(conn, config.room_key, config.window_days),
        reading.balance_cents,
    )

    if dry_run:
        _print_summary(config, reading, estimate_result, inserted=False, dry_run=True)
        return EXIT_OK

    inserted = store.insert_reading(conn, reading, config.keep_raw)
    store.set_meta(conn, "last_success_at", clock.iso())
    logger.info("已写入读数：%s 元（%s）", reading.balance_yuan, "新增" if inserted else "重复")

    estimate_result = estimate(
        store.daily_points(conn, config.room_key, config.window_days),
        reading.balance_cents,
    )
    decisions = alerts.decide(
        reading.balance_cents,
        estimate_result,
        config.balance_floor_cents,
        config.days_left_warn,
    )
    for decision in decisions:
        title, body = _format_alert(config, decision, reading, estimate_result)
        _send_once(config, conn, logger, decision.level, title, body, enabled, dry_run)

    _print_summary(config, reading, estimate_result, inserted=inserted, dry_run=False)
    return EXIT_OK


def cmd_report(args: argparse.Namespace) -> int:
    config = _load(args)
    conn = store.connect(config.db_path)
    store.init_db(conn)
    days = int(getattr(args, "days", 14) or 14)
    points = store.daily_points(conn, config.room_key, days)
    estimate_result = estimate(points)
    text = report.render(
        config,
        points,
        estimate_result,
        store.recent_alerts(conn, config.room_key),
        days=days,
    )
    print(text)
    csv_path = getattr(args, "csv", None)
    if csv_path:
        report.export_csv(points, Path(csv_path))
        print(f"\n已导出 CSV：{csv_path}")

    if getattr(args, "push", False):
        balance_text = yuan(points[-1][1]) if points else "暂无数据"
        title = f"电费报表：{balance_text} 元"
        body = "```\n" + text + "\n```"
        try:
            notify.make_notifier(config).send(title, body)
        except NotifierError as exc:
            print(f"推送失败：{exc}", file=sys.stderr)
            return EXIT_FAIL
        print("\n已推送到微信。")
    return EXIT_OK


def cmd_probe(args: argparse.Namespace) -> int:
    config = _load(args)
    path = Path(args.file)
    if not path.exists():
        raise ConfigError(f"样本文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"样本不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ProtocolError("样本顶层必须是 JSON 对象")

    field = args.field or config.balance_field
    unit = args.unit or config.balance_unit
    value = get_path(data, field)
    cents = to_cents(value, unit)

    print(f"文件　　　{path}")
    print(f"字段路径　{field}")
    print(f"原始值　　{value!r}")
    print(f"单位　　　{unit}")
    print(f"解析结果　{cents} 分 = {yuan(cents)} 元")
    print(f"房间　　　{config.room_key}")
    return EXIT_OK


def _set_toml_string(path: Path, section: str, key: str, value: str) -> None:
    """在 TOML 文件中就地设置一个字符串键，尽量保留其余内容与注释。"""
    line_new = f"{key} = {json.dumps(value, ensure_ascii=False)}"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    section_header = f"[{section}]"
    section_index = next(
        (index for index, line in enumerate(lines) if line.strip() == section_header), None
    )

    if section_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([section_header, line_new])
    else:
        end_index = next(
            (
                index
                for index in range(section_index + 1, len(lines))
                if lines[index].strip().startswith("[")
            ),
            len(lines),
        )
        key_index = next(
            (
                index
                for index in range(section_index + 1, end_index)
                if lines[index].strip().startswith(f"{key} ")
                or lines[index].strip().startswith(f"{key}=")
            ),
            None,
        )
        if key_index is None:
            lines.insert(section_index + 1, line_new)
        else:
            lines[key_index] = line_new

    text = "\n".join(lines) + "\n"
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"写入后会破坏 TOML 结构，已放弃：{exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.write_text(text, encoding="utf-8")


def cmd_refresh_token(args: argparse.Namespace) -> int:
    path = Path(getattr(args, "secrets", None) or DEFAULT_SECRETS)
    token = args.token.strip()
    if not token:
        raise ConfigError("--token 不能为空")
    _set_toml_string(path, "api", "token", token)
    print(f"已更新 token：{path}")
    print("提示：原文件已备份为 secrets.toml.bak；下一次 once 会使用新 token。")
    return EXIT_OK


def cmd_token_status(args: argparse.Namespace) -> int:
    """本地解析 JWT 的过期时间，不联网。"""
    config = _load(args)
    token = config.secret("api", "token")
    if not token:
        print("secrets.toml 里没有 token。", file=sys.stderr)
        print("请先运行 proxy-on.ps1 和 mitmweb -s scripts\\token_watcher.py，然后打开一次小程序。")
        return EXIT_AUTH

    raw = token[len("Bearer ") :] if token.startswith("Bearer ") else token
    parts = raw.split(".")
    if len(parts) != 3:
        print("当前 token 不是 JWT，无法判断有效期。")
        return EXIT_OK
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except Exception as exc:
        print(f"token 解析失败：{exc}", file=sys.stderr)
        return EXIT_FAIL
    if "exp" not in claims:
        print("token 不含 exp 字段，无法判断有效期。")
        return EXIT_OK

    zone = datetime.timezone(datetime.timedelta(hours=8))
    expires = datetime.datetime.fromtimestamp(claims["exp"], zone)
    remaining = (claims["exp"] - clock.now().timestamp()) / 60

    print(f"token 长度　{len(raw)}")
    print(f"签发时间　{datetime.datetime.fromtimestamp(claims.get('iat', 0), zone):%Y-%m-%d %H:%M:%S}")
    print(f"过期时间　{expires:%Y-%m-%d %H:%M:%S}")
    if remaining > 0:
        print(f"状态　　　有效，还剩 {remaining:.0f} 分钟")
        return EXIT_OK
    print(f"状态　　　已过期 {abs(remaining):.0f} 分钟，请重新打开小程序以刷新 token")
    return EXIT_AUTH


def cmd_init_db(args: argparse.Namespace) -> int:
    config = _load(args)
    conn = store.connect(config.db_path)
    store.init_db(conn)
    print(f"数据库就绪：{config.db_path}")
    return EXIT_OK


def cmd_selftest(args: argparse.Namespace) -> int:
    results: list[tuple[str, str, str]] = []  # (级别, 项目, 详情)

    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 11):
        results.append(("OK", "Python 版本", sys.version.split()[0]))
    else:
        results.append(("FAIL", "Python 版本", f"{sys.version.split()[0]}，需要 3.11+ 才有 tomllib"))

    results.append(("OK", "时区", f"{clock.TZ_NAME}，当前 {clock.iso()}"))

    config: Config | None = None
    try:
        config = _load(args)
        results.append(("OK", "配置", str(config.config_path)))
        if config.secrets_path is None:
            results.append(("WARN", "密钥", config.secrets_error or "未找到密钥文件"))
        else:
            results.append(("OK", "密钥", str(config.secrets_path)))
    except ConfigError as exc:
        results.append(("FAIL", "配置", str(exc)))

    if config is not None:
        try:
            conn = store.connect(config.db_path)
            store.init_db(conn)
            store.set_meta(conn, "selftest_at", clock.iso())
            results.append(("OK", "数据库", str(config.db_path)))
        except Exception as exc:  # noqa: BLE001 - 自检需要报告任何失败
            results.append(("FAIL", "数据库", str(exc)))

        if config.api_mode == "http":
            missing = [
                name
                for name, value in (
                    ("base_url", config.base_url),
                    ("balance_path", config.balance_path),
                )
                if not value
            ]
            if missing:
                results.append(("FAIL", "接口配置", f"缺少 {'、'.join(missing)}"))
            elif not config.secret("api", "token") and not config.secret("api", "cookie"):
                results.append(("WARN", "接口凭证", "token 与 cookie 都为空"))
            else:
                results.append(("OK", "接口配置", f"{config.base_url}{config.balance_path}"))

        try:
            notifier = notify.make_notifier(config)
            results.append(("OK", "推送通道", notifier.name))
            if getattr(args, "send_test", False):
                notifier.send("rest_electric 自检", "这是一条测试消息，收到即表示推送通道正常。")
                results.append(("OK", "推送测试", "已发送"))
        except NotifierError as exc:
            results.append(("WARN", "推送通道", str(exc)))
        except RestElectricError as exc:
            results.append(("FAIL", "推送测试", str(exc)))

    print(f"rest_electric {__version__} 自检")
    print("-" * 52)
    for level, name, detail in results:
        print(f"[{level:<4}] {name:<10} {detail}")

    failures = [item for item in results if item[0] == "FAIL"]
    print("-" * 52)
    print(f"结果：{len(results) - len(failures)} 项通过，{len(failures)} 项失败")
    return EXIT_OK if not failures else EXIT_CONFIG


# ---------------------------------------------------------------- 入口


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="rest_electric",
        description="宿舍电费自动查询与告警",
        parents=[common],
    )
    parser.add_argument("--version", action="version", version=f"rest_electric {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    once = subparsers.add_parser("once", parents=[common], help="查询一次并落库、按需告警")
    once.add_argument("--dry-run", action="store_true", help="不写库、不推送，只验证链路")
    once.add_argument("--no-notify", action="store_true", help="写库但不推送")
    once.set_defaults(handler=cmd_once)

    report_cmd = subparsers.add_parser("report", parents=[common], help="打印历史报表")
    report_cmd.add_argument("--days", type=int, default=14, help="回看天数，默认 14")
    report_cmd.add_argument("--csv", type=Path, default=None, help="导出 CSV 的路径")
    report_cmd.add_argument("--push", action="store_true", help="把报表推送到微信")
    report_cmd.set_defaults(handler=cmd_report)

    probe = subparsers.add_parser("probe", parents=[common], help="用样本文件验证解析逻辑")
    probe.add_argument("--file", required=True, help="响应样本 JSON 路径")
    probe.add_argument("--field", default="", help="金额字段点号路径，覆盖配置")
    probe.add_argument("--unit", default="", choices=["", "yuan", "cent"], help="金额单位，覆盖配置")
    probe.set_defaults(handler=cmd_probe)

    refresh = subparsers.add_parser(
        "refresh-token", parents=[common], help="更新 secrets.toml 中的 token"
    )
    refresh.add_argument("--token", required=True, help="新抓到的 token")
    refresh.set_defaults(handler=cmd_refresh_token)

    status = subparsers.add_parser(
        "token-status", parents=[common], help="查看当前 token 的过期时间（不联网）"
    )
    status.set_defaults(handler=cmd_token_status)

    init = subparsers.add_parser("init-db", parents=[common], help="初始化数据库")
    init.set_defaults(handler=cmd_init_db)

    selftest = subparsers.add_parser("selftest", parents=[common], help="环境与配置自检")
    selftest.add_argument("--send-test", action="store_true", help="实际发一条测试推送")
    selftest.set_defaults(handler=cmd_selftest)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse 已保证有子命令
        parser.print_help()
        return EXIT_CONFIG

    try:
        return int(handler(args) or EXIT_OK)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except RestElectricError as exc:
        print(f"执行失败：{exc}", file=sys.stderr)
        return EXIT_FAIL
    except KeyboardInterrupt:
        print("已中断", file=sys.stderr)
        return 130
