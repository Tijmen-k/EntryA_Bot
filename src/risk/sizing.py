"""
Position sizing for Entry A — single source of truth for the scaling ladder.

Two position size modes per rung:
  - Default  : used when boost conditions are NOT met
  - Boosted  : used when last 6 trades have 4+ wins AND last 2 are both wins

The dashboard imports SCALING_LADDER and is_boost_active() directly.
Never duplicate this table elsewhere.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger
import config


# ── Scaling Ladder ────────────────────────────────────────────────────────────
# Single source of truth. Edit ONLY here — bot and dashboard both import this.
# Rows are (min_equity, default_usdt, boosted_usdt), highest equity first.

_RAW = [
    (40_000.0, 5_200.0, 10_400.0),
    (32_000.0, 4_400.0,  8_800.0),
    (25_000.0, 3_700.0,  7_400.0),
    (20_000.0, 3_100.0,  6_200.0),
    (16_000.0, 2_600.0,  5_200.0),
    (12_500.0, 2_200.0,  4_400.0),
    (10_000.0, 1_850.0,  3_700.0),
    ( 8_000.0, 1_550.0,  3_100.0),
    ( 6_300.0, 1_300.0,  2_600.0),
    ( 5_000.0, 1_100.0,  2_200.0),
    ( 4_000.0,   900.0,  1_800.0),
    ( 3_200.0,   750.0,  1_500.0),
    ( 2_500.0,   625.0,  1_250.0),
    ( 2_000.0,   520.0,  1_040.0),
    ( 1_600.0,   430.0,    860.0),
    ( 1_250.0,   360.0,    720.0),
    ( 1_000.0,   300.0,    600.0),
]


@dataclass
class ScalingRung:
    level:        int    # 1 = lowest equity, N = highest
    min_equity:   float
    default_usdt: float
    boosted_usdt: float


# Build ascending (Level 1 = $1,000, Level 17 = $40,000)
SCALING_LADDER: list[ScalingRung] = [
    ScalingRung(level=i + 1, min_equity=eq, default_usdt=d, boosted_usdt=b)
    for i, (eq, d, b) in enumerate(reversed(_RAW))
]


# ── Ladder helpers ────────────────────────────────────────────────────────────

def get_current_rung(equity: float) -> ScalingRung:
    """Return the highest rung the account has unlocked."""
    result = SCALING_LADDER[0]
    for rung in SCALING_LADDER:
        if equity >= rung.min_equity:
            result = rung
    return result


def get_next_rung(equity: float) -> Optional[ScalingRung]:
    """Return the next locked rung, or None if already at maximum."""
    for rung in SCALING_LADDER:
        if equity < rung.min_equity:
            return rung
    return None


def get_ladder_progress(equity: float) -> tuple[ScalingRung, Optional[ScalingRung], float]:
    """Return (current_rung, next_rung, progress_pct 0-100)."""
    current = get_current_rung(equity)
    nxt     = get_next_rung(equity)
    if nxt is None:
        return current, None, 100.0
    band   = nxt.min_equity - current.min_equity
    filled = equity - current.min_equity
    pct    = min(filled / band * 100, 100.0) if band > 0 else 100.0
    return current, nxt, pct


# ── Boost detection ───────────────────────────────────────────────────────────

def is_boost_active(closed_trades: list[dict]) -> bool:
    """
    Boost is a state, not a per-trade check:
      - TURNS ON  when 4+ of the last 6 closed trades are wins AND the last 2 are both wins
      - STAYS ON  until wins-in-last-6 drops below 4 (a single loss right after
        activation does NOT turn it off, as long as the trailing 6-window still
        has >=4 wins)
    Replayed over the full trade history each call since we don't persist boost
    state anywhere else — cheap for the trade counts this bot sees.
    """
    boosted = False
    for i in range(len(closed_trades)):
        window    = closed_trades[max(0, i - 5): i + 1]
        wins_in_6 = sum(1 for t in window if (t.get("pnl_usdt") or 0) > 0)
        if not boosted:
            last_2 = closed_trades[max(0, i - 1): i + 1]
            last_2_win = len(last_2) == 2 and all((t.get("pnl_usdt") or 0) > 0 for t in last_2)
            if wins_in_6 >= 4 and last_2_win:
                boosted = True
        elif wins_in_6 < 4:
            boosted = False
    return boosted


def get_active_position_usdt(
    equity: float,
    closed_trades: list[dict],
) -> tuple[float, bool]:
    """Return (position_usdt_to_use, is_boosted)."""
    rung    = get_current_rung(equity)
    boosted = is_boost_active(closed_trades)
    size    = rung.boosted_usdt if boosted else rung.default_usdt
    return size, boosted


def boost_streak_info(closed_trades: list[dict]) -> dict:
    """Return dict with wins_in_last_6, last_2_wins, boost_active for display."""
    last_6     = closed_trades[-6:] if len(closed_trades) >= 6 else closed_trades
    wins_in_6  = sum(1 for t in last_6 if (t.get("pnl_usdt") or 0) > 0)
    last_2_win = (
        len(closed_trades) >= 2
        and all((t.get("pnl_usdt") or 0) > 0 for t in closed_trades[-2:])
    )
    return {
        "wins_in_last_6": wins_in_6,
        "trades_in_last_6": len(last_6),
        "last_2_wins": last_2_win,
        "boost_active": is_boost_active(closed_trades),
    }


# ── Risk-based sizer (fallback) ───────────────────────────────────────────────

class PositionSizer:
    def calculate(
        self,
        account_balance: float,
        current_price: float,
        sl_pct: float,
    ) -> float:
        if account_balance <= 0 or current_price <= 0 or sl_pct <= 0:
            logger.warning("Invalid inputs for position sizing")
            return 0.0
        risk_amount       = account_balance * config.RISK_PER_TRADE_PCT
        position_notional = risk_amount / sl_pct
        max_notional      = account_balance * config.LEVERAGE
        if position_notional > max_notional:
            position_notional = max_notional
        contracts = position_notional / current_price
        if contracts < config.CONTRACT_SIZE:
            logger.warning(f"Calculated size {contracts:.6f} below minimum {config.CONTRACT_SIZE}")
            return 0.0
        return round(contracts, 4)