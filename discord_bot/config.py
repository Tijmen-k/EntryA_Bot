"""
Configuration for the Discord control-center bot.

This is a separate config module from the trading engine's root `config.py` —
that module is a flat namespace `main.py` imports directly, and adding
bot-only concerns (DB path, cache TTLs, embed footer version) there would
couple the engine to a subsystem it doesn't need to know about.

Trading-related constants the bot needs for display are re-exported by
reference from root `config` below — single source of truth, zero duplication.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv

import config as trading_config

load_dotenv()

# ──────────────────────────────────────────────
# Discord bot credentials / registration
# ──────────────────────────────────────────────
DISCORD_BOT_TOKEN: str          = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_GUILD_ID: str           = os.getenv("DISCORD_GUILD_ID", "")
DISCORD_COMMAND_CHANNEL_ID: str = os.getenv("DISCORD_COMMAND_CHANNEL_ID", "")

# ──────────────────────────────────────────────
# Bot-owned storage
# ──────────────────────────────────────────────
BOT_DB_PATH: str = os.getenv("DISCORD_BOT_DB_PATH", "discord_bot/data/bot.db")

# ──────────────────────────────────────────────
# Presentation
# ──────────────────────────────────────────────
BOT_VERSION: str = "1.0.0"
FOOTER_TEXT: str = f"EntryA Control  •  v{BOT_VERSION}"

# ──────────────────────────────────────────────
# Caching / limits
# ──────────────────────────────────────────────
ACCOUNT_CACHE_TTL_S: int = int(os.getenv("DISCORD_ACCOUNT_CACHE_TTL_S", "20"))
MARKET_CACHE_TTL_S: int  = int(os.getenv("DISCORD_MARKET_CACHE_TTL_S", "30"))
MAX_CHART_BARS: int      = 500
MAX_HISTORY_ROWS: int    = 500
CONFIRM_TIMEOUT_S: int   = 30

# ──────────────────────────────────────────────
# Trading engine constants (re-exported by reference — see module docstring)
# ──────────────────────────────────────────────
SYMBOL: str              = trading_config.SYMBOL
TRADING_MODE: str        = trading_config.TRADING_MODE
LEVERAGE: float          = trading_config.LEVERAGE
MARGIN_MODE: str         = trading_config.MARGIN_MODE
LADDER_SCALE_FACTOR: float = trading_config.LADDER_SCALE_FACTOR
STATE_FILE: str          = trading_config.STATE_FILE
LOG_DIR: str             = trading_config.LOG_DIR
DRY_RUN: bool            = trading_config.DRY_RUN

# Base currency symbol for display (e.g. "ETH" from "ETHUSDT")
BASE_CCY: str = SYMBOL
for _suffix in ("USDT", "USD"):
    if BASE_CCY.endswith(_suffix):
        BASE_CCY = BASE_CCY[: -len(_suffix)]
        break

# Project root — used by process control to find/launch main.py
PROJECT_DIR: str = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
