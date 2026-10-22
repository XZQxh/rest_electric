"""读取并校验 TOML 配置。

设计原则：配置错误必须在启动的第一秒以人话报出来，而不是等到半夜查询时才失败。
所有错误都抛 ConfigError，CLI 统一映射为退出码 3。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "config.toml"
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.toml"

_API_MODES = {"fixture", "http"}
_NOTIFY_CHANNELS = {"serverchan", "pushplus", "smtp", "none"}
_UNITS = {"yuan", "cent"}
_METHODS = {"GET", "POST"}


@dataclass(frozen=True)
class Config:
    # [room]
    room_key: str
    building: str
    room: str
    # [storage]
    db_path: Path
    keep_raw: bool
    # [alert]
    balance_floor_cents: int
    days_left_warn: float
    window_days: int
    jitter_minutes: int
    # [http]
    http_timeout: float
    http_attempts: int
    user_agent: str
    # [api]
    api_mode: str
    base_url: str
    balance_path: str
    method: str
    building_param: str
    room_param: str
    balance_field: str
    balance_unit: str
    token_header: str
    token_prefix: str
    extra_body: dict[str, str]
    api_headers: dict[str, str]
    capture_file: Path
    # [notify]
    notify_channel: str
    include_history: bool
    # 元信息
    config_path: Path
    secrets_path: Path | None
    secrets: dict[str, Any] = field(default_factory=dict, repr=False)
    secrets_error: str = ""

    # ---- 便捷访问 ----

    def secret(self, section: str, key: str, default: str = "") -> str:
        table = self.secrets.get(section) or {}
        value = table.get(key, default)
        return "" if value is None else str(value)

    def extra_headers(self) -> dict[str, str]:
        table = self.secrets.get("api") or {}
        raw = table.get("extra_headers") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()}


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"配置文件不存在：{path}\n"
            f"请先复制模板：copy config\\config.example.toml config\\config.toml"
        )
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"配置文件不是合法的 TOML：{path}\n{exc}") from exc


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"缺少配置段 [{name}]")
    return value


def _req_str(table: dict[str, Any], section: str, key: str) -> str:
    if key not in table:
        raise ConfigError(f"缺少配置项 [{section}].{key}")
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}].{key} 必须是非空字符串，当前为 {value!r}")
    return value.strip()


def _opt_str(table: dict[str, Any], key: str, default: str = "") -> str:
    value = table.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"配置项 {key} 必须是字符串，当前为 {value!r}")
    return value.strip()


def _opt_raw_str(table: dict[str, Any], key: str, default: str = "") -> str:
    """与 _opt_str 相同，但保留原样空格。

    用于 token 前缀这类「末尾空格有语义」的字段（"Bearer " 少了空格会被服务端拒绝）。
    """
    value = table.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"配置项 {key} 必须是字符串，当前为 {value!r}")
    return value


def _opt_int(table: dict[str, Any], key: str, default: int, minimum: int | None = None) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"配置项 {key} 必须是整数，当前为 {value!r}")
    if minimum is not None and value < minimum:
        raise ConfigError(f"配置项 {key} 不能小于 {minimum}，当前为 {value}")
    return value


def _opt_float(table: dict[str, Any], key: str, default: float, minimum: float | None = None) -> float:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"配置项 {key} 必须是数字，当前为 {value!r}")
    number = float(value)
    if minimum is not None and number < minimum:
        raise ConfigError(f"配置项 {key} 不能小于 {minimum}，当前为 {number}")
    return number


def _opt_bool(table: dict[str, Any], key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"配置项 {key} 必须是 true/false，当前为 {value!r}")
    return value


def _opt_str_table(
    table: dict[str, Any], key: str, section: str
) -> dict[str, str]:
    """读取一个「键值都是字符串」的子表，用于静态请求头与固定请求体字段。"""
    value = table.get(key, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{section}].{key} 必须是表（table），当前为 {value!r}")
    result: dict[str, str] = {}
    for item_key, item_value in value.items():
        if isinstance(item_value, (dict, list)):
            raise ConfigError(f"[{section}].{key}.{item_key} 必须是字符串或数字")
        result[str(item_key)] = str(item_value)
    return result


def _resolve(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def _load_secrets(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, f"密钥文件不存在：{path}（发送推送与真实查询前需要）"
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh), ""
    except tomllib.TOMLDecodeError as exc:
        return {}, f"密钥文件不是合法的 TOML：{path}：{exc}"


def load_config(
    config_path: Path | str | None = None,
    secrets_path: Path | str | None = None,
) -> Config:
    """读取配置。路径缺省为 config/config.toml 与 config/secrets.toml。"""
    cfg_path = Path(config_path) if config_path else DEFAULT_CONFIG
    sec_path = Path(secrets_path) if secrets_path else cfg_path.with_name("secrets.toml")

    data = _load_toml(cfg_path)

    room = _table(data, "room")
    storage = _table(data, "storage")
    alert = _table(data, "alert")
    http = _table(data, "http")
    api = _table(data, "api")
    notify = _table(data, "notify")

    api_mode = _opt_str(api, "mode", "fixture").lower()
    if api_mode not in _API_MODES:
        raise ConfigError(f"[api].mode 只能是 {sorted(_API_MODES)}，当前为 {api_mode!r}")

    channel = _opt_str(notify, "channel", "none").lower()
    if channel not in _NOTIFY_CHANNELS:
        raise ConfigError(f"[notify].channel 只能是 {sorted(_NOTIFY_CHANNELS)}，当前为 {channel!r}")

    unit = _opt_str(api, "balance_unit", "yuan").lower()
    if unit not in _UNITS:
        raise ConfigError(f"[api].balance_unit 只能是 {sorted(_UNITS)}，当前为 {unit!r}")

    method = _opt_str(api, "method", "GET").upper()
    if method not in _METHODS:
        raise ConfigError(f"[api].method 只能是 {sorted(_METHODS)}，当前为 {method!r}")

    balance_field = _opt_str(api, "balance_field", "")
    if api_mode == "http" and not balance_field:
        raise ConfigError("[api].balance_field 在 http 模式下必填，例如 data.balance")

    capture_file = _resolve(_opt_str(api, "capture_file", "capture/sample_response.json"))
    if api_mode == "fixture" and not capture_file.exists():
        raise ConfigError(
            f"[api].mode=fixture 但样本文件不存在：{capture_file}\n"
            f"请按 docs/recon-sop.md 抓包后放入样本，或先把 mode 改成 http。"
        )

    secrets, secrets_error = _load_secrets(sec_path)

    return Config(
        room_key=_req_str(room, "room", "room_key"),
        building=_opt_str(room, "building", ""),
        room=_opt_str(room, "room", ""),
        db_path=_resolve(_opt_str(storage, "db_path", "data/electric.db")),
        keep_raw=_opt_bool(storage, "keep_raw", True),
        balance_floor_cents=_opt_int(alert, "balance_floor_cents", 2000, minimum=0),
        days_left_warn=_opt_float(alert, "days_left_warn", 3.0, minimum=0.0),
        window_days=_opt_int(alert, "window_days", 7, minimum=2),
        jitter_minutes=_opt_int(alert, "jitter_minutes", 5, minimum=0),
        http_timeout=_opt_float(http, "timeout_seconds", 15.0, minimum=1.0),
        http_attempts=_opt_int(http, "attempts", 3, minimum=1),
        user_agent=_opt_str(http, "user_agent", "rest_electric/0.1"),
        api_mode=api_mode,
        base_url=_opt_str(api, "base_url", "").rstrip("/"),
        balance_path=_opt_str(api, "balance_path", ""),
        method=method,
        building_param=_opt_str(api, "building_param", "buildingId"),
        room_param=_opt_str(api, "room_param", "roomId"),
        balance_field=balance_field,
        balance_unit=unit,
        token_header=_opt_str(api, "token_header", "Authorization"),
        token_prefix=_opt_raw_str(api, "token_prefix", ""),
        extra_body=_opt_str_table(api, "extra_body", "api"),
        api_headers=_opt_str_table(api, "headers", "api"),
        capture_file=capture_file,
        notify_channel=channel,
        include_history=_opt_bool(notify, "include_history", True),
        config_path=cfg_path,
        secrets_path=sec_path if sec_path.exists() else None,
        secrets=secrets,
        secrets_error=secrets_error,
    )
