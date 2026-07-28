"""
/risk and /config — read-only config display plus whitelisted .env edits.

Both commands use an explicit ALLOW-list (never a deny-list) of exposable or
editable fields, so a future new secret in config.py can't accidentally leak
through, and so a typo in a field name can't accidentally let someone edit
something unintended.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

from dotenv import set_key

import config as trading_config
from src.risk import sizing


# ── /config — read-only ────────────────────────────────────────────────────────

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


# ── /risk — view + whitelisted edit ────────────────────────────────────────────

class RiskField(NamedTuple):
    env_key: str
    parser: Callable[[str], float]
    min_value: float
    max_value: float
    formatter: Callable[[float], str]


def _pct_parser(raw: str) -> float:
    """Accepts either "1.5" (meaning 1.5%) or "0.015" — normalises to a fraction."""
    value = float(raw)
    return value / 100 if value > 1 else value


_RISK_FIELDS: dict[str, RiskField] = {
    "risk_per_trade_pct": RiskField("RISK_PER_TRADE_PCT", _pct_parser, 0.001, 0.10, lambda v: f"{v*100:.2f}%"),
    "leverage":           RiskField("LEVERAGE", float, 1, 125, lambda v: f"{v:.0f}x"),
    "ladder_scale_factor": RiskField("LADDER_SCALE_FACTOR", float, 0.001, 10.0, lambda v: f"{v:.4f}"),
    "london_sl_pct":      RiskField("LONDON_SL_PCT", _pct_parser, 0.001, 0.05, lambda v: f"{v*100:.2f}%"),
    "ny_sl_pct":          RiskField("NY_SL_PCT", _pct_parser, 0.001, 0.05, lambda v: f"{v*100:.2f}%"),
}

RISK_FIELD_NAMES: tuple[str, ...] = tuple(_RISK_FIELDS.keys())


@dataclass
class RiskSnapshot:
    risk_per_trade_pct: float
    leverage: float
    ladder_scale_factor: float
    london_sl_pct: float
    ny_sl_pct: float
    ladder_level: int
    ladder_default_usdt: float
    ladder_boosted_usdt: float


async def get_risk_snapshot(equity: float) -> RiskSnapshot:
    rung = await asyncio.to_thread(sizing.get_current_rung, equity)
    return RiskSnapshot(
        risk_per_trade_pct=trading_config.RISK_PER_TRADE_PCT,
        leverage=trading_config.LEVERAGE,
        ladder_scale_factor=trading_config.LADDER_SCALE_FACTOR,
        london_sl_pct=trading_config.LONDON_SL_PCT,
        ny_sl_pct=trading_config.NY_SL_PCT,
        ladder_level=rung.level,
        ladder_default_usdt=rung.default_usdt,
        ladder_boosted_usdt=rung.boosted_usdt,
    )


class RiskEditError(ValueError):
    pass


def validate_risk_edit(field_name: str, raw_value: str) -> tuple[str, float, str]:
    """Returns (env_key, parsed_value, formatted_value) or raises RiskEditError."""
    field = _RISK_FIELDS.get(field_name)
    if not field:
        raise RiskEditError(f"Unknown field '{field_name}'. Valid fields: {', '.join(RISK_FIELD_NAMES)}")
    try:
        parsed = field.parser(raw_value)
    except ValueError:
        raise RiskEditError(f"'{raw_value}' is not a valid number for {field_name}.")
    if not (field.min_value <= parsed <= field.max_value):
        raise RiskEditError(f"{field_name} must be between {field.min_value} and {field.max_value}.")
    return field.env_key, parsed, field.formatter(parsed)


def _write_env_sync(env_key: str, parsed: float) -> None:
    set_key(".env", env_key, str(parsed))


async def write_risk_field(field_name: str, raw_value: str) -> tuple[str, str]:
    """Validates, writes to .env, and returns (env_key, formatted_value). Raises RiskEditError on invalid input."""
    env_key, parsed, formatted = validate_risk_edit(field_name, raw_value)
    await asyncio.to_thread(_write_env_sync, env_key, parsed)
    return env_key, formatted
