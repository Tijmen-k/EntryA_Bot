"""
Central configuration — all values loaded from .env or environment variables.
Live vs demo is controlled by TRADING_MODE=live|demo.
Bitget uses the same API base URL for both; demo is account-level (paper trading).
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Bitget Futures API
# ──────────────────────────────────────────────
TRADING_MODE: str = os.getenv("TRADING_MODE", "demo")  # "demo" | "live"

BITGET_API_KEY: str        = os.getenv("BITGET_API_KEY", "")
BITGET_API_SECRET: str     = os.getenv("BITGET_API_SECRET", "")
BITGET_API_PASSPHRASE: str = os.getenv("BITGET_API_PASSPHRASE", "")

BITGET_BASE_URL: str = "https://api.bitget.com"  # same endpoint for demo & live

# ──────────────────────────────────────────────
# Instrument
# ──────────────────────────────────────────────
SYMBOL: str        = os.getenv("SYMBOL", "BTCUSDT")   # USDT-M linear perpetual
PRODUCT_TYPE: str  = "USDT-FUTURES"
MARGIN_COIN: str   = "USDT"
MARGIN_MODE: str   = os.getenv("MARGIN_MODE", "isolated")  # "crossed" | "isolated"
CONTRACT_SIZE: float   = float(os.getenv("CONTRACT_SIZE", "0.001"))   # BTC per contract for BTCUSDT
PRICE_PRECISION: int   = int(os.getenv("PRICE_PRECISION", "1"))       # decimal places for BTCUSDT prices (tick = 0.1)

CHART_TICK_TYPE: str = "trade"
RESOLUTION: str      = "1m"

# ──────────────────────────────────────────────
# Strategy parameters (per-session, env-overrideable)
# ──────────────────────────────────────────────
LONDON_SL_PCT: float      = float(os.getenv("LONDON_SL_PCT",      "0.0075"))
NY_SL_PCT: float          = float(os.getenv("NY_SL_PCT",          "0.0085"))

# Fixed USDT position size for the algo (0 = use RISK_PER_TRADE_PCT instead)
ALGO_POSITION_USDT: float = float(os.getenv("ALGO_POSITION_USDT", "300.0"))

LIMIT_ENTRY_BUFFER_PCT: float = float(os.getenv("LIMIT_ENTRY_BUFFER_PCT", "0.0003"))

# ──────────────────────────────────────────────
# Sessions (all times in UTC)
# ──────────────────────────────────────────────
@dataclass
class SessionConfig:
    name: str
    orb_start_h: int      # ORB window opens (hour)
    orb_start_m: int      # ORB window opens (minute)
    orb_end_h: int        # ORB window closes
    orb_end_m: int
    session_close_h: int  # session hard close
    session_close_m: int
    sl_pct: float         # stop loss %

SESSIONS: list[SessionConfig] = [
    SessionConfig(
        name="London",
        orb_start_h=int(os.getenv("LONDON_ORB_START_H", "9")),
        orb_start_m=int(os.getenv("LONDON_ORB_START_M", "0")),
        orb_end_h=  int(os.getenv("LONDON_ORB_END_H",   "9")),
        orb_end_m=  int(os.getenv("LONDON_ORB_END_M",   "30")),
        session_close_h=12, session_close_m=0,
        sl_pct=LONDON_SL_PCT,
    ),
    SessionConfig(
        name="NY",
        orb_start_h=int(os.getenv("NY_ORB_START_H", "12")),
        orb_start_m=int(os.getenv("NY_ORB_START_M", "0")),
        orb_end_h=  int(os.getenv("NY_ORB_END_H",   "12")),
        orb_end_m=  int(os.getenv("NY_ORB_END_M",   "30")),
        session_close_h=23, session_close_m=0,
        sl_pct=NY_SL_PCT,
    ),
]

# Active weekdays: Mon=0…Sun=6
ACTIVE_WEEKDAYS: list[int] = [0, 1, 2, 3, 4]

# Cutoff: no new entries within this many minutes of session close
NO_ENTRY_BEFORE_CLOSE_MINS: int = 60

# ──────────────────────────────────────────────
# Strategy parameters
# ──────────────────────────────────────────────
ORB_HIGH_BUFFER_PCT: float = 0.0001
ORB_LOW_BUFFER_PCT: float  = 0.0001
INVALIDATION_PCT: float    = 0.003

# ──────────────────────────────────────────────
# Risk management
# ──────────────────────────────────────────────
RISK_PER_TRADE_PCT: float  = float(os.getenv("RISK_PER_TRADE_PCT", "0.001"))
LEVERAGE: float            = float(os.getenv("LEVERAGE", "50"))
MAX_DAILY_LOSS_PCT: float  = 0.03
MAX_WEEKLY_LOSS_PCT: float = 0.06
COMMISSION_PCT: float      = 0.0002  # ~0.02% maker fee on Bitget futures

# ──────────────────────────────────────────────
# Operational
# ──────────────────────────────────────────────
DRY_RUN: bool  = os.getenv("DRY_RUN", "false").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
STATE_FILE: str = "state/state.json"
LOG_DIR: str    = "logs"

# Retry / network
REQUEST_TIMEOUT_S: int  = 10
MAX_RETRIES: int        = 3
RETRY_DELAY_S: float    = 2.0

# ──────────────────────────────────────────────
# Discord notifications
# ──────────────────────────────────────────────
# Paste your Discord channel webhook URL here (or in .env).
# Leave blank to disable all Discord notifications.
DISCORD_WEBHOOK_URL: str           = os.getenv("DISCORD_WEBHOOK_URL", "")
# How often to send a heartbeat ping (minutes). 0 = disabled.
DISCORD_HEARTBEAT_INTERVAL_MINS: int = int(os.getenv("DISCORD_HEARTBEAT_INTERVAL_MINS", "60"))
