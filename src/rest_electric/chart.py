"""把读数画成 PNG 折线图 / 柱状图（纯标准库，不依赖 matplotlib）。

为什么手写 PNG：邮件客户端普遍不渲染 SVG、也会拦 data URI 图片，
只有内嵌 PNG（CID）是所有客户端都认的。

实现要点：
  * 先按 2 倍画布渲染，再 2×2 平均降采样——用极小的代价换平滑的斜线；
  * 图内不写中文（没有字库），只用 5×7 点阵数字画刻度，
    中文说明放在邮件正文里；
  * 只输出 IHDR / IDAT / IEND 三个块，8 位真彩，所有解码器都认。
"""

from __future__ import annotations

import math
import struct
import zlib
from datetime import date, datetime, time, timedelta

from .clock import TZ
from .models import Reading
from .usage import DayUsage

# ---- 配色（白底，与邮件正文一致）
BG = (255, 255, 255)
GRID = (229, 231, 235)
AXIS = (156, 163, 175)
TEXT = (107, 114, 128)
LINE = (37, 99, 235)
FILL = (219, 234, 254)
BAR = (96, 165, 250)
GREEN = (16, 185, 129)

SCALE = 2  # 超采样倍数

# ---- 5×7 点阵字体：只覆盖数字与分隔符
_GLYPHS = {
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11111", "00010", "00100", "00010", "00001", "10001", "01110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "11110", "00001", "00001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "01100"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "00110", "00110"),
    ":": ("00000", "00110", "00110", "00000", "00110", "00110", "00000"),
    "/": ("00001", "00010", "00010", "00100", "01000", "01000", "10000"),
    " ": ("00000",)*7,
}
GLYPH_W, GLYPH_H = 5, 7


# ---------------------------------------------------------------- 画布


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.w = width
        self.h = height
        self.buf = bytearray(bytes(BG) * (width * height))

    def pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            index = (y * self.w + x) * 3
            self.buf[index : index + 3] = bytes(color)

    def rect(self, x0: int, y0: int, x1: int, y1: int, color) -> None:
        x0, x1 = sorted((int(x0), int(x1)))
        y0, y1 = sorted((int(y0), int(y1)))
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(self.w - 1, x1), min(self.h - 1, y1)
        if x1 < x0 or y1 < y0:
            return
        row = bytes(color) * (x1 - x0 + 1)
        for y in range(y0, y1 + 1):
            start = (y * self.w + x0) * 3
            self.buf[start : start + len(row)] = row

    def line(self, x0: int, y0: int, x1: int, y1: int, color, width: int = 1) -> None:
        """Bresenham + 方形笔刷；宽笔刷保证降采样后线条不会消失。"""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        half = max(0, width // 2)
        while True:
            self.rect(x0 - half, y0 - half, x0 + half, y0 + half, color)
            if x0 == x1 and y0 == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x0 += sx
            if err2 <= dx:
                err += dx
                y0 += sy

    def text(self, content: str, x: int, y: int, color, pixel: int = 2) -> None:
        """用点阵字体画 ASCII 数字/符号；每个字模点放大 pixel 倍。"""
        cursor = x
        for char in content:
            glyph = _GLYPHS.get(char, _GLYPHS[" "])
            for row, bits in enumerate(glyph):
                for col, bit in enumerate(bits):
                    if bit == "1":
                        self.rect(
                            cursor + col * pixel,
                            y + row * pixel,
                            cursor + col * pixel + pixel - 1,
                            y + row * pixel + pixel - 1,
                            color,
                        )
            cursor += (GLYPH_W + 1) * pixel

    def downsample(self) -> "Canvas":
        """2×2 平均降采样。"""
        out = Canvas.__new__(Canvas)
        out.w, out.h = self.w // SCALE, self.h // SCALE
        out.buf = bytearray(out.w * out.h * 3)
        stride = self.w * 3
        for y in range(out.h):
            top = (y * SCALE) * stride
            bottom = top + stride
            row = bytearray(out.w * 3)
            for x in range(out.w):
                a = top + x * SCALE * 3
                b = bottom + x * SCALE * 3
                base = x * 3
                for channel in range(3):
                    row[base + channel] = (
                        self.buf[a + channel]
                        + self.buf[a + 3 + channel]
                        + self.buf[b + channel]
                        + self.buf[b + 3 + channel]
                    ) >> 2
            out.buf[y * out.w * 3 : (y + 1) * out.w * 3] = row
        return out

    def to_png(self) -> bytes:
        final = self.downsample()
        raw = bytearray()
        stride = final.w * 3
        for y in range(final.h):
            raw.append(0)  # filter: none
            raw += final.buf[y * stride : (y + 1) * stride]
        ihdr = struct.pack(">IIBBBBB", final.w, final.h, 8, 2, 0, 0, 0)
        return (
            b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b"")
        )


