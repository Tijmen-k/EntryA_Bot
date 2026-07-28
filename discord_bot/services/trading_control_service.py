"""
Trading control: /pause /resume /close /closeall /killswitch.

This is the highest-risk code in the bot — it can flatten real positions and
halt the live engine. State-file mutation follows the exact same
read-merge-write pattern app.py already uses for bias overrides
(_set_bias_override), which main.py's _sync_halt_flag() (see main.py) polls
once per tick (~65s latency, accepted per the implementation plan).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord_bot.config as bot_config
from src.broker.bitget import BitgetBroker


def _read_state() -> dict:
    path = Path(bot_config.STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _write_state_patch(patch: dict) -> None:
    path = Path(bot_config.STATE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_state()
    data.update(patch)
    path.write_text(json.dumps(data, indent=2))


def _set_halted_sync(halted: bool, reason: Optional[str], username: Optional[str]) -> None:
    if halted:
        patch = {
            "trading_halted": True,
            "trading_halted_reason": reason,
            "trading_halted_by": username,
            "trading_halted_at": datetime.now(timezone.utc).isoformat(),
        }
    else:
        patch = {
            "trading_halted": False,
            "trading_halted_reason": None,
            "trading_halted_by": None,
            "trading_halted_at": None,
        }
    _write_state_patch(patch)


async def set_halted(halted: bool, reason: Optional[str] = None, username: Optional[str] = None) -> None:
    await asyncio.to_thread(_set_halted_sync, halted, reason, username)


@dataclass
class CloseResult:
    symbol: str
    side: str
    success: bool
    message: str
    pnl_usdt: Optional[float] = None


def _close_symbol_sync(symbol: str) -> CloseResult:
    broker = BitgetBroker()
    positions = broker.get_open_positions()
    position = next((p for p in positions if p.symbol.upper() == symbol.upper()), None)
    if not position:
        return CloseResult(symbol=symbol, side="-", success=False, message=f"No open position for {symbol}.")

    ok = broker.flash_close(position.side)
    if not ok:
        return CloseResult(symbol=symbol, side=position.side, success=False, message=broker.last_error or "Close failed.")

    # Cancel any leftover SL/TP trigger orders for this symbol — Bitget doesn't
    # always auto-cancel them when a position is closed via flash_close.
    broker.cancel_all_orders()

    closed = broker.get_closed_position_data(position.side)
    pnl = closed["net_pnl"] if closed else None
    return CloseResult(symbol=symbol, side=position.side, success=True, message="Closed at market.", pnl_usdt=pnl)


async def close_symbol(symbol: str) -> CloseResult:
    return await asyncio.to_thread(_close_symbol_sync, symbol)


def _close_all_sync() -> list[CloseResult]:
    broker = BitgetBroker()
    positions = broker.get_open_positions()
    results = []
    for p in positions:
        ok = broker.flash_close(p.side)
        closed = broker.get_closed_position_data(p.side) if ok else None
        results.append(CloseResult(
            symbol=p.symbol, side=p.side, success=ok,
            message="Closed at market." if ok else (broker.last_error or "Close failed."),
            pnl_usdt=closed["net_pnl"] if closed else None,
        ))
    broker.cancel_all_orders()
    return results


async def close_all() -> list[CloseResult]:
    return await asyncio.to_thread(_close_all_sync)


@dataclass
class KillswitchResult:
    positions_closed: int
    positions_failed: int
    orders_cancelled: bool
    pending_entry_cancelled: bool
    details: list[CloseResult] = field(default_factory=list)


def _killswitch_sync(username: str) -> KillswitchResult:
    broker = BitgetBroker()
    positions = broker.get_open_positions()
    details = [
        CloseResult(
            symbol=p.symbol, side=p.side, success=broker.flash_close(p.side),
            message="Closed at market.",
        )
        for p in positions
    ]
    closed_count = sum(1 for d in details if d.success)
    failed_count = len(details) - closed_count

    broker.cancel_all_orders()

    state = _read_state()
    pending = state.get("pending_entry") or {}
    pending_order_id = pending.get("entry_order_id")
    pending_cancelled = False
    if pending_order_id:
        pending_cancelled = broker.cancel_order(pending_order_id)

    _set_halted_sync(True, reason="killswitch", username=username)

    return KillswitchResult(
        positions_closed=closed_count,
        positions_failed=failed_count,
        orders_cancelled=True,
        pending_entry_cancelled=pending_cancelled,
        details=details,
    )


async def killswitch(username: str) -> KillswitchResult:
    return await asyncio.to_thread(_killswitch_sync, username)
