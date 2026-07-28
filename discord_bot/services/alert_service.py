"""CRUD wrapper over discord_bot/db.py's alert_subscriptions table — zero engine calls."""
from __future__ import annotations

import asyncio

import discord_bot.db as bot_db

EVENT_TYPES: tuple[str, ...] = (
    "trade_open", "trade_close", "killswitch", "ladder_boost", "error", "daily_report",
)


async def subscribe(guild_id: str, channel_id: str, event_type: str) -> None:
    await asyncio.to_thread(bot_db.add_subscription, guild_id, channel_id, event_type)


async def unsubscribe(guild_id: str, channel_id: str, event_type: str) -> None:
    await asyncio.to_thread(bot_db.remove_subscription, guild_id, channel_id, event_type)


async def list_subscriptions(guild_id: str) -> list[dict]:
    return await asyncio.to_thread(bot_db.list_subscriptions, guild_id)