def _chunk(tag: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + tag
        + payload
        + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
    )


# ---------------------------------------------------------------- 数据


def _nice_ticks(low: float, high: float, count: int = 5) -> list[float]:
    """在 [low, high] 里挑"整齐"的刻度（1/2/2.5/5 系列），大约 count 个。"""
    if high <= low:
        high = low + 1.0
    span = high - low
    magnitude = 10 ** math.floor(math.log10(span / max(1, count)))
    step = magnitude * 10
    for multiplier in (1, 2, 2.5, 5, 10):
        candidate = multiplier * magnitude
        if span / candidate <= count + 1:
            step = candidate
            break
    start = math.ceil(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 1e-9:
        ticks.append(round(value, 6))
        value += step
    return ticks


def _tick_label(value: float, step: float) -> str:
    return f"{value:.2f}" if step < 0.1 else f"{value:.1f}"


# ---------------------------------------------------------------- 折线图


def line_chart(
    points: list[tuple[date, int]],
    *,
    recharge_days: set[date] | None = None,
    width: int = 1000,
    height: int = 360,
) -> bytes:
    """余额趋势。points 为 [(date, cents)]，按日期升序；充值日标绿点。"""
    if len(points) < 2:
        raise ValueError("至少需要两个数据点")
    recharge_days = recharge_days or set()

    canvas = Canvas(width * SCALE, height * SCALE)
    left, right, top, bottom = 84, 28, 26, 46  # 最终像素
    plot_l, plot_r = left * SCALE, (width - right) * SCALE
    plot_t, plot_b = top * SCALE, (height - bottom) * SCALE

    values = [cents / 100.0 for _, cents in points]
    low, high = min(values), max(values)
    pad = max((high - low) * 0.15, 0.05)
    low, high = low - pad, high + pad

    def y_of(value: float) -> int:
        ratio = (value - low) / (high - low)
        return int(plot_b - ratio * (plot_b - plot_t))

    def x_of(index: int) -> int:
        if len(points) == 1:
            return (plot_l + plot_r) // 2
        return int(plot_l + (plot_r - plot_l) * index / (len(points) - 1))

    # 面积填充（先铺底，网格线再叠上去，避免被盖住）
    base_y = plot_b
    for index in range(len(points) - 1):
        x0, x1 = x_of(index), x_of(index + 1)
        y0, y1 = y_of(values[index]), y_of(values[index + 1])
        for x in range(x0, x1 + 1):
            ratio = (x - x0) / max(1, (x1 - x0))
            y = int(y0 + (y1 - y0) * ratio)
            canvas.rect(x, y, x, base_y, FILL)

    # 网格 + Y 轴刻度
    ticks = _nice_ticks(low, high)
    step = (ticks[1] - ticks[0]) if len(ticks) > 1 else (high - low)
    for tick in ticks:
        y = y_of(tick)
        canvas.line(plot_l, y, plot_r, y, GRID, width=2)
        label = _tick_label(tick, step)
        canvas.text(label, plot_l - (len(label) * 12 + 8) * SCALE, y - 7 * SCALE, TEXT, pixel=2 * SCALE)

    # 折线
    for index in range(len(points) - 1):
        canvas.line(
            x_of(index), y_of(values[index]), x_of(index + 1), y_of(values[index + 1]),
            LINE, width=4,
        )
    for index in range(len(points)):
        cx, cy = x_of(index), y_of(values[index])
        color = GREEN if points[index][0] in recharge_days else LINE
        canvas.rect(cx - 5, cy - 5, cx + 5, cy + 5, color)

    # 坐标轴
    canvas.line(plot_l, plot_b, plot_r, plot_b, AXIS, width=2)
    canvas.line(plot_l, plot_t, plot_l, plot_b, AXIS, width=2)

    # X 轴日期标签：最多 7 个
    step = max(1, (len(points) - 1) // 6)
    for index in range(0, len(points), step):
        label = points[index][0].strftime("%m-%d")
        text_width = len(label) * 12 * SCALE
        canvas.text(label, x_of(index) - text_width // 2, plot_b + 12 * SCALE, TEXT, pixel=2 * SCALE)

    return canvas.to_png()


# ---------------------------------------------------------------- 柱状图


def bar_chart(
    days: list[DayUsage],
    *,
    width: int = 1000,
    height: int = 360,
) -> bytes:
    """每日消耗（纵轴单位：元）。柱子是「真实消耗」（已把当天充值加回去），充值日用绿点+金额标出。"""
    if len(days) < 2:
        raise ValueError("至少需要两天数据")

    canvas = Canvas(width * SCALE, height * SCALE)
    left, right, top, bottom = 84, 28, 34, 46
    plot_l, plot_r = left * SCALE, (width - right) * SCALE
    plot_t, plot_b = top * SCALE, (height - bottom) * SCALE

    values = [max(0, item.used_cents) / 100.0 for item in days]
    known = [value for value in values if value > 0]
    high = (max(known) if known else 1.0) * 1.35  # 顶部留出标记充值金额的位置

    def y_of(value: float) -> int:
        return int(plot_b - (value / high) * (plot_b - plot_t))

    for tick in _nice_ticks(0, high):
        y = y_of(tick)
        canvas.line(plot_l, y, plot_r, y, GRID, width=2)
        label = f"{tick:.2f}" if high < 2 else f"{tick:.1f}"
        canvas.text(
            label, plot_l - (len(label) * 12 + 8) * SCALE, y - 7 * SCALE, TEXT, pixel=2 * SCALE
        )

    spacing = (plot_r - plot_l) / max(1, len(days))
    bar_width = max(6, int(spacing * 0.55))
    for index, item in enumerate(days):
        cx = int(plot_l + spacing * (index + 0.5))
        value = values[index]
        top_y = y_of(value) if value > 0 else plot_b
        if value > 0:
            canvas.rect(cx - bar_width // 2, top_y, cx + bar_width // 2, plot_b, BAR)

        if item.recharged_cents > 0:
            label = f"+{item.recharged_cents // 100}"
            dot_y = max(plot_t + 10 * SCALE, top_y - 22 * SCALE)
            canvas.rect(cx - 5 * SCALE, dot_y - 5 * SCALE, cx + 5 * SCALE, dot_y + 5 * SCALE, GREEN)
            text_width = len(label) * 6 * 2 * SCALE
            text_x = cx + 14 * SCALE
            if text_x + text_width > plot_r:
                text_x = cx - 14 * SCALE - text_width
            canvas.text(label, text_x, dot_y - 7 * SCALE, GREEN, pixel=2 * SCALE)

    canvas.line(plot_l, plot_b, plot_r, plot_b, AXIS, width=2)
    canvas.line(plot_l, plot_t, plot_l, plot_b, AXIS, width=2)

    step = max(1, (len(days) - 1) // 6)
    for index in range(0, len(days), step):
        label = days[index].day.strftime("%m-%d")
        text_width = len(label) * 12 * SCALE
        cx = int(plot_l + spacing * (index + 0.5))
        canvas.text(label, cx - text_width // 2, plot_b + 12 * SCALE, TEXT, pixel=2 * SCALE)

    return canvas.to_png()


# ---------------------------------------------------------------- 演示数据


def demo_readings(
    days: int = 14,
    end_balance: float = 22.87,
    recharges: dict[int, float] | None = None,
) -> list[Reading]:
    """造一组像真的读数：每天 08:00–22:00 每 2 小时一条，第 5 天充 1 元、第 9 天充 10 元。

    第 5 天特意用「充值 1 元 < 当天用电」，用来验证"日终对比会算少"的问题。
    """
    recharges = {5: 1.0, 9: 10.0} if recharges is None else recharges
    rates = [0.078, 0.065, 0.092, 0.071, 0.084, 0.062, 0.088, 0.074,
             0.069, 0.081, 0.075, 0.090, 0.066, 0.083]
    today = date.today()
    rows: list[tuple[datetime, float]] = []
    balance = 0.0
    for index in range(days):
        day = today - timedelta(days=days - 1 - index)
        rate = rates[index % len(rates)]
        for slot, hour in enumerate((8, 10, 12, 14, 16, 18, 20, 22)):
            if slot > 0:
                balance -= rate * 2
            if hour == 12 and index in recharges:
                balance += recharges[index]
            rows.append((datetime.combine(day, time(hour=hour), tzinfo=TZ), balance))

    offset = end_balance - rows[-1][1]
    return [
        Reading(
            room_key="demo",
            balance_cents=int(round((value + offset) * 100)),
            captured_at=moment.isoformat(timespec="seconds"),
            source="epay",
        )
        for moment, value in rows
    ]
