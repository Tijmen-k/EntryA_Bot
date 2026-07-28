"""Diagnostics for /health — CPU/RAM/disk, DB reachability, exchange latency, git commit, last exception."""
from __future__ import annotations

import asyncio
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil

import discord_bot.config as bot_config
import discord_bot.db as bot_db
from src.broker.bitget import BitgetBroker
from src.journal import db as journal_db
from src.utils import process_control

_COMPONENT = "discord_bot"
_git_commit_cache: Optional[str] = None


def _git_commit() -> Optional[str]:
    global _git_commit_cache
    if _git_commit_cache is not None:
        return _git_commit_cache
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=bot_config.PROJECT_DIR, capture_output=True, text=True, timeout=5,
        )
        _git_commit_cache = result.stdout.strip() if result.returncode == 0 else None
    except Exception:
        _git_commit_cache = None
    return _git_commit_cache


@dataclass
class HealthReport:
    cpu_pct: float
    ram_pct: float
    disk_pct: float
    db_ok: bool
    exchange_ok: bool
    exchange_latency_ms: Optional[float]
    engine_running: bool
    git_commit: Optional[str]
    last_exception: Optional[str]
    last_exception_at: Optional[str]


def _check_db() -> bool:
    try:
        journal_db.init_db()
        journal_db.get_all_trades()
        return True
    except Exception:
        return False


def _check_exchange() -> tuple[bool, Optional[float]]:
    start = time.monotonic()
    try:
        broker = BitgetBroker()
        broker.get_account_balance()
        elapsed_ms = (time.monotonic() - start) * 1000
        return True, elapsed_ms
    except Exception:
        return False, None


async def build_health_report() -> HealthReport:
    cpu, ram, disk, db_ok, (exchange_ok, exchange_ms), running, snapshot = await asyncio.gather(
        asyncio.to_thread(psutil.cpu_percent, 0.3),
        asyncio.to_thread(lambda: psutil.virtual_memory().percent),
        asyncio.to_thread(lambda: psutil.disk_usage(str(Path(bot_config.PROJECT_DIR))).percent),
        asyncio.to_thread(_check_db),
        asyncio.to_thread(_check_exchange),
        asyncio.to_thread(process_control.is_running, bot_config.PROJECT_DIR),
        asyncio.to_thread(bot_db.get_health_snapshot, _COMPONENT),
    )

    return HealthReport(
        cpu_pct=cpu,
        ram_pct=ram,
        disk_pct=disk,
        db_ok=db_ok,
        exchange_ok=exchange_ok,
        exchange_latency_ms=exchange_ms,
        engine_running=running,
        git_commit=_git_commit(),
        last_exception=snapshot.get("last_exception") if snapshot else None,
        last_exception_at=snapshot.get("last_exception_at") if snapshot else None,
    )


def record_exception(exc: BaseException) -> None:
    bot_db.record_exception(_COMPONENT, exc)


def record_ok() -> None:
    bot_db.record_ok(_COMPONENT)
