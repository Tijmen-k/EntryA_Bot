"""SQLite-backed trade journal.

Stores every opened and closed trade with performance data.
All times are stored as ISO-8601 UTC strings.
"""
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_PATH = "state/journal.db"

# ── Connection ────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id      TEXT    UNIQUE NOT NULL,
                symbol        TEXT    NOT NULL,
                side          TEXT    NOT NULL,
                entry_time    TEXT,
                exit_time     TEXT,
                entry_price   REAL,
                exit_price    REAL,
                size_btc      REAL,
                notional_usdt REAL,
                pnl_usdt      REAL,
                pnl_pct       REAL,
                fees_usdt     REAL    DEFAULT 0,
                session       TEXT    DEFAULT 'Manual',
                source        TEXT    DEFAULT 'manual',
                leverage      INTEGER DEFAULT 1,
                notes         TEXT    DEFAULT '',
                status        TEXT    DEFAULT 'open'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS position_boosts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                date                TEXT    NOT NULL,
                trade_count         INTEGER DEFAULT 0,
                old_level           INTEGER NOT NULL,
                new_level           INTEGER NOT NULL,
                old_position_usdt   REAL    NOT NULL,
                new_position_usdt   REAL    NOT NULL,
                equity              REAL    NOT NULL,
                risk_pct            REAL    NOT NULL,
                notes               TEXT    DEFAULT ''
            )
        """)
        conn.commit()

# ── Write operations ──────────────────────────────────────────────────────────

def open_trade(
    trade_id: str,
    symbol: str,
    side: str,
    entry_price: float,
    size_btc: float,
    notional_usdt: float,
    session: str = "Manual",
    source: str = "manual",
    leverage: int = 1,
) -> None:
    with _conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO trades
              (trade_id, symbol, side, entry_time, entry_price, size_btc,
               notional_usdt, session, source, leverage, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')
        """, (
            trade_id, symbol, side,
            datetime.now(timezone.utc).isoformat(),
            entry_price, size_btc, notional_usdt,
            session, source, leverage,
        ))
        conn.commit()


def close_trade(
    trade_id: str,
    exit_price: float,
    pnl_usdt: float,
    pnl_pct: float,
    fees_usdt: float = 0.0,
) -> None:
    with _conn() as conn:
        conn.execute("""
            UPDATE trades SET
                exit_time  = ?,
                exit_price = ?,
                pnl_usdt   = ?,
                pnl_pct    = ?,
                fees_usdt  = ?,
                status     = 'closed'
            WHERE trade_id = ?
        """, (
            datetime.now(timezone.utc).isoformat(),
            exit_price, pnl_usdt, pnl_pct, fees_usdt,
            trade_id,
        ))
        conn.commit()


def update_notes(trade_id: str, notes: str) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE trades SET notes = ? WHERE trade_id = ?",
            (notes, trade_id),
        )
        conn.commit()


def delete_trade(trade_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM trades WHERE trade_id = ?", (trade_id,))
        conn.commit()


# ── Position boost history ────────────────────────────────────────────────────

def log_boost(
    old_level: int,
    new_level: int,
    old_position_usdt: float,
    new_position_usdt: float,
    equity: float,
    risk_pct: float,
    notes: str = "",
) -> None:
    with _conn() as conn:
        trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        conn.execute("""
            INSERT INTO position_boosts
              (date, trade_count, old_level, new_level,
               old_position_usdt, new_position_usdt, equity, risk_pct, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade_count,
            old_level, new_level,
            old_position_usdt, new_position_usdt,
            equity, risk_pct, notes,
        ))
        conn.commit()


def get_boosts() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM position_boosts ORDER BY date DESC"
        ).fetchall()
    return [dict(r) for r in rows]

# ── Read operations ───────────────────────────────────────────────────────────

def get_all_trades() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY entry_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_closed_trades() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY entry_time ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_open_journal_trades() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY entry_time DESC"
        ).fetchall()
    return [dict(r) for r in rows]

# ── Stats ─────────────────────────────────────────────────────────────────────

def get_daily_pnl_usdt() -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE status='closed' AND date(exit_time) = ?",
            (today,),
        ).fetchone()
    return float(row[0])


def get_weekly_pnl_usdt() -> float:
    from datetime import timedelta
    now   = datetime.now(timezone.utc)
    start = (now - timedelta(days=now.weekday())).date().isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(pnl_usdt), 0) FROM trades WHERE status='closed' AND date(exit_time) >= ?",
            (start,),
        ).fetchone()
    return float(row[0])


def reset_journal() -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM trades")
        conn.execute("DELETE FROM position_boosts")
        conn.commit()


def get_stats() -> dict:
    closed = get_closed_trades()
    empty = {
        "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
        "total_pnl": 0.0, "avg_win": 0.0, "avg_loss": 0.0,
        "profit_factor": 0.0, "max_drawdown": 0.0,
        "best_trade": 0.0, "worst_trade": 0.0, "total_fees": 0.0,
    }
    if not closed:
        return empty

    pnls   = [t["pnl_usdt"] or 0.0 for t in closed]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_profit = sum(wins)
    gross_loss   = abs(sum(losses))

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    return {
        "total_trades":  len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      len(wins) / len(closed) * 100 if closed else 0.0,
        "total_pnl":     sum(pnls),
        "avg_win":       gross_profit / len(wins)   if wins   else 0.0,
        "avg_loss":      sum(losses)  / len(losses) if losses else 0.0,
        "profit_factor": gross_profit / gross_loss  if gross_loss else float("inf"),
        "max_drawdown":  max_dd,
        "best_trade":    max(pnls) if pnls else 0.0,
        "worst_trade":   min(pnls) if pnls else 0.0,
        "total_fees":    sum(t.get("fees_usdt") or 0.0 for t in closed),
    }
