"""
Entry A — Liquidity Sweep Fade Strategy.

State machine per session:
  WAITING_FOR_ORB       → build the Opening Range from first 30 minutes
  WAITING_FOR_BREAKOUT  → wait for price to sweep outside ORB
  WAITING_FOR_CONFIRM   → wait for close back inside ORB (false breakout confirmed)
  TRADE_OPEN            → position is live; monitor SL/TP
  DONE                  → session finished (trade closed or invalidated)

Bias (direction filter):
  London: bearish if today's 09:00 close < yesterday's open
  NY    : bearish if today's 14:00 close < today's 09:00 close
  Only trades aligned with session bias are taken.
"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, time as dtime, timedelta
from enum import Enum, auto
from pathlib import Path
from typing import Optional

from loguru import logger

import config
from config import SessionConfig
from src.data.feed import Bar


# ──────────────────────────────────────────────────────
# Enums & small data classes
# ──────────────────────────────────────────────────────

class SessionPhase(Enum):
    WAITING_FOR_ORB     = auto()
    WAITING_FOR_BREAKOUT = auto()
    WAITING_FOR_CONFIRM  = auto()
    TRADE_OPEN           = auto()
    DONE                 = auto()


@dataclass
class ORBRange:
    high: float = 0.0
    low: float = float("inf")
    complete: bool = False


@dataclass
class TradeSignal:
    """Emitted when Entry A fires and a trade should be placed."""
    session: str
    direction: str          # "long" | "short"
    entry_price: float
    sl_price: float
    tp_price: float
    orb_high: float
    orb_low: float
    breakout_level: float


@dataclass
class OpenTrade:
    """Tracks the live trade state (persisted to state.json)."""
    signal: dict            # TradeSignal as dict for serialisation
    contracts: float
    entry_order_id: Optional[str] = None
    sl_order_id:    Optional[str] = None
    tp_order_id:    Optional[str] = None
    status: str = "open"    # "open" | "sl_hit" | "tp_hit" | "closed"


# ──────────────────────────────────────────────────────
# Per-session state machine
# ──────────────────────────────────────────────────────

class SessionStateMachine:
    """
    One instance per session per day (London, NY).
    Feed it every closed 1-minute bar via on_bar().
    """

    def __init__(self, session_cfg: SessionConfig, today: datetime) -> None:
        self.cfg = session_cfg
        self.today = today.date()
        self.phase = SessionPhase.WAITING_FOR_ORB
        self.orb = ORBRange()
        self.bias_is_bearish: Optional[bool] = None
        self.breakout_level: Optional[float] = None
        self.sweep_extreme: Optional[float] = None
        self.signal: Optional[TradeSignal] = None

    # ── time helpers ────────────────────────────────

    def _orb_start(self) -> datetime:
        return datetime.combine(
            self.today,
            dtime(self.cfg.orb_start_h, self.cfg.orb_start_m),
            tzinfo=timezone.utc,
        )

    def _orb_end(self) -> datetime:
        return datetime.combine(
            self.today,
            dtime(self.cfg.orb_end_h, self.cfg.orb_end_m),
            tzinfo=timezone.utc,
        )

    def _session_close(self) -> datetime:
        return datetime.combine(
            self.today,
            dtime(self.cfg.session_close_h, self.cfg.session_close_m),
            tzinfo=timezone.utc,
        )

    def _entry_cutoff(self) -> datetime:
        return self._session_close() - timedelta(minutes=config.NO_ENTRY_BEFORE_CLOSE_MINS)

    # ── main entry point ────────────────────────────

    def on_bar(self, bar: Bar) -> Optional[TradeSignal]:
        """
        Process a closed 1-minute bar.
        Returns a TradeSignal when entry conditions are met, else None.
        """
        if self.phase == SessionPhase.DONE:
            return None

        now = bar.timestamp
        orb_start = self._orb_start()
        orb_end   = self._orb_end()
        cutoff    = self._entry_cutoff()
        close_t   = self._session_close()

        # Hard session close
        if now >= close_t:
            self._goto(SessionPhase.DONE, "session closed")
            return None

        # ── Phase: Build ORB ────────────────────────
        if now >= orb_start and now < orb_end:
            self.phase = SessionPhase.WAITING_FOR_ORB
            self.orb.high = max(self.orb.high, bar.high)
            self.orb.low  = min(self.orb.low,  bar.low)
            return None

        # ORB just completed
        if now >= orb_end and not self.orb.complete:
            if self.orb.high == 0 or self.orb.low == float("inf"):
                logger.warning(f"[{self.cfg.name}] ORB window passed but no bars received")
                self._goto(SessionPhase.DONE, "no ORB data")
                return None
            self.orb.complete = True
            logger.info(
                f"[{self.cfg.name}] ORB set → H={self.orb.high:.4f}  L={self.orb.low:.4f}"
            )
            self._goto(SessionPhase.WAITING_FOR_BREAKOUT, "ORB complete")

        if not self.orb.complete:
            return None

        # No new entries too close to session close
        if now >= cutoff and self.phase in (
            SessionPhase.WAITING_FOR_BREAKOUT, SessionPhase.WAITING_FOR_CONFIRM
        ):
            self._goto(SessionPhase.DONE, "entry cutoff reached")
            return None

        # Bias must be set
        if self.bias_is_bearish is None:
            return None

        # ── Phase: Wait for sweep ───────────────────
        if self.phase == SessionPhase.WAITING_FOR_BREAKOUT:
            return self._check_breakout(bar)

        # ── Phase: Wait for close-back ──────────────
        if self.phase == SessionPhase.WAITING_FOR_CONFIRM:
            return self._check_confirmation(bar)

        return None

    # ── bias setter (called externally before bars) ─

    def set_bias(self, is_bearish: bool) -> None:
        self.bias_is_bearish = is_bearish
        logger.info(
            f"[{self.cfg.name}] Bias set: {'BEARISH (short only)' if is_bearish else 'BULLISH (long only)'}"
        )

    # ── breakout detection ──────────────────────────

    def _check_breakout(self, bar: Bar) -> None:
        """
        If bearish bias: look for price sweeping ABOVE ORB high (liquidity grab above).
        If bullish bias: look for price sweeping BELOW ORB low  (liquidity grab below).
        """
        if self.bias_is_bearish:
            # Bullish sweep above ORB high → fade SHORT
            if bar.high > self.orb.high:
                self.breakout_level = self.orb.high
                self.sweep_extreme  = bar.high
                self._goto(SessionPhase.WAITING_FOR_CONFIRM,
                           f"sweep above ORB H={self.orb.high:.4f} bar_high={bar.high:.4f}")
        else:
            # Bearish sweep below ORB low → fade LONG
            if bar.low < self.orb.low:
                self.breakout_level = self.orb.low
                self.sweep_extreme  = bar.low
                self._goto(SessionPhase.WAITING_FOR_CONFIRM,
                           f"sweep below ORB L={self.orb.low:.4f} bar_low={bar.low:.4f}")
        return None

    # ── confirmation detection ──────────────────────

    def _check_confirmation(self, bar: Bar) -> Optional[TradeSignal]:
        """
        Wait for price to close back inside the ORB boundary.
        Also check for invalidation (price extends 0.3% further opposite direction).
        """
        is_bearish = self.bias_is_bearish
        level = self.breakout_level

        # Invalidation check first
        if is_bearish:
            inv_level = self.orb.low * (1.0 - config.INVALIDATION_PCT)
            if bar.low < inv_level:
                self._goto(SessionPhase.DONE,
                           f"invalidated: price extended below ORB low to {bar.low:.4f}")
                return None
        else:
            inv_level = self.orb.high * (1.0 + config.INVALIDATION_PCT)
            if bar.high > inv_level:
                self._goto(SessionPhase.DONE,
                           f"invalidated: price extended above ORB high to {bar.high:.4f}")
                return None

        # Confirmation: close back inside ORB
        confirmed = (
            (is_bearish and bar.close < level) or
            (not is_bearish and bar.close > level)
        )
        if not confirmed:
            return None

        # Build the signal
        direction  = "short" if is_bearish else "long"
        entry      = self._calc_entry(level, direction)
        sl         = self._calc_sl(entry, direction)
        tp         = self._calc_tp(entry, direction)

        if tp is None:
            self._goto(SessionPhase.DONE, "could not calculate TP (prev-day range too small)")
            return None

        signal = TradeSignal(
            session=self.cfg.name,
            direction=direction,
            entry_price=entry,
            sl_price=sl,
            tp_price=tp,
            orb_high=self.orb.high,
            orb_low=self.orb.low,
            breakout_level=level,
        )

        self._goto(SessionPhase.TRADE_OPEN, f"SIGNAL confirmed → {direction} entry={entry:.4f} sl={sl:.4f} tp={tp:.4f}")
        self.signal = signal
        return signal

    # ── price calculations ──────────────────────────

    def _calc_entry(self, level: float, direction: str) -> float:
        buf = config.ENTRY_BUFFER_PCT
        slip = config.SLIPPAGE_PCT
        if direction == "short":
            return level * (1.0 - buf) - level * slip
        else:
            return level * (1.0 + buf) + level * slip

    def _calc_sl(self, entry: float, direction: str) -> float:
        sl_pct = self.cfg.sl_pct
        if direction == "short":
            return entry * (1.0 + sl_pct)
        else:
            return entry * (1.0 - sl_pct)

    def _calc_tp(self, entry: float, direction: str) -> Optional[float]:
        """
        Measured move: use the previous day's range projected from the ORB boundary.
        prev_day_range_pct is stored externally and must be set before bars arrive.
        """
        pdr = getattr(self, "prev_day_range_pct", None)
        if pdr is None or pdr <= 0:
            return None
        if direction == "short":
            tp = self.orb.low * (1.0 - pdr)
            return tp if tp < entry else None
        else:
            tp = self.orb.high * (1.0 + pdr)
            return tp if tp > entry else None

    def set_prev_day_range_pct(self, pdr_pct: float) -> None:
        self.prev_day_range_pct = pdr_pct

    # ── SL/TP monitoring (called when trade is open) ─

    def check_sl_tp(self, bar: Bar, trade_signal: TradeSignal) -> Optional[str]:
        """
        Returns "sl" | "tp" | None.
        Called every bar while a position is open.
        """
        d = trade_signal.direction
        if d == "short":
            if bar.high >= trade_signal.sl_price:
                return "sl"
            if bar.low <= trade_signal.tp_price:
                return "tp"
        else:
            if bar.low <= trade_signal.sl_price:
                return "sl"
            if bar.high >= trade_signal.tp_price:
                return "tp"
        return None

    # ── helpers ─────────────────────────────────────

    def _goto(self, phase: SessionPhase, reason: str) -> None:
        logger.debug(f"[{self.cfg.name}] {self.phase.name} → {phase.name} | {reason}")
        self.phase = phase


# ──────────────────────────────────────────────────────
# Bias calculator
# ──────────────────────────────────────────────────────

class BiasCalculator:
    """
    Determines intraday directional bias from daily bars or intraday reference prices.

    London bias:
      is_bearish = price_at_09:00_UTC < yesterday_open
    NY bias:
      is_bearish = price_at_14:00_UTC < price_at_09:00_UTC

    Overall: is_bearish = (today_close < yesterday_open)
    """

    @staticmethod
    def compute_london_bias(
        yesterday_open: float,
        today_at_09: float,
    ) -> bool:
        """Returns True if bearish (short-only for London session)."""
        is_bearish = today_at_09 < yesterday_open
        pct = (today_at_09 - yesterday_open) / yesterday_open * 100
        logger.info(
            f"[Bias/London] yesterday_open={yesterday_open:.4f} "
            f"today_09={today_at_09:.4f} "
            f"move={pct:+.2f}% → {'BEARISH' if is_bearish else 'BULLISH'}"
        )
        return is_bearish

    @staticmethod
    def compute_ny_bias(
        price_at_09: float,
        price_at_14: float,
    ) -> bool:
        """Returns True if bearish (short-only for NY session)."""
        is_bearish = price_at_14 < price_at_09
        pct = (price_at_14 - price_at_09) / price_at_09 * 100
        logger.info(
            f"[Bias/NY] price_09={price_at_09:.4f} "
            f"price_14={price_at_14:.4f} "
            f"move={pct:+.2f}% → {'BEARISH' if is_bearish else 'BULLISH'}"
        )
        return is_bearish

    @staticmethod
    def compute_prev_day_range_pct(prev_high: float, prev_low: float) -> float:
        """
        Measured-move multiplier = previous day's high-low range as % of high.
        Used for TP calculation.
        """
        if prev_high <= 0:
            return 0.0
        return (prev_high - prev_low) / prev_high
