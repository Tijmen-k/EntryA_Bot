"""
Entry A — Liquidity Sweep Fade Strategy.

State machine per session:
  WAITING_FOR_ORB       → build the Opening Range from first 30 minutes
  WAITING_FOR_BREAKOUT  → wait for price to sweep outside ORB
  WAITING_FOR_CONFIRM   → wait for close back inside ORB (false breakout confirmed)
  PENDING_ENTRY         → resting limit order placed, waiting for it to fill or invalidate
  TRADE_OPEN            → position is live; monitor SL/TP
  DONE                  → session finished (trade closed, invalidated, or cancelled)

Bias (direction filter):
  London: bearish if price at London's ORB start < yesterday's open
  NY    : bearish if price at NY's ORB start < price at London's ORB start
  Both reference points follow each session's configured orb_start_h/m.
  Only trades aligned with session bias are taken.

Entry is a LIMIT order, not a market fill: once the confirming candle closes
back inside the ORB, a limit order is placed slightly beyond that close
(above it for a long/bullish setup, below it for a short/bearish setup),
rather than entering immediately at the close price. Whether it actually
fills is decided by the exchange, not this module — main.py polls the broker
and calls back in once a fill (or cancellation) happens.
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
    WAITING_FOR_ORB      = auto()
    WAITING_FOR_BREAKOUT = auto()
    WAITING_FOR_CONFIRM  = auto()
    PENDING_ENTRY        = auto()
    TRADE_OPEN           = auto()
    DONE                 = auto()


@dataclass
class ORBRange:
    high: float = 0.0
    low: float = float("inf")
    complete: bool = False


@dataclass
class TradeSignal:
    """
    Emitted when Entry A confirms a false breakout and a limit order should
    be placed. `entry_price` is the intended LIMIT price (slightly beyond the
    confirming candle's close), not a guaranteed fill.
    """
    session: str
    direction: str          # "long" | "short"
    entry_price: float      # limit order price
    sl_price: float
    tp_price: float
    orb_high: float
    orb_low: float
    breakout_level: float
    sweep_price: float = 0.0        # the actual wick extreme that swept beyond the ORB boundary
    sweep_time: Optional[str] = None  # ISO timestamp of the sweep bar


@dataclass
class OpenTrade:
    """Tracks the live trade state (persisted to state.json), after the limit entry fills."""
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
        self.sweep_time: Optional[datetime] = None
        self.pending_signal: Optional[TradeSignal] = None  # limit order resting, not yet filled
        self.signal: Optional[TradeSignal] = None          # signal for the trade that actually opened

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
        Returns a TradeSignal (a limit order to place) when entry conditions
        are met, else None. PENDING_ENTRY / TRADE_OPEN lifecycle (fill
        detection, SL/TP monitoring) is handled by main.py, not here.
        """
        if self.phase in (SessionPhase.DONE, SessionPhase.PENDING_ENTRY, SessionPhase.TRADE_OPEN):
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

    def _check_breakout(self, bar: Bar) -> Optional[TradeSignal]:
        """
        If bearish bias: look for price sweeping ABOVE ORB high (liquidity grab above).
        If bullish bias: look for price sweeping BELOW ORB low  (liquidity grab below).

        A wick through the boundary is enough to qualify as a sweep — the bar does NOT
        need to close outside.  After detecting the sweep we immediately run the
        confirmation check on the same bar, so a single candle that wicks outside and
        closes back inside fires the entry without waiting for the next bar.
        """
        if self.bias_is_bearish:
            # Bullish sweep above ORB high → fade SHORT
            if bar.high > self.orb.high:
                self.breakout_level = self.orb.high
                self.sweep_extreme  = bar.high
                self.sweep_time     = bar.timestamp
                self._goto(SessionPhase.WAITING_FOR_CONFIRM,
                           f"sweep above ORB H={self.orb.high:.4f} bar_high={bar.high:.4f}")
                return self._check_confirmation(bar)
        else:
            # Bearish sweep below ORB low → fade LONG
            if bar.low < self.orb.low:
                self.breakout_level = self.orb.low
                self.sweep_extreme  = bar.low
                self.sweep_time     = bar.timestamp
                self._goto(SessionPhase.WAITING_FOR_CONFIRM,
                           f"sweep below ORB L={self.orb.low:.4f} bar_low={bar.low:.4f}")
                return self._check_confirmation(bar)
        return None

    # ── confirmation detection ──────────────────────

    def _check_confirmation(self, bar: Bar) -> Optional[TradeSignal]:
        """
        Wait for price to close back inside the ORB boundary.
        Also check for invalidation (price extends 0.3% further opposite direction).
        On confirmation, build a LIMIT entry slightly beyond the confirming
        candle's close (not a market fill) and move to PENDING_ENTRY.
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

        direction = "short" if is_bearish else "long"

        # LIMIT entry, placed slightly beyond the confirming candle's close:
        # long (bullish bias)  -> limit ABOVE close
        # short (bearish bias) -> limit BELOW close
        buffer_pct = config.LIMIT_ENTRY_BUFFER_PCT
        if direction == "long":
            limit_price = bar.close * (1.0 + buffer_pct)
        else:
            limit_price = bar.close * (1.0 - buffer_pct)

        sl = self._calc_sl(limit_price, direction)
        tp = self._calc_tp(limit_price, direction)

        if tp is None:
            self._goto(SessionPhase.DONE, "could not calculate TP (prev-day range too small)")
            return None

        signal = TradeSignal(
            session=self.cfg.name,
            direction=direction,
            entry_price=limit_price,
            sl_price=sl,
            tp_price=tp,
            orb_high=self.orb.high,
            orb_low=self.orb.low,
            breakout_level=level,
            sweep_price=self.sweep_extreme or 0.0,
            sweep_time=self.sweep_time.isoformat() if self.sweep_time else None,
        )

        self.pending_signal = signal
        self._goto(SessionPhase.PENDING_ENTRY,
                   f"confirmed → LIMIT {direction} @ {limit_price:.4f} sl={sl:.4f} tp={tp:.4f}")
        return signal

    # ── pending-entry lifecycle (limit order resting, not yet filled) ──

    def check_pending_invalidation(self, bar: Bar) -> Optional[str]:
        """
        While a limit entry is resting, decide whether it should be cancelled:
        price extends 0.3% further away (same rule as post-confirmation
        invalidation), or the entry cutoff / session close has arrived.
        Returns a reason string if it should be cancelled, else None.
        Pure price/time logic only — main.py is responsible for actually
        cancelling the exchange order and detecting fills.
        """
        if self.pending_signal is None:
            return None
        if bar.timestamp >= self._entry_cutoff():
            return "entry cutoff reached before fill"
        if self.bias_is_bearish:
            inv_level = self.orb.low * (1.0 - config.INVALIDATION_PCT)
            if bar.low < inv_level:
                return f"price extended below ORB low to {bar.low:.4f} before fill"
        else:
            inv_level = self.orb.high * (1.0 + config.INVALIDATION_PCT)
            if bar.high > inv_level:
                return f"price extended above ORB high to {bar.high:.4f} before fill"
        return None

    def cancel_pending_entry(self, reason: str) -> None:
        logger.info(f"[{self.cfg.name}] Cancelling pending limit entry: {reason}")
        self.pending_signal = None
        self._goto(SessionPhase.DONE, f"pending entry cancelled: {reason}")

    # ── price calculations ──────────────────────────

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
      is_bearish = price_at_London_ORB_start < yesterday_open
    NY bias:
      is_bearish = price_at_NY_ORB_start < price_at_London_ORB_start

    Both reference points follow each session's configured orb_start_h/m.
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
        Superseded by compute_measured_move_pct() for TP, kept for any other callers.
        """
        if prev_high <= 0:
            return 0.0
        return (prev_high - prev_low) / prev_high

    @staticmethod
    def compute_measured_move_pct(bars: list[Bar], window_start: datetime, window_end: datetime) -> Optional[float]:
        """
        TP measured-move %, computed as: the highest high and lowest low of ALL
        price action from `window_start` (previous calendar day, 00:00 UTC)
        through `window_end` (this session's own ORB-open moment) — not just
        yesterday's single daily candle. Distance between those two extremes,
        as a % of the highest high.
        """
        in_window = [b for b in bars if window_start <= b.timestamp <= window_end]
        if not in_window:
            return None
        highest = max(b.high for b in in_window)
        lowest  = min(b.low  for b in in_window)
        if highest <= 0:
            return None
        return (highest - lowest) / highest