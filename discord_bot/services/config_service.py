"""
/config — read-only config display.

Uses an explicit ALLOW-list (never a deny-list) of exposable fields, so a
future new secret in config.py can't accidentally leak through.
"""
from __future__ import annotations

import config as trading_config

_CONFIG_ALLOW_LIST: tuple[str, ...] = (
    "SYMBOL", "PRODUCT_TYPE", "MARGIN_COIN", "MARGIN_MODE", "TRADING_MODE",
    "LEVERAGE", "DRY_RUN", "LADDER_SCALE_FACTOR", "RISK_PER_TRADE_PCT",
    "ACTIVE_WEEKDAYS", "NO_ENTRY_BEFORE_CLOSE_MINS",
    "MAX_DAILY_LOSS_PCT", "MAX_WEEKLY_LOSS_PCT",
)


def get_public_config() -> dict[str, str]:
    result = {}
    for key in _CONFIG_ALLOW_LIST:
        value = getattr(trading_config, key, None)
        result[key] = str(value)
    for session in trading_config.SESSIONS:
        result[f"{session.name}_ORB"] = (
            f"{session.orb_start_h:02d}:{session.orb_start_m:02d}"
            f"-{session.orb_end_h:02d}:{session.orb_end_m:02d} UTC  (SL {session.sl_pct*100:.2f}%)"
        )
    return result
