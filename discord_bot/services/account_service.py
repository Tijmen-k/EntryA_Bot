"""
Read-only account/market data for /balance /positions /position /orders.

Every value here comes from the existing engine modules (src/broker,
src/data, src/risk) — this module only adds a shared TTL cache (so several
Discord users hitting these commands don't hammer the single Bitget account)
and reshapes the results into small dataclasses for the cogs to format.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import config as trading_config
import discord_bot.config as bot_config
from src.broker.bitget import BitgetBroker, Order, Position
from src.data.feed import BitgetFeed
from src.journal import db as journal_db
from src.risk import sizing


class _TTLCache:
    """Tiny shared cache — one entry per key, expires after `ttl_s` seconds."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()

    async def get_or_set(self, key: str, ttl_s: int, factory):
        async with self._lock:
            cached = self._store.get(key)
            now = time.monotonic()
            if cached and now - cached[0] < ttl_s:
                return cached[1]
        value = await asyncio.to_thread(factory)
        async with self._lock:
            self._store[key] = (time.monotonic(), value)
        return value


_cache = _TTLCache()


@dataclass
class LadderStatus:
    level: int
    default_usdt: float
    boosted_usdt: float
    boost_active: bool
    active_size_usdt: float
    progress_pct: float
    next_level_equity: Optional[float]


@dataclass
class BalanceSnapshot:
    balance_usdt: float
    ladder: LadderStatus


async def get_balance() -> BalanceSnapshot:
    balance = await _cache.get_or_set(
        "balance", bot_config.ACCOUNT_CACHE_TTL_S, lambda: BitgetBroker().get_account_balance()
    )
    closed_trades = await asyncio.to_thread(journal_db.get_closed_trades)

    rung = sizing.get_current_rung(balance)
    current, nxt, progress_pct = sizing.get_ladder_progress(balance)
    size_usdt, boosted = sizing.get_active_position_usdt(balance, closed_trades)

    ladder = LadderStatus(
        level=rung.level,
        default_usdt=rung.default_usdt,
        boosted_usdt=rung.boosted_usdt,
        boost_active=boosted,
        active_size_usdt=size_usdt,
        progress_pct=progress_pct,
        next_level_equity=nxt.min_equity if nxt else None,
    )
    return BalanceSnapshot(balance_usdt=balance, ladder=ladder)


async def get_positions() -> list[Position]:
    return await _cache.get_or_set(
        "positions", bot_config.ACCOUNT_CACHE_TTL_S, lambda: BitgetBroker().get_open_positions()
    )


async def get_orders() -> list[Order]:
    return await _cache.get_or_set(
        "orders", bot_config.ACCOUNT_CACHE_TTL_S, lambda: BitgetBroker().get_open_orders()
    )


async def get_current_price() -> Optional[float]:
    return await _cache.get_or_set(
        "price", bot_config.MARKET_CACHE_TTL_S,
        lambda: BitgetFeed().get_current_price(trading_config.SYMBOL),
    )


def _read_open_trade_signal() -> Optional[dict]:
    """SL/TP live in the strategy signal (state.json), not as regular pending orders —
    Bitget places them as preset/plan orders attached to the position, which
    get_open_orders() (pending limit/market orders only) can never see."""
    try:
        path = Path(bot_config.STATE_FILE)
        if not path.exists():
            return None
        state = json.loads(path.read_text())
    except Exception:
        return None
    open_trade = state.get("open_trade")
    return (open_trade or {}).get("signal")


@dataclass
class PositionDetail:
    position: Position
    mark_price: Optional[float]
    unrealised_pnl_pct: Optional[float]
    sl_price: Optional[float]
    tp_price: Optional[float]
    session: Optional[str]
    entry_time: Optional[str]


async def get_position_detail(symbol: str) -> Optional[PositionDetail]:
    positions = await get_positions()
    position = next((p for p in positions if p.symbol.upper() == symbol.upper()), None)
    if not position:
        return None

    price = await get_current_price()
    unrealised_pct = None
    if price and position.entry_price:
        direction = 1 if position.side == "long" else -1
        unrealised_pct = direction * (price - position.entry_price) / position.entry_price * 100

    signal = await asyncio.to_thread(_read_open_trade_signal)
    sl_price = (signal or {}).get("sl_price")
    tp_price = (signal or {}).get("tp_price")

    if sl_price is None or tp_price is None:
        # Fallback for positions the strategy didn't open (e.g. manual trades)
        orders = await get_orders()
        symbol_orders = [o for o in orders if o.symbol.upper() == symbol.upper()]
        if sl_price is None:
            order_sl = next((o for o in symbol_orders if o.order_type == "stop"), None)
            sl_price = order_sl.stop_price if order_sl else None
        if tp_price is None:
            order_tp = next((o for o in symbol_orders if o.limit_price and o.order_type != "stop"), None)
            tp_price = order_tp.limit_price if order_tp else None

    open_trades = await asyncio.to_thread(journal_db.get_open_journal_trades)
    journal_row = next((t for t in open_trades if t["symbol"].upper() == symbol.upper()), None)

    return PositionDetail(
        position=position,
        mark_price=price,
        unrealised_pnl_pct=unrealised_pct,
        sl_price=sl_price,
        tp_price=tp_price,
        session=journal_row["session"] if journal_row else None,
        entry_time=journal_row["entry_time"] if journal_row else None,
    )
