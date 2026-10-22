"""命令行入口。所有子命令都在这里，退出码见 README。"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from . import __version__, alerts, clock, epay, ingest, log, notify, report, store
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


def _record_and_alert(
    config: Config,
    conn: Any,
    reading: Reading,
    logger: Any,
    *,
    enabled: bool,
) -> tuple[bool, Estimate]:
    """写库 + 预测 + 告警。`once` 与手机上报（`ingest`）共用同一条管线。"""
    inserted = store.insert_reading(conn, reading, config.keep_raw)
    store.set_meta(conn, "last_success_at", clock.iso())
    logger.info(
        "已写入读数：%s 元（%s，来源 %s）",
        reading.balance_yuan,
        "新增" if inserted else "重复",
        reading.source,
    )

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
        _send_once(config, conn, logger, decision.level, title, body, enabled, False)
    return inserted, estimate_result


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
        if config.api_mode == "epay":
            title = "电费网页端登录失败"
            hint = (
                "网页版自动登录没有成功。请检查：\n"
                "1. `config/secrets.toml` 的 `[epay].username` / `[epay].password` 是否正确；\n"
                "2. 学校登录页是否新增了验证码（有的话脚本需要人工处理）；\n"
                "3. 账号是否被锁定。"
            )
        else:
            title = "电费查询登录态失效，需要续期"
            hint = (
                "请在 PC 微信里重新打开「上海海洋大学一卡通」小程序查一次电费，"
                "按 docs/recon-sop.md 抓出新 token 后执行：\n"
                "`python -m rest_electric refresh-token --token <新值>`"
            )
        body = (
            f"**房间**：{config.room_key}\n\n"
            f"**原因**：{exc}\n\n"
            f"{hint}"
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

    if dry_run:
        estimate_result = estimate([], reading.balance_cents)
        _print_summary(config, reading, estimate_result, inserted=False, dry_run=True)
        return EXIT_OK

    inserted, estimate_result = _record_and_alert(
        config, conn, reading, logger, enabled=enabled
    )

    _print_summary(config, reading, estimate_result, inserted=inserted, dry_run=False)
    return EXIT_OK


def cmd_serve_ingest(args: argparse.Namespace) -> int:
    """常驻：接收手机上报的余额。"""
    config = _load(args)
    logger = _logger(config, args)
    if not config.secret("ingest", "key"):
        raise ConfigError(
            "secrets.toml 里没有 [ingest].key。生成一个再填进去：\n"
            '  python -c "import secrets;print(secrets.token_hex(16))"\n'
            '  [ingest]\n  key = "<上面那串>"'
        )
    enabled = not getattr(args, "no_notify", False)

    def record(reading: Reading) -> dict[str, Any]:
        conn = store.connect(config.db_path)
        try:
            store.init_db(conn)
            duplicate = ingest.recent_duplicate(config, conn, reading)
            if duplicate is not None:
                logger.info(
                    "忽略重复上报：%s 元（已有 %s）", reading.balance_yuan, duplicate.captured_at
                )
                return {"inserted": False, "duplicate": True}
            reason = ingest.implausible(config, conn, reading)
            if reason:
                title = "收到一条可疑读数，未入库"
                body = (
                    f"**房间**：{config.room_key}\n\n"
                    f"**收到的数值**：{reading.balance_yuan} 元（来源 {reading.source}）\n\n"
                    f"**为什么可疑**：{reason}\n\n"
                    f"**时间**：{reading.captured_at}\n\n"
                    "如果这确实是你刚充值或大额用电后的余额，手工补录：\n"
                    f"`python -m rest_electric ingest --balance {reading.balance_yuan} --force`"
                )
                _send_once(
                    config, conn, logger, alerts.LEVEL_SUSPECT_READING, title, body, enabled, False
                )
                raise ProtocolError(reason)
            inserted, estimate_result = _record_and_alert(
                config, conn, reading, logger, enabled=enabled
            )
            result: dict[str, Any] = {"inserted": inserted}
            if estimate_result.days_left is not None:
                result["days_left"] = round(estimate_result.days_left, 1)
            return result
        finally:
            conn.close()

    ingest.serve(
        config,
        record,
        logger=logger,
        bind=getattr(args, "bind", None),
        port=getattr(args, "port", None),
    )
    return EXIT_OK


def cmd_ingest(args: argparse.Namespace) -> int:
    """手工补录一条读数（手机上报失败、或读数被差值校验拦下时的兜底）。"""
    config = _load(args)
    logger = _logger(config, args)
    unit = args.unit or config.balance_unit
    reading = Reading(
        room_key=config.room_key,
        balance_cents=to_cents(args.balance, unit),
        captured_at=args.at or clock.iso(),
        source=args.source,
        raw={"ingest": True, "manual": True},
    )

    conn = store.connect(config.db_path)
    try:
        store.init_db(conn)
        if not args.force:
            reason = ingest.implausible(config, conn, reading)
            if reason:
                raise ProtocolError(reason)
        inserted, estimate_result = _record_and_alert(
            config, conn, reading, logger, enabled=not args.no_notify
        )
    finally:
        conn.close()

    print(f"{'已新增' if inserted else '已存在，忽略'}：{reading.balance_yuan} 元 @ {reading.captured_at}")
    if estimate_result.days_left is not None:
        print(f"预计可用　{estimate_result.days_left:.1f} 天")
    return EXIT_OK


def cmd_check_stale(args: argparse.Namespace) -> int:
    """采集端长时间没有新读数时告警。由 systemd timer 定时调用。"""
    config = _load(args)
    logger = _logger(config, args)

    conn = store.connect(config.db_path)
    try:
        store.init_db(conn)
        hours = args.hours or config.ingest_stale_hours
        age = ingest.stale_age_hours(config, conn)
        if age is not None and age <= hours:
            print(f"正常：最后一条读数在 {age:.1f} 小时前（阈值 {hours} 小时）")
            return EXIT_OK

        last = store.last_reading(conn, config.room_key)
        when = "还没有任何读数" if age is None else f"最后一条读数在 {age:.1f} 小时前"
        last_text = f"{last.balance_yuan} 元（{last.captured_at}）" if last else "—"
        title = "电费采集端可能离线"
        body = (
            f"**房间**：{config.room_key}\n\n"
            f"**情况**：{when}（阈值 {hours} 小时）\n\n"
            f"**最后读数**：{last_text}\n\n"
            "请检查手机：微信是否还登录着、自动化脚本是否被杀、Wi-Fi 是否正常。"
        )
        _send_once(
            config, conn, logger, alerts.LEVEL_STALE_INGEST, title, body,
            not getattr(args, "no_notify", False), False,
        )
        print(f"异常：{when}")
        return EXIT_FAIL
    finally:
        conn.close()


def cmd_epay_cookie(args: argparse.Namespace) -> int:
    """把浏览器里的 JSESSIONID 写进 Cookie 文件，先跳过自动登录跑通链路。"""
    config = _load(args)
    raw = (args.cookie or "").strip()
    if not raw:
        raw = input("粘贴 shoucard.shou.edu.cn 的 Cookie（形如 JSESSIONID=xxxx）：").strip()
    if not raw:
        raise ConfigError("Cookie 不能为空")

    pairs: list[tuple[str, str]] = []
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name and value:
            pairs.append((name, value))
    if not pairs:
        raise ConfigError("没解析出任何 Cookie，格式应为 name=value; name2=value2")

    path = config.db_path.parent / epay.COOKIE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for name, value in pairs:
        lines.append(f"shoucard.shou.edu.cn\tFALSE\t/\tFALSE\t0\t{name}\t{value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if os.name == "posix":
        os.chmod(path, 0o600)

    print(f"已写入 {path}（{len(pairs)} 个 Cookie，有效期到浏览器会话结束）")
    print("现在可以直接试：python -m rest_electric once --dry-run")
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

        if config.api_mode == "epay":
            target = f"{config.epay_base_url}{config.epay_result_path}"
            results.append(("OK", "接口配置", f"网页版 {target}"))
            if not config.secret("epay", "username") or not config.secret("epay", "password"):
                results.append(("WARN", "登录凭证", "[epay].username / password 未填，只能靠已保存的 Cookie"))
            else:
                results.append(("OK", "登录凭证", "已配置学号与密码"))

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

    serve_ingest = subparsers.add_parser(
        "serve-ingest", parents=[common], help="常驻接收手机上报的余额（HTTP）"
    )
    serve_ingest.add_argument("--bind", default=None, help="覆盖 [ingest].bind")
    serve_ingest.add_argument("--port", type=int, default=None, help="覆盖 [ingest].port")
    serve_ingest.add_argument("--no-notify", action="store_true", help="只入库不推送")
    serve_ingest.set_defaults(handler=cmd_serve_ingest)

    ingest_cmd = subparsers.add_parser("ingest", parents=[common], help="手工补录一条读数")
    ingest_cmd.add_argument("--balance", required=True, help="余额，例如 24.03")
    ingest_cmd.add_argument(
        "--unit", choices=["yuan", "cent"], default="", help="默认用 [api].balance_unit"
    )
    ingest_cmd.add_argument("--at", default="", help="ISO 8601 时间，默认现在")
    ingest_cmd.add_argument("--source", default="manual", help="来源标记，默认 manual")
    ingest_cmd.add_argument("--force", action="store_true", help="跳过与上一条读数的差值校验")
    ingest_cmd.add_argument("--no-notify", action="store_true", help="只入库不推送")
    ingest_cmd.set_defaults(handler=cmd_ingest)

    stale = subparsers.add_parser(
        "check-stale", parents=[common], help="采集端是否长时间没有上报"
    )
    stale.add_argument("--hours", type=int, default=0, help="超过多少小时算离线，默认用配置")
    stale.add_argument("--no-notify", action="store_true", help="只检查不推送")
    stale.set_defaults(handler=cmd_check_stale)

    cookie = subparsers.add_parser(
        "epay-cookie", parents=[common], help="写入网页端 Cookie（先用浏览器会话跑通，跳过自动登录）"
    )
    cookie.add_argument(
        "--cookie", default="", help="形如 JSESSIONID=xxxx；不填则交互输入（建议交互，别留在历史里）"
    )
    cookie.set_defaults(handler=cmd_epay_cookie)

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
