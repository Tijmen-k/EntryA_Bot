"""
Central configuration — all values loaded from .env or environment variables.
Live vs demo is controlled by TRADING_MODE=live|demo.
"""
import os
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ──────────────────────────────────────────────
# Kraken Futures API
# ──────────────────────────────────────────────
TRADING_MODE: str = os.getenv("TRADING_MODE", "demo")  # "demo" | "live"

KRAKEN_API_KEY: str = os.getenv("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET: str = os.getenv("KRAKEN_API_SECRET", "")

KRAKEN_BASE_URL: str = (
    "https://demo-futures.kraken.com"
    if TRADING_MODE == "demo"
    else "https://futures.kraken.com"
)
KRAKEN_REST_PATH: str = "/derivatives/api/v3"
KRAKEN_CHART_PATH: str = "/api/charts/v1"

# ──────────────────────────────────────────────
# Instrument
# ──────────────────────────────────────────────
SYMBOL: str = os.getenv("SYMBOL", "PF_ETHUSD")          # Linear perpetual
CHART_TICK_TYPE: str = "trade"                           # "trade" | "mark"
RESOLUTION: str = "1m"

# ──────────────────────────────────────────────
# Sessions (all times in UTC)
# ──────────────────────────────────────────────
@dataclass
class SessionConfig:
    name: str
    orb_start_h: int      # ORB window opens (hour)
    orb_start_m: int      # ORB window opens (minute)
    orb_end_h: int        # ORB window closes (30 min later)
    orb_end_m: int
    session_close_h: int  # session hard close
    session_close_m: int
    sl_pct: float         # stop loss %

SESSIONS: list[SessionConfig] = [
    SessionConfig(
        name="London",
        orb_start_h=9,  orb_start_m=0,
        orb_end_h=9,    orb_end_m=30,
        session_close_h=14, session_close_m=0,
        sl_pct=0.007,
    ),
    SessionConfig(
        name="NY",
        orb_start_h=14, orb_start_m=0,
        orb_end_h=14,   orb_end_m=30,
        session_close_h=23, session_close_m=0,
        sl_pct=0.007,
    ),
]

# Active weekdays: Tue=1, Wed=2, Thu=3 (Python: Mon=0…Sun=6)
ACTIVE_WEEKDAYS: list[int] = [1, 2, 3]

# Cutoff: no new entries within this many minutes of session close
NO_ENTRY_BEFORE_CLOSE_MINS: int = 60

# ──────────────────────────────────────────────
# Strategy parameters
# ──────────────────────────────────────────────
ORB_HIGH_BUFFER_PCT: float = 0.0001    # 0.01% buffer on ORB boundary
ORB_LOW_BUFFER_PCT: float  = 0.0001
INVALIDATION_PCT: float    = 0.003    # 0.3% — abandon signal if exceeded
ENTRY_BUFFER_PCT: float    = 0.0001   # 0.01% beyond ORB for entry
SLIPPAGE_PCT: float        = 0.0001   # 0.01% simulated slippage

# ──────────────────────────────────────────────
# Risk management
# ──────────────────────────────────────────────
RISK_PER_TRADE_PCT: float  = float(os.getenv("RISK_PER_TRADE_PCT", "0.01"))  # 1% of account
LEVERAGE: float            = float(os.getenv("LEVERAGE", "5.0"))              # 5x default live
MAX_DAILY_LOSS_PCT: float  = 0.03   # 3% — halt trading if exceeded today
MAX_WEEKLY_LOSS_PCT: float = 0.06   # 6%
COMMISSION_PCT: float      = 0.0002  # 0.02% per side (Kraken maker)

# ──────────────────────────────────────────────
# Operational
# ──────────────────────────────────────────────
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() == "true"
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
STATE_FILE: str = "state/state.json"
LOG_DIR: str = "logs"

# Retry / network
REQUEST_TIMEOUT_S: int = 10
MAX_RETRIES: int = 3
RETRY_DELAY_S: float = 2.0
