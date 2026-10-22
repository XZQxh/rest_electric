"""SQLite 存储层：建表、幂等写入、按天聚合、告警去重。

每天只写 2 行，用 WAL + synchronous=NORMAL 兼顾掉电安全与 SD 卡寿命。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from .clock import parse_day, today
from .models import AlertRecord, Reading

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    room_key      TEXT    NOT NULL,
    balance_cents INTEGER NOT NULL,
    source        TEXT    NOT NULL DEFAULT 'api',
    raw_json      TEXT,
    captured_at   TEXT    NOT NULL,
    UNIQUE (room_key, captured_at)
);

CREATE INDEX IF NOT EXISTS idx_readings_room_time
    ON readings (room_key, captured_at);

CREATE TABLE IF NOT EXISTS alerts_sent (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    room_key TEXT NOT NULL,
    level    TEXT NOT NULL,
    day      TEXT NOT NULL,
    sent_at  TEXT NOT NULL,
    detail   TEXT,
    UNIQUE (room_key, level, day)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    set_meta(conn, "schema_version", str(SCHEMA_VERSION))
    conn.commit()


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return str(row["value"]) if row else default


def insert_reading(conn: sqlite3.Connection, reading: Reading, keep_raw: bool = True) -> bool:
    """写入一条读数。已存在同一 (room_key, captured_at) 时返回 False。"""
    raw_json = None
    if keep_raw and reading.raw is not None:
        raw_json = json.dumps(reading.raw, ensure_ascii=False)[:20000]
    cursor = conn.execute(
        "INSERT OR IGNORE INTO readings (room_key, balance_cents, source, raw_json, captured_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            reading.room_key,
            reading.balance_cents,
            reading.source,
            raw_json,
            reading.captured_at,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def last_reading(conn: sqlite3.Connection, room_key: str) -> Reading | None:
    row = conn.execute(
        "SELECT room_key, balance_cents, captured_at, source "
        "FROM readings WHERE room_key = ? ORDER BY captured_at DESC LIMIT 1",
        (room_key,),
    ).fetchone()
    if not row:
        return None
    return Reading(
        room_key=row["room_key"],
        balance_cents=row["balance_cents"],
        captured_at=row["captured_at"],
        source=row["source"],
    )


def daily_points(
    conn: sqlite3.Connection,
    room_key: str,
    days: int | None = 30,
    until: date | None = None,
) -> list[tuple[date, int]]:
    """按本地日期取当天最后一条读数，返回升序 [(date, balance_cents), ...]。"""
    rows = conn.execute(
        "SELECT captured_at, balance_cents FROM readings "
        "WHERE room_key = ? ORDER BY captured_at",
        (room_key,),
    ).fetchall()

    by_day: dict[date, int] = {}
    for row in rows:
        by_day[parse_day(row["captured_at"])] = int(row["balance_cents"])

    points = sorted(by_day.items())
    if days is not None:
        end = until or today()
        cutoff = end - timedelta(days=days - 1)
        points = [item for item in points if item[0] >= cutoff]
    return points


def already_alerted(
    conn: sqlite3.Connection,
    room_key: str,
    level: str,
    cooldown_days: int = 1,
) -> bool:
    """最近 cooldown_days 天内（含今天）是否已推过同级别告警。"""
    if cooldown_days < 1:
        return False
    cutoff = (today() - timedelta(days=cooldown_days - 1)).isoformat()
    row = conn.execute(
        "SELECT 1 FROM alerts_sent WHERE room_key = ? AND level = ? AND day >= ? LIMIT 1",
        (room_key, level, cutoff),
    ).fetchone()
    return row is not None


def record_alert(conn: sqlite3.Connection, record: AlertRecord) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO alerts_sent (room_key, level, day, sent_at, detail) "
        "VALUES (?, ?, ?, ?, ?)",
        (record.room_key, record.level, record.day, record.sent_at, record.detail),
    )
    conn.commit()


def recent_alerts(conn: sqlite3.Connection, room_key: str, limit: int = 10) -> list[AlertRecord]:
    rows = conn.execute(
        "SELECT room_key, level, day, sent_at, detail FROM alerts_sent "
        "WHERE room_key = ? ORDER BY sent_at DESC LIMIT ?",
        (room_key, limit),
    ).fetchall()
    return [
        AlertRecord(
            room_key=row["room_key"],
            level=row["level"],
            day=row["day"],
            sent_at=row["sent_at"],
            detail=row["detail"] or "",
        )
        for row in rows
    ]
