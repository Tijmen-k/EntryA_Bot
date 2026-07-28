"""Cached OHLCV fetch for /chart and /dailyreport — wraps src/data/feed.py."""
from __future__ import annotations

import asyncio
import time

import discord_bot.config as bot_config
from src.data.feed import Bar, BitgetFeed

_ohlcv_cache: dict[str, tuple[float, list[Bar]]] = {}
_price_cache: dict[str, tuple[float, float | None]] = {}
_lock = asyncio.Lock()


async def get_ohlcv_cached(symbol: str, resolution: str, count: int) -> list[Bar]:
    count = min(count, bot_config.MAX_CHART_BARS)
    key = f"{symbol}:{resolution}:{count}"

    async with _lock:
        cached = _ohlcv_cache.get(key)
        if cached and time.monotonic() - cached[0] < bot_config.MARKET_CACHE_TTL_S:
            return cached[1]

    bars = await asyncio.to_thread(BitgetFeed().fetch_ohlcv, symbol, resolution, count)

    async with _lock:
        _ohlcv_cache[key] = (time.monotonic(), bars)
    return bars


async def get_current_price_cached(symbol: str) -> float | None:
    async with _lock:
        cached = _price_cache.get(symbol)
        if cached and time.monotonic() - cached[0] < bot_config.MARKET_CACHE_TTL_S:
            return cached[1]

    price = await asyncio.to_thread(BitgetFeed().get_current_price, symbol)

    async with _lock:
        _price_cache[symbol] = (time.monotonic(), price)
    return price
