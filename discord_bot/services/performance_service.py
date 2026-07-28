"""Performance stats for /performance /history /dailyreport — all sourced from src/journal/db.py."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from src.journal import db as journal_db
from src.risk import sizing


@dataclass
class PerformanceSummary:
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    expectancy: float
    max_drawdown: float
    best_trade: float
    worst_trade: float
    total_pnl: float
    total_fees: float
    daily_pnl_usdt: float
    weekly_pnl_usdt: float
    boost_active: bool
    wins_in_last_6: int
    last_2_wins: bool


def _expectancy(win_rate_pct: float, avg_win: float, avg_loss: float) -> float:
    """avg_loss is stored as a non-positive number (sum of losses / count)."""
    p_win = win_rate_pct / 100
    return p_win * avg_win + (1 - p_win) * avg_loss


async def get_summary() -> PerformanceSummary:
    stats = await asyncio.to_thread(journal_db.get_stats)
    daily = await asyncio.to_thread(journal_db.get_daily_pnl_usdt)
    weekly = await asyncio.to_thread(journal_db.get_weekly_pnl_usdt)
    closed = await asyncio.to_thread(journal_db.get_closed_trades)
    streak = sizing.boost_streak_info(closed)

    return PerformanceSummary(
        total_trades=stats["total_trades"],
        wins=stats["wins"],
        losses=stats["losses"],
        win_rate=stats["win_rate"],
        profit_factor=stats["profit_factor"],
        avg_win=stats["avg_win"],
        avg_loss=stats["avg_loss"],
        expectancy=_expectancy(stats["win_rate"], stats["avg_win"], stats["avg_loss"]),
        max_drawdown=stats["max_drawdown"],
        best_trade=stats["best_trade"],
        worst_trade=stats["worst_trade"],
        total_pnl=stats["total_pnl"],
        total_fees=stats["total_fees"],
        daily_pnl_usdt=daily,
        weekly_pnl_usdt=weekly,
        boost_active=streak["boost_active"],
        wins_in_last_6=streak["wins_in_last_6"],
        last_2_wins=streak["last_2_wins"],
    )


async def get_history(limit: int) -> list[dict]:
    closed = await asyncio.to_thread(journal_db.get_closed_trades)
    return closed[-limit:][::-1]  # most recent first
