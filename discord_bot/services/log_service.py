"""
Tail the engine's existing rotating loguru log files for /logs — no new log
storage, this reads directly from the same files main.py already writes
(logs/bot_{date}.log and logs/trades.log, see src/utils/logger.py).
"""
from __future__ import annotations

import asyncio
import glob
import os
from typing import Literal

import discord_bot.config as bot_config

LogCategory = Literal["errors", "warnings", "trades", "system"]

_LEVEL_BY_CATEGORY: dict[str, tuple[str, ...]] = {
    "errors":   ("ERROR", "CRITICAL"),
    "warnings": ("WARNING",),
}


def _latest_bot_log() -> str | None:
    pattern = os.path.join(bot_config.LOG_DIR, "bot_*.log")
    matches = [f for f in glob.glob(pattern) if not f.endswith(".gz")]
    if not matches:
        return None
    return max(matches, key=os.path.getmtime)


def _tail_lines(path: str, n: int) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-n:]]


def _read_logs_sync(category: LogCategory, keyword: str | None, n: int) -> list[str]:
    if category == "trades":
        path = os.path.join(bot_config.LOG_DIR, "trades.log")
        lines = _tail_lines(path, n * 5)  # over-fetch since we'll filter by keyword below
    else:
        path = _latest_bot_log()
        if not path:
            return []
        lines = _tail_lines(path, n * 20)  # over-fetch since level filtering discards most lines
        levels = _LEVEL_BY_CATEGORY.get(category)
        if levels:
            lines = [ln for ln in lines if any(f"| {lvl:<8}|" in ln or f"| {lvl} " in ln for lvl in levels)]

    if keyword:
        needle = keyword.lower()
        lines = [ln for ln in lines if needle in ln.lower()]

    return lines[-n:]


async def tail_logs(category: LogCategory, keyword: str | None, n: int) -> list[str]:
    return await asyncio.to_thread(_read_logs_sync, category, keyword, n)
