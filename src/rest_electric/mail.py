"""每日邮件日报：HTML 正文 + CID 内嵌两张 PNG + 纯文本兜底。

邮件结构（有图时）::

    multipart/related
    ├─ multipart/alternative
    │  ├─ text/plain         report.render() 的原样输出，老客户端兜底
    │  └─ text/html          正文 + <img src="cid:chart-balance"> / chart-usage
    ├─ image/png             Content-ID: <chart-balance>
    └─ image/png             Content-ID: <chart-usage>

为什么用 CID 而不是 data URI：Gmail 会拦 data URI，QQ 邮箱行为也不稳定；
CID 内嵌是 QQ 邮箱 / Gmail / Outlook 都认的做法。图里只有 5×7 点阵数字，
中文全部在 HTML 正文里（没有字库）。

数据不足（少于 2 个数据点）时不画图，正文给一句提示；图表生成抛异常时
整体降级为纯文本邮件，并把 traceback 写进日志——宁可少两张图，不能发不出去。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr
from html import escape
from typing import Sequence

from . import chart, clock, notify, report
from .config import Config
from .errors import ConfigError
from .forecast import Estimate
from .models import AlertRecord, Reading, yuan
from .usage import DayUsage

BALANCE_CID = "chart-balance"
USAGE_CID = "chart-usage"
TABLE_DAYS = 7
ALERT_DAYS = 3

_log = logging.getLogger(__name__)

# 邮件里统一用内联样式：QQ 邮箱会清掉 <style> 块，类选择器不可靠。
_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif"


# ---------------------------------------------------------------- 文本片段


def _usage_map(usages: Sequence[DayUsage]) -> dict[date, DayUsage]:
    return {item.day: item for item in usages}


def _usage_text(item: DayUsage | None) -> str:
    if item is None:
        return "—"
    return f"{item.used_cents / 100:.2f} 元"


def _delta_text(today_item: DayUsage | None, yesterday_item: DayUsage | None) -> str:
    if today_item is None or yesterday_item is None:
        return "—"
    delta = today_item.used_cents - yesterday_item.used_cents
    return f"{delta / 100:+.2f} 元"


def _days_left_text(estimate: Estimate) -> str:
    if estimate.days_left is None:
        return "—"
    if estimate.days_left < 1:
        return "不足 1 天"
    return f"约 {estimate.days_left:.0f} 天"


def _daily_text(estimate: Estimate) -> str:
    if estimate.daily_cents is None:
        return "数据不足"
    return f"日均 {estimate.daily_cents / 100:.2f} 元"


def subject_of(
    today: date,
    points: Sequence[tuple[date, int]],
    estimate: Estimate,
) -> str:
    """主题：`电费日报 2026-10-23 · 余额 22.87 元 · 约 12 天`；缺今日读数加 ⚠️。"""
    if not points:
        return f"⚠️ 电费日报 {today.isoformat()} · 今日无数据"

    last_day, last_balance = points[-1]
    parts = [f"电费日报 {today.isoformat()}", f"余额 {yuan(last_balance)} 元"]
    if estimate.days_left is not None:
        parts.append(_days_left_text(estimate))
    prefix = "" if last_day == today else "⚠️ "
    return prefix + " · ".join(parts)


# ---------------------------------------------------------------- HTML


def _metric(label: str, value: str, note: str = "") -> str:
    note_html = (
        f'<div style="font-size:12px;color:#6b7280;margin-top:2px;">{escape(note)}</div>'
        if note
        else ""
    )
    return (
        '<td width="50%" style="padding:12px 14px;background:#ffffff;'
        'border:1px solid #e5e7eb;border-radius:8px;vertical-align:top;">'
        f'<div style="font-size:12px;color:#6b7280;">{escape(label)}</div>'
        f'<div style="font-size:20px;font-weight:700;color:#111827;margin-top:2px;">'
        f"{escape(value)}</div>{note_html}</td>"
    )


def _html_body(
    config: Config,
    points: Sequence[tuple[date, int]],
    estimate: Estimate,
    alerts: Sequence[AlertRecord],
    usages: Sequence[DayUsage],
    *,
    days: int,
    last_reading: Reading | None,
    has_images: bool,
    now_text: str,
) -> str:
    today = clock.today()
    points = list(points)
    by_day = _usage_map(usages)
    last_day = points[-1][0] if points else None
    last_balance = points[-1][1] if points else None

    today_item = by_day.get(today)
    yesterday_item = by_day.get(today - timedelta(days=1))
    stale_note = ""
    if not points:
        stale_note = "还没有任何读数，采集端可能没有跑起来。"
    elif last_day != today:
        stale_note = f"今天还没有读数（最后一条是 {last_day.isoformat()}），采集端可能离线。"

    parts: list[str] = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f"<title>电费日报 {today.isoformat()}</title></head>",
        f'<body style="margin:0;padding:0;background:#f3f4f6;font-family:{_FONT};">',
        '<div style="max-width:720px;margin:0 auto;padding:20px 12px 28px;">',
        '<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:12px;'
        'padding:20px 20px 8px;">',
        f'<div style="font-size:13px;color:#6b7280;">{escape(config.room_key)} · 宿舍电费日报</div>',
        f'<h1 style="font-size:22px;line-height:1.3;margin:4px 0 14px;color:#111827;">'
        f"电费日报 {today.isoformat()}</h1>",
    ]

    if stale_note:
        parts.append(
            '<div style="background:#fef3c7;border:1px solid #fcd34d;color:#92400e;'
            'padding:10px 12px;border-radius:8px;font-size:14px;margin:0 0 14px;">'
            f"⚠️ {escape(stale_note)}</div>"
        )

    parts.append('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                 'style="border-collapse:separate;border-spacing:8px;margin:0 -8px 4px;">')
    parts.append("<tr>")
    parts.append(
        _metric(
            "当前余额",
            f"{yuan(last_balance)} 元" if last_balance is not None else "暂无数据",
            (
                f"最后读数 {last_reading.captured_at[:16].replace('T', ' ')}"
                if last_reading is not None
                else (f"最后读数 {last_day.isoformat()}" if last_day else "")
            ),
        )
    )
    parts.append(_metric("今日用电", _usage_text(today_item), "截至最后一次采集"))
    parts.append("</tr><tr>")
    parts.append(_metric("较昨日", _delta_text(today_item, yesterday_item), "今日用电 − 昨日用电"))
    parts.append(_metric("预计可用", _days_left_text(estimate), _daily_text(estimate)))
    parts.append("</tr></table>")

    if has_images:
        parts.extend(
            [
                '<h2 style="font-size:15px;color:#111827;margin:18px 0 8px;">'
                f"余额趋势（最近 {days} 天）</h2>",
                f'<img src="cid:{BALANCE_CID}" alt="余额趋势图" width="1000" '
                'style="display:block;width:100%;max-width:1000px;height:auto;'
                'border:1px solid #e5e7eb;border-radius:8px;">',
                '<p style="font-size:12px;color:#6b7280;margin:4px 0 0;">'
                "纵轴单位：元</p>",
                '<h2 style="font-size:15px;color:#111827;margin:18px 0 8px;">'
                f"每日用电（最近 {days} 天，绿点是充值）</h2>",
                f'<img src="cid:{USAGE_CID}" alt="每日用电柱状图" width="1000" '
                'style="display:block;width:100%;max-width:1000px;height:auto;'
                'border:1px solid #e5e7eb;border-radius:8px;">',
                '<p style="font-size:12px;color:#6b7280;margin:4px 0 0;">'
                "纵轴单位：元；柱子为当天真实消耗（已加回充值）</p>",
            ]
        )
    else:
        parts.append(
            '<p style="font-size:13px;color:#6b7280;margin:18px 0 4px;">'
            "数据不足（至少需要两天的读数），明天起才会有趋势图。</p>"
        )

    parts.append(
        '<h2 style="font-size:15px;color:#111827;margin:20px 0 8px;">最近 7 天明细</h2>'
        '<table role="presentation" width="100%" cellpadding="6" cellspacing="0" '
        'style="border-collapse:collapse;font-size:13px;color:#374151;">'
        '<thead><tr style="background:#f9fafb;color:#6b7280;">'
        '<th align="left">日期</th><th align="right">余额(元)</th>'
        '<th align="right">用电(元)</th><th align="right">充值(元)</th></tr></thead><tbody>'
    )
    table_dates = [today - timedelta(days=offset) for offset in range(TABLE_DAYS - 1, -1, -1)]
    if not points and not usages:
        parts.append(
            '<tr><td colspan="4" style="padding:10px 6px;color:#9ca3af;">暂无读数</td></tr>'
        )
    else:
        balances = dict(points)
        for moment in table_dates:
            item = by_day.get(moment)
            balance = balances.get(moment)
            recharge = "—"
            if item is not None and item.recharged_cents > 0:
                recharge = f'+{yuan(item.recharged_cents)}'
            parts.append(
                "<tr>"
                f'<td style="border-top:1px solid #f3f4f6;">{moment.isoformat()}</td>'
                f'<td align="right" style="border-top:1px solid #f3f4f6;">'
                f'{"—" if balance is None else yuan(balance)}</td>'
                f'<td align="right" style="border-top:1px solid #f3f4f6;">'
                f'{"—" if item is None else f"{item.used_cents / 100:.2f}"}</td>'
                f'<td align="right" style="border-top:1px solid #f3f4f6;'
                f'{"color:#059669;font-weight:600;" if recharge != "—" else ""}">'
                f"{recharge}</td>"
                "</tr>"
            )
    parts.append("</tbody></table>")

    parts.append('<h2 style="font-size:15px;color:#111827;margin:20px 0 8px;">最近告警</h2>')
    if alerts:
        items = "".join(
            "<li style=\"margin:4px 0;\">"
            f'{escape(record.sent_at[:16])} · {escape(report.LEVEL_LABELS.get(record.level, record.level))}'
            f" · {escape(record.detail)}</li>"
            for record in alerts[:ALERT_DAYS]
        )
        parts.append(f'<ul style="margin:0;padding-left:20px;font-size:13px;color:#374151;">{items}</ul>')
    else:
        parts.append('<p style="font-size:13px;color:#6b7280;margin:0;">最近没有告警。</p>')

    last_text = last_reading.captured_at if last_reading is not None else (
        f"{last_day.isoformat()}（当天最后一条）" if last_day else "—"
    )
    parts.extend(
        [
            '<hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0 10px;">',
            f'<p style="font-size:12px;color:#6b7280;line-height:1.7;margin:0;">'
            f"房间 {escape(config.room_key)} · 数据来源 {escape(config.api_mode)}<br>"
            f"最后读数 {escape(last_text)} · 生成时间 {escape(now_text)}</p>",
            "</div></div></body></html>",
        ]
    )
    return "".join(parts)


# ---------------------------------------------------------------- 组装


def _addresses(config: Config) -> tuple[str, str]:
    sender = config.secret("notify", "smtp_user")
    return sender, config.report_recipient()


def _apply_headers(message: EmailMessage, config: Config, subject: str) -> None:
    sender, recipient = _addresses(config)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    if "MIME-Version" not in message:
        message["MIME-Version"] = "1.0"
    if sender:
        message["From"] = sender
        domain = parseaddr(sender)[1].rpartition("@")[2]
        message["Message-ID"] = make_msgid(domain=domain or None)
    if recipient:
        message["To"] = recipient


def _assemble(
    config: Config,
    subject: str,
    text: str,
    html_body: str | None,
    images: Sequence[tuple[str, bytes, str]],
) -> EmailMessage:
    if html_body is None:
        message = EmailMessage()
        message.set_content(text)
        _apply_headers(message, config, subject)
        return message

    alternative = EmailMessage()
    alternative.set_content(text)
    alternative.add_alternative(html_body, subtype="html")

    if not images:
        message = alternative
        _apply_headers(message, config, subject)
        return message

    message = EmailMessage()
    message.make_related()
    message.attach(alternative)
    for cid, png, filename in images:
        message.add_related(
            png,
            maintype="image",
            subtype="png",
            cid=f"<{cid}>",
            disposition="inline",
            filename=filename,
        )
    _apply_headers(message, config, subject)
    return message


def build_daily_email(
    config: Config,
    points: Sequence[tuple[date, int]],
    estimate: Estimate,
    alerts: Sequence[AlertRecord] | None = None,
    *,
    day_usages: Sequence[DayUsage] | None = None,
    days: int = 14,
    last_reading: Reading | None = None,
) -> EmailMessage:
    """组装日报邮件；不联网、不落盘。图表异常时自动降级为纯文本。"""
    today = clock.today()
    now_text = clock.now().strftime("%Y-%m-%d %H:%M:%S %z")
    points = sorted(points)
    cutoff = today - timedelta(days=max(1, days) - 1)
    usages = sorted(
        (item for item in (day_usages or []) if item.day >= cutoff),
        key=lambda item: item.day,
    )
    alerts = list(alerts or [])[:ALERT_DAYS]
    subject = subject_of(today, points, estimate)
    text = report.render(
        config, points, estimate, alerts, days=days, last_reading=last_reading
    )

    images: list[tuple[str, bytes, str]] = []
    if len(points) >= 2 and len(usages) >= 2:
        try:
            recharge_days = {item.day for item in usages if item.recharged_cents > 0}
            images = [
                (BALANCE_CID, chart.line_chart(points, recharge_days=recharge_days), "balance.png"),
                (USAGE_CID, chart.bar_chart(usages), "usage.png"),
            ]
        except Exception:  # noqa: BLE001 - 图表不能拖垮整封邮件
            _log.exception("日报图表生成失败，降级为纯文本邮件")
            return _assemble(
                config,
                subject,
                text + "\n\n（图表生成失败，本条为纯文本邮件）",
                None,
                [],
            )

    html_body = _html_body(
        config,
        points,
        estimate,
        alerts,
        usages,
        days=days,
        last_reading=last_reading,
        has_images=bool(images),
        now_text=now_text,
    )
    return _assemble(config, subject, text, html_body, images)


def send_daily_email(config: Config, message: EmailMessage) -> None:
    """通过 [notify] 里的 SMTP 账号发出（与 [notify].channel 无关）。"""
    recipient = config.report_recipient()
    if not recipient:
        raise ConfigError(
            "日报缺少收件人：请配置 [report].to，或复用 [notify].smtp_to"
        )
    missing = [
        key
        for key in ("smtp_host", "smtp_user", "smtp_password")
        if not config.secret("notify", key)
    ]
    if missing:
        raise ConfigError(
            "日报邮件缺少 SMTP 配置：" + "、".join(f"[notify].{key}" for key in missing)
            + "（见 config/secrets.example.toml）"
        )
    if not message["To"]:
        message["To"] = recipient
    notify.smtp_notifier(config, to=recipient).send_message(message)
