"""/restart — wraps src/utils/process_control.py (local Windows process management)."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import discord_bot.config as bot_config
from src.utils import process_control


@dataclass
class RestartResult:
    was_running: bool
    stopped: bool
    started: bool
    new_pid: int | None


async def restart_engine() -> RestartResult:
    was_running = await asyncio.to_thread(process_control.is_running, bot_config.PROJECT_DIR)
    stopped = True
    if was_running:
        stopped = await asyncio.to_thread(process_control.stop, bot_config.PROJECT_DIR)

    proc = await asyncio.to_thread(process_control.start, bot_config.PROJECT_DIR, False)
    started = await asyncio.to_thread(process_control.is_running, bot_config.PROJECT_DIR)
    return RestartResult(was_running=was_running, stopped=stopped, started=started, new_pid=proc.pid if started else None)
