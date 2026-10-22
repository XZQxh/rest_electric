"""领域模型与金额换算。

金额在系统内部一律是「整数分」。任何需要元的地方都通过 yuan() 转换，
禁止用 float 参与比较。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .errors import ProtocolError


def yuan(cents: int) -> str:
    """1234 -> '12.34'。仅用于展示。"""
    sign = "-" if cents < 0 else ""
    value = abs(int(cents))
    return f"{sign}{value // 100}.{value % 100:02d}"


def to_cents(value: Any, unit: str = "yuan") -> int:
    """把接口返回值转成整数分。

    unit="yuan" 接受 str/int/float/Decimal，用 Decimal 中转避免二进制浮点误差；
    unit="cent" 直接取整。
    """
    if value is None:
        raise ProtocolError("金额字段为 null")
    if isinstance(value, bool):
        raise ProtocolError(f"金额字段类型异常：{value!r}")
    try:
        decimal = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ProtocolError(f"金额字段无法解析为数字：{value!r}") from exc
    if not decimal.is_finite():
        raise ProtocolError(f"金额字段不是有限数：{value!r}")
    if unit == "cent":
        return int(decimal.to_integral_value())
    return int((decimal * 100).to_integral_value())


@dataclass(frozen=True)
class Reading:
    room_key: str
    balance_cents: int
    captured_at: str
    source: str = "api"
    raw: dict[str, Any] | None = field(default=None, repr=False)

    @property
    def balance_yuan(self) -> str:
        return yuan(self.balance_cents)


@dataclass(frozen=True)
class AlertRecord:
    room_key: str
    level: str
    day: str
    sent_at: str
    detail: str = ""
