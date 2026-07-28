"""
SQLite storage for the Discord bot's own concerns: per-guild alert
subscriptions, a health snapshot for /health, and an audit trail of every
mutating command. Trade/performance data is never duplicated here — it's
always read live from src/journal/db.py.

Sync stdlib sqlite3, same style as src/journal/db.py. Swappable for
PostgreSQL later by replacing this module's connection/query layer only —
callers depend on the function signatures, not on sqlite3 directly.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord_bot.config as bot_config

_ALLOWED_EVENT_TYPES = (
    "trade_open", "trade_close", "killswitch", "ladder_boost", "error", "daily_report",
)


def _conn() -> sqlite3.Connection:
    Path(bot_config.BOT_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(bot_config.BOT_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id         TEXT PRIMARY KEY,
                alert_channel_id TEXT,
                timezone_display TEXT NOT NULL DEFAULT 'UTC',
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            )
        """)
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS alert_subscriptions (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT    NOT NULL,
                channel_id  TEXT    NOT NULL,
                event_type  TEXT    NOT NULL CHECK (event_type IN {_ALLOWED_EVENT_TYPES!r}),
                enabled     INTEGER NOT NULL DEFAULT 1,
                created_at  TEXT    NOT NULL,
                updated_at  TEXT    NOT NULL,
                UNIQUE(guild_id, channel_id, event_type)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS health_snapshot (
                component          TEXT PRIMARY KEY,
                last_exception      TEXT,
                last_exception_at   TEXT,
                last_ok_at          TEXT,
                updated_at          TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS command_audit (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id    TEXT,
                user_id     TEXT NOT NULL,
                username    TEXT NOT NULL,
                command     TEXT NOT NULL,
                params      TEXT NOT NULL DEFAULT '',
                result      TEXT NOT NULL,
                executed_at TEXT NOT NULL
            )
        """)
        conn.commit()


# ── Alert subscriptions ────────────────────────────────────────────────────────

def add_subscription(guild_id: str, channel_id: str, event_type: str) -> None:
    if event_type not in _ALLOWED_EVENT_TYPES:
        raise ValueError(f"Unknown event_type: {event_type}")
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO alert_subscriptions (guild_id, channel_id, event_type, enabled, created_at, updated_at)
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(guild_id, channel_id, event_type)
            DO UPDATE SET enabled = 1, updated_at = excluded.updated_at
        """, (guild_id, channel_id, event_type, now, now))
        conn.commit()


def remove_subscription(guild_id: str, channel_id: str, event_type: str) -> None:
    with _conn() as conn:
        conn.execute("""
            UPDATE alert_subscriptions SET enabled = 0, updated_at = ?
            WHERE guild_id = ? AND channel_id = ? AND event_type = ?
        """, (datetime.now(timezone.utc).isoformat(), guild_id, channel_id, event_type))
        conn.commit()


def list_subscriptions(guild_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM alert_subscriptions
            WHERE guild_id = ? AND enabled = 1
            ORDER BY event_type ASC
        """, (guild_id,)).fetchall()
    return [dict(r) for r in rows]


def list_all_active_subscriptions(event_type: str) -> list[dict]:
    """Every enabled subscription for an event type, across all guilds — used by the alert poller."""
    with _conn() as conn:
        rows = conn.execute("""
            SELECT * FROM alert_subscriptions WHERE event_type = ? AND enabled = 1
        """, (event_type,)).fetchall()
    return [dict(r) for r in rows]


# ── Health snapshot ─────────────────────────────────────────────────────────────

def record_exception(component: str, exc: BaseException) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO health_snapshot (component, last_exception, last_exception_at, last_ok_at, updated_at)
            VALUES (?, ?, ?, NULL, ?)
            ON CONFLICT(component) DO UPDATE SET
                last_exception = excluded.last_exception,
                last_exception_at = excluded.last_exception_at,
                updated_at = excluded.updated_at
        """, (component, f"{type(exc).__name__}: {exc}", now, now))
        conn.commit()


def record_ok(component: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO health_snapshot (component, last_exception, last_exception_at, last_ok_at, updated_at)
            VALUES (?, NULL, NULL, ?, ?)
            ON CONFLICT(component) DO UPDATE SET last_ok_at = excluded.last_ok_at, updated_at = excluded.updated_at
        """, (component, now, now))
        conn.commit()


def get_health_snapshot(component: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM health_snapshot WHERE component = ?", (component,)
        ).fetchone()
    return dict(row) if row else None


# ── Command audit ──────────────────────────────────────────────────────────────

def log_command(
    guild_id: Optional[str],
    user_id: str,
    username: str,
    command: str,
    params: str,
    result: str,
) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT INTO command_audit (guild_id, user_id, username, command, params, result, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (guild_id, user_id, username, command, params, result, datetime.now(timezone.utc).isoformat()))
        conn.commit()
