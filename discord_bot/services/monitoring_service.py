"""Engine status snapshot for /status — reads state.json + process state, no engine changes required."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import discord_bot.config as bot_config
from src.utils import process_control


@dataclass
class StatusSnapshot:
    engine_running: bool
    dry_run: bool
    trading_halted: bool
    halted_reason: Optional[str]
    active_session: Optional[str]
    phase: Optional[str]
    has_open_position: bool
    has_pending_entry: bool
    symbol: str
    trading_mode: str
    daily_pnl_pct: float
    weekly_pnl_pct: float


def _read_state() -> dict:
    path = Path(bot_config.STATE_FILE)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


async def get_status_snapshot() -> StatusSnapshot:
    running, state = await asyncio.gather(
        asyncio.to_thread(process_control.is_running, bot_config.PROJECT_DIR),
        asyncio.to_thread(_read_state),
    )

    session = state.get("active_session")
    phase = None
    if session == "London":
        phase = state.get("london_phase")
    elif session == "NY":
        phase = state.get("ny_phase")

    return StatusSnapshot(
        engine_running=running,
        dry_run=bot_config.DRY_RUN,
        trading_halted=bool(state.get("trading_halted", False)),
        halted_reason=state.get("trading_halted_reason"),
        active_session=session,
        phase=phase,
        has_open_position=state.get("open_trade") is not None,
        has_pending_entry=state.get("pending_entry") is not None,
        symbol=bot_config.SYMBOL,
        trading_mode=bot_config.TRADING_MODE,
        daily_pnl_pct=float(state.get("daily_pnl_pct") or 0.0),
        weekly_pnl_pct=float(state.get("weekly_pnl_pct") or 0.0),
    )
