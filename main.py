"""
Entry A — Kraken Futures Trading Bot
======================================
Runs the Entry A liquidity-sweep fade strategy on PF_ETHUSD (or configured symbol).

Usage:
  python main.py            # live/demo mode per .env
  python main.py --dry-run  # simulate — reads real data but places no orders

The main loop wakes every minute (5 seconds after bar close) to process the latest bar.
State is persisted to state/state.json so the bot recovers after a restart.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from typing import Optional

from loguru import logger

import config
from src.utils.logger import setup_logger
from src.data.feed import BitgetFeed, Bar
from src.broker.bitget import BitgetBroker, Position
from src.strategy.entry_a import (
    SessionStateMachine, BiasCalculator, TradeSignal, OpenTrade, SessionPhase
)
from src.risk.sizing import PositionSizer, get_current_rung, get_ladder_progress, get_active_position_usdt, SCALING_LADDER
from src.journal import db as journal_db
from src.notifications.discord import DiscordNotifier


def drop_unclosed_bar(bars: list[Bar], now: datetime) -> list[Bar]:
    """
    Bitget's candle endpoint returns the still-forming current candle as the
    last entry — its H/L only reflect a few seconds of price action, not a
    real closed range. Strip it so callers only ever see fully closed bars.
    """
    if bars and bars[-1].timestamp + timedelta(minutes=1) > now:
        return bars[:-1]
    return bars


def resolve_bias(computed: Optional[bool], override: Optional[bool]) -> Optional[bool]:
    """A manual override (set via the dashboard, for testing) always wins over the auto-computed bias."""
    return override if override is not None else computed


# ──────────────────────────────────────────────────────
# State persistence
# ──────────────────────────────────────────────────────

class BotState:
    """
    Lightweight JSON state persisted between restarts.
    Tracks open trades per session and daily P&L for risk limits.
    """

    _defaults = {
        "date": "",           # YYYY-MM-DD of the current trading day
        "open_trade": None,   # serialised OpenTrade or None
        "pending_entry": None,  # resting limit order waiting to fill, or None
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "london_bias_bearish": None,
        "ny_bias_bearish": None,
        "london_bias_override": None,  # manual test override, set from the dashboard
        "ny_bias_override":     None,
        "price_at_09": None,
        "price_at_14": None,
        "yesterday_open": None,
        "prev_day_high": None,
        "prev_day_low": None,
        "london_range_pct": None,   # measured-move % for TP: prev-day-start → London open
        "ny_range_pct":     None,   # measured-move % for TP: prev-day-start → NY open
        # ORB / session-phase tracking (for dashboard)
        "london_phase":           "WAITING_FOR_ORB",
        "london_orb_high":        None,
        "london_orb_low":         None,
        "london_breakout_level":  None,
        "ny_phase":               "WAITING_FOR_ORB",
        "ny_orb_high":            None,
        "ny_orb_low":             None,
        "ny_breakout_level":      None,
        "active_session":         None,
        # Remote control (set externally by the Discord bot's /pause /resume
        # /killswitch — see _sync_halt_flag()). Durable: survives restarts.
        "trading_halted":        False,
        "trading_halted_reason": None,   # "manual_pause" | "killswitch"
        "trading_halted_by":     None,
        "trading_halted_at":     None,
    }

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(exist_ok=True)
        self._data: dict = {}
        self._load()

    def _load(self) -> None:
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text())
                return
            except Exception as exc:
                logger.warning(f"State file corrupted ({exc}), resetting")
        self._data = dict(self._defaults)
        self._save()

    def _save(self) -> None:
        self._path.write_text(json.dumps(self._data, indent=2))

    def __getattr__(self, key: str):
        if key.startswith("_"):
            raise AttributeError(key)
        return self._data.get(key, self._defaults.get(key))

    def set(self, **kwargs) -> None:
        self._data.update(kwargs)
        self._save()

    def reset_for_day(self, date_str: str) -> None:
        self._data = {**self._defaults, "date": date_str}
        self._save()


# ──────────────────────────────────────────────────────
# Bot
# ──────────────────────────────────────────────────────

class EntryABot:

    def __init__(self, dry_run: bool = False) -> None:
        if dry_run:
            import os; os.environ["DRY_RUN"] = "true"
            config.DRY_RUN = True

        self.feed       = BitgetFeed()
        self.broker     = BitgetBroker()
        self.sizer      = PositionSizer()
        self.bias       = BiasCalculator()
        self.state      = BotState(config.STATE_FILE)
        self.notifier   = DiscordNotifier(config.DISCORD_WEBHOOK_URL)

        self._london_sm: Optional[SessionStateMachine] = None
        self._ny_sm:     Optional[SessionStateMachine] = None
        self._active_sm: Optional[SessionStateMachine] = None
        self._open_trade: Optional[OpenTrade] = None
        self._pending_entry: Optional[dict] = None   # resting limit order, not yet filled
        self._last_heartbeat: Optional[datetime] = None
        self._last_rung_level: Optional[int] = None   # tracks ladder level for boost detection
        self._applied_london_bias: Optional[bool] = None  # last bias actually pushed to the session SM
        self._applied_ny_bias: Optional[bool] = None

        journal_db.init_db()

        logger.info(f"Bot initialised | mode={config.TRADING_MODE} | symbol={config.SYMBOL} "
                    f"| dry_run={config.DRY_RUN}")

    # ──────────────────────────────────────────
    # Public run
    # ──────────────────────────────────────────

    def run(self) -> None:
        logger.info("Starting main loop…")
        while True:
            try:
                self._tick()
            except KeyboardInterrupt:
                logger.info("Keyboard interrupt — shutting down")
                self.notifier.bot_stopped("Keyboard interrupt")
                sys.exit(0)
            except Exception as exc:
                logger.exception(f"Unhandled exception in tick: {exc}")
                self.notifier.error(exc, context="main tick loop")
            self._sleep_until_next_bar()

    # ──────────────────────────────────────────
    # Core tick
    # ──────────────────────────────────────────

    def _tick(self) -> None:
        now = datetime.now(tz=timezone.utc)
        today_str = now.strftime("%Y-%m-%d")

        # ── Day rollover ────────────────────────
        if self.state.date != today_str:
            self._day_rollover(now, today_str)

        # ── Active weekday guard ────────────────
        if now.weekday() not in config.ACTIVE_WEEKDAYS:
            logger.debug(f"Inactive weekday ({now.strftime('%A')}) — skipping")
            return

        # ── Fetch latest bars ───────────────────
        bars = self.feed.fetch_ohlcv(config.SYMBOL, config.RESOLUTION, count=350)
        if not bars:
            logger.warning("No bars received — skipping tick")
            return

        bars = drop_unclosed_bar(bars, now)
        if not bars:
            logger.warning("No closed bars available yet — skipping tick")
            return

        # Pick up any manual bias override set from the dashboard since our last tick
        self._sync_bias_overrides()

        # Pick up /pause, /resume, /killswitch from the Discord bot since our last tick
        self._sync_halt_flag()

        last_bar = bars[-1]
        logger.debug(f"Latest bar: {last_bar.timestamp.isoformat()} "
                     f"O={last_bar.open} H={last_bar.high} L={last_bar.low} C={last_bar.close}")

        # ── Capture key reference prices ───────
        # Must run before _resume_in_progress_day(): if a session's ORB window
        # already closed while the bot was offline and its bias hadn't been
        # captured yet, this is what captures it — the replay below needs that
        # bias already known on this same tick, or it misses the one shot it gets.
        self._update_reference_prices(bars, now)

        # Resuming an already-active trading day after a restart — _day_rollover()
        # won't fire again today, so without this the session state machines stay
        # None forever and the whole engine goes silently inert for the rest of the day.
        if self._london_sm is None or self._ny_sm is None:
            self._resume_in_progress_day(now, bars)

        # ── Scaling ladder — boost detection ───
        self._check_ladder_boost()

        # ── Heartbeat ───────────────────────────
        self._maybe_send_heartbeat(now, last_bar.close)

        # ── Monitor pending limit entry if any ──
        if self._pending_entry:
            self._monitor_pending_entry(last_bar)
            return  # one order/trade at a time

        # ── Monitor open trade if any ───────────
        if self._open_trade and self._open_trade.status == "open":
            self._monitor_open_trade(last_bar)
            return  # one active trade at a time

        # ── Run session state machines (skipped while halted/paused) ──
        if self.state.trading_halted:
            logger.debug(
                f"Trading halted ({self.state.trading_halted_reason}) — skipping new-signal generation"
            )
            return
        self._run_sessions(last_bar, now)

    # ──────────────────────────────────────────
    # Day rollover
    # ──────────────────────────────────────────

    def _day_rollover(self, now: datetime, today_str: str) -> None:
        logger.info(f"New trading day: {today_str}")

        # Carry weekly P&L, reset daily
        prev_weekly = self.state.weekly_pnl_pct
        self.state.reset_for_day(today_str)
        self.state.set(weekly_pnl_pct=prev_weekly)

        # Reset session state machines
        london_cfg = config.SESSIONS[0]
        ny_cfg     = config.SESSIONS[1]
        self._london_sm = SessionStateMachine(london_cfg, now)
        self._ny_sm     = SessionStateMachine(ny_cfg, now)
        self._active_sm = None
        self._open_trade = None
        self._applied_london_bias = None
        self._applied_ny_bias = None

        logger.info("Session state machines reset for new day")

    # ──────────────────────────────────────────
    # Resuming mid-day after a restart
    # ──────────────────────────────────────────

    def _resume_in_progress_day(self, now: datetime, bars: list[Bar]) -> None:
        """
        Rebuild the session state machines when the process (re)started on a day
        that's already in progress — _day_rollover() only fires on an actual date
        change, so without this a same-day restart would leave both SMs as None
        forever, silently disabling the entire trading engine for the rest of the day.

        Also restores an in-flight trade from state.json so a restart while a
        position is open can't lead to a duplicate entry, and replays today's ORB/
        breakout history through each session so a restart doesn't just start a
        blank ORB partway through (or after) the ORB window. Any signal that would
        have fired during the offline gap is deliberately NOT acted on — the price
        has already moved on, so a live order now would be entering at a stale
        setup — the session is marked done for today instead, with an alert.
        """
        logger.warning(
            "Session state machines missing at tick time (process (re)started mid-day) "
            "— rebuilding from persisted state and replaying today's bars"
        )
        london_cfg, ny_cfg = config.SESSIONS[0], config.SESSIONS[1]
        self._london_sm = SessionStateMachine(london_cfg, now)
        self._ny_sm     = SessionStateMachine(ny_cfg, now)
        self._applied_london_bias = None
        self._applied_ny_bias = None

        # Restore an in-flight trade rather than risk re-entering a duplicate for
        # the session that already has a live position on the exchange.
        owning_session: Optional[str] = None
        if self.state.open_trade and self._open_trade is None:
            self._open_trade = OpenTrade(**self.state.open_trade)
            owning_session = self._open_trade.signal.get("session")
            logger.warning(
                f"Restored in-flight trade from state.json | session={owning_session} "
                f"dir={self._open_trade.signal.get('direction')} status={self._open_trade.status}"
            )
            owner_sm = self._london_sm if owning_session == "London" else self._ny_sm
            owner_sm.phase = SessionPhase.TRADE_OPEN
            owner_sm.signal = TradeSignal(**self._open_trade.signal)
        elif self.state.pending_entry and self._pending_entry is None:
            self._pending_entry = self.state.pending_entry
            owning_session = self._pending_entry["signal"].get("session")
            logger.warning(
                f"Restored pending limit entry from state.json | session={owning_session} "
                f"dir={self._pending_entry['signal'].get('direction')} "
                f"order_id={self._pending_entry.get('entry_order_id')}"
            )
            pe_sm = self._london_sm if owning_session == "London" else self._ny_sm
            pe_sm.pending_signal = TradeSignal(**self._pending_entry["signal"])
            pe_sm.phase = SessionPhase.PENDING_ENTRY

        today = now.date()
        for key, sm, cfg in (("london", self._london_sm, london_cfg), ("ny", self._ny_sm, ny_cfg)):
            if cfg.name == owning_session:
                continue  # already resolved above — don't replay over a live trade

            # Always replay this session's bars so the ORB gets built correctly
            # even if bias isn't known yet — on_bar() safely no-ops past
            # ORB-completion while bias_is_bearish is still None, so this is
            # safe to run unconditionally. Skipping the replay when bias was
            # None (previous behaviour) left `sm.orb` at its empty default
            # (high=0, low=inf) whenever the bot restarted before the
            # ORB-start bias bar was captured — the next live tick then built
            # a truncated ORB from only bars arriving after that point instead
            # of the true 30-minute range. Since breakout_level is always
            # exactly orb.low or orb.high, a wrong ORB silently produces a
            # breakout line sitting inside the real ORB range.
            bias = resolve_bias(getattr(self.state, f"{key}_bias_bearish"), getattr(self.state, f"{key}_bias_override"))
            if bias is not None:
                sm.set_bias(bias)
                range_pct = getattr(self.state, f"{key}_range_pct")
                if range_pct is not None:
                    sm.set_prev_day_range_pct(range_pct)
                setattr(self, f"_applied_{key}_bias", bias)

            orb_start = datetime.combine(today, dtime(cfg.orb_start_h, cfg.orb_start_m), tzinfo=timezone.utc)
            replay_bars = [b for b in bars if orb_start <= b.timestamp < now]
            for b in replay_bars:
                signal = sm.on_bar(b)
                if signal:
                    logger.warning(
                        f"[{cfg.name}] Signal would have fired at {b.timestamp.isoformat()} while the bot "
                        f"was offline (entry={signal.entry_price:.2f}) — NOT acted on, price has since "
                        f"moved on. Session done for today."
                    )
                    self.notifier.signal_missed(cfg.name, signal.direction, signal.entry_price, b.timestamp)
                    # on_bar() naturally moves the SM to TRADE_OPEN when it returns a signal — force it
                    # to DONE instead since no real position was opened for it, so the dashboard doesn't
                    # show a phantom "Trade Open" for a session with no actual trade.
                    sm.phase = SessionPhase.DONE
                    break

    # ──────────────────────────────────────────
    # Manual bias override (dashboard testing aid)
    # ──────────────────────────────────────────

    def _sync_bias_overrides(self) -> None:
        """
        Re-read just the override fields from state.json. The dashboard runs in a
        separate process and writes overrides directly to the file, so our
        in-memory BotState (loaded once at startup) won't otherwise see them.
        """
        try:
            raw = json.loads(Path(config.STATE_FILE).read_text())
        except Exception:
            return
        lon_ovr = raw.get("london_bias_override")
        ny_ovr  = raw.get("ny_bias_override")
        if lon_ovr != self.state.london_bias_override or ny_ovr != self.state.ny_bias_override:
            logger.info(f"Bias override changed | London={lon_ovr} NY={ny_ovr}")
            self.state.set(london_bias_override=lon_ovr, ny_bias_override=ny_ovr)

    def _sync_halt_flag(self) -> None:
        """
        Re-read the trading_halted flag from state.json. The Discord bot's
        /pause, /resume, and /killswitch commands run in a separate process
        and write this flag directly to the file, so our in-memory BotState
        won't otherwise see it. Runs first, before anything else touches
        state.json this tick, so a flag set by the Discord bot between our
        last tick and now can't be silently clobbered by our own next _save().
        """
        try:
            raw = json.loads(Path(config.STATE_FILE).read_text())
        except Exception:
            return
        halted = bool(raw.get("trading_halted", False))
        if halted != self.state.trading_halted:
            logger.warning(
                f"Trading halted flag changed: {halted} "
                f"(reason={raw.get('trading_halted_reason')}, by={raw.get('trading_halted_by')})"
            )
        self.state.set(
            trading_halted=halted,
            trading_halted_reason=raw.get("trading_halted_reason"),
            trading_halted_by=raw.get("trading_halted_by"),
            trading_halted_at=raw.get("trading_halted_at"),
        )

    # ──────────────────────────────────────────
    # Reference price tracking
    # ──────────────────────────────────────────

    def _update_reference_prices(self, bars: list[Bar], now: datetime) -> None:
        """
        Capture prices at key timestamps for bias calculation.
        Fires once per day per checkpoint.
        """
        today = now.date()

        # Yesterday's open/high/low: pulled from a daily candle, not the 1m rolling
        # window. "Yesterday 00:00 UTC" is always ~24h in the past, which a ~350-minute
        # (5.8h) 1m lookback can never reach — the old scan-the-1m-bars approach left
        # yesterday_open permanently null (and prev_day_high/low only ever a partial,
        # wrong range), which silently blocked London bias — and therefore the entire
        # London session state machine — from ever running.
        if self.state.yesterday_open is None or self.state.prev_day_high is None:
            yesterday = today - timedelta(days=1)
            daily = self.feed.fetch_ohlcv(config.SYMBOL, "1d", count=3)
            y_bar = next((b for b in daily if b.timestamp.date() == yesterday), None)
            if y_bar is None and daily:
                y_bar = next((b for b in daily if b.timestamp.date() < today), None)
            if y_bar:
                self.state.set(
                    yesterday_open=y_bar.open,
                    prev_day_high=y_bar.high,
                    prev_day_low=y_bar.low,
                )
                logger.info(
                    f"Yesterday daily candle captured: O={y_bar.open:.4f} "
                    f"H={y_bar.high:.4f} L={y_bar.low:.4f}"
                )

        # Price at London's ORB start (bias endpoint 1) — follows orb_start_h/m
        # dynamically, same as NY below, instead of a hardcoded 09:00. It used to
        # be hardcoded, which silently drifted out of sync with LONDON_ORB_START_H
        # (7 in .env) — bias was being measured 1.5h after the ORB actually built.
        _lon = config.SESSIONS[0]
        if self.state.price_at_09 is None:
            ref_09 = self._find_bar_at(bars, today, _lon.orb_start_h, _lon.orb_start_m)
            if ref_09:
                self.state.set(price_at_09=ref_09.close)
                logger.info(
                    f"Price at London ORB start ({_lon.orb_start_h:02d}:{_lon.orb_start_m:02d} UTC) "
                    f"captured: {ref_09.close:.4f}"
                )
                # Calculate London bias now
                if self.state.yesterday_open:
                    is_bearish = self.bias.compute_london_bias(
                        self.state.yesterday_open, ref_09.close
                    )
                    self.state.set(london_bias_bearish=is_bearish)
                    self.notifier.bias_set("London", is_bearish, ref_09.close, self.state.yesterday_open)
                self._capture_range_pct("london", _lon, today)

        # Price at NY session open (bias endpoint 2)
        _ny = config.SESSIONS[1]
        if self.state.price_at_14 is None:
            ref_14 = self._find_bar_at(bars, today, _ny.orb_start_h, _ny.orb_start_m)
            if ref_14:
                self.state.set(price_at_14=ref_14.close)
                logger.info(
                    f"Price at NY ORB start ({_ny.orb_start_h:02d}:{_ny.orb_start_m:02d} UTC) "
                    f"captured: {ref_14.close:.4f}"
                )
                # Calculate NY bias
                if self.state.price_at_09:
                    is_bearish = self.bias.compute_ny_bias(
                        self.state.price_at_09, ref_14.close
                    )
                    self.state.set(ny_bias_bearish=is_bearish)
                    self.notifier.bias_set("NY", is_bearish, ref_14.close, self.state.price_at_09)
                self._capture_range_pct("ny", _ny, today)

    @staticmethod
    def _find_bar_at(bars: list[Bar], date, hour: int, minute: int) -> Optional[Bar]:
        for b in bars:
            if b.timestamp.date() == date and b.timestamp.hour == hour and b.timestamp.minute == minute:
                return b
        return None

    def _capture_range_pct(self, key: str, cfg, today) -> None:
        """
        TP measured-move %: highest high / lowest low across ALL price action
        from the previous calendar day's 00:00 UTC through this session's own
        ORB-open moment (today) — not just yesterday's single daily candle.
        Fires once per session per day, same as the bias/reference captures above.
        """
        if getattr(self.state, f"{key}_range_pct") is not None:
            return
        window_end   = datetime.combine(today, dtime(cfg.orb_start_h, cfg.orb_start_m), tzinfo=timezone.utc)
        window_start = datetime.combine(today - timedelta(days=1), dtime.min, tzinfo=timezone.utc)
        try:
            lookback_bars = self.feed.fetch_ohlcv(config.SYMBOL, "1h", count=48)
        except Exception as exc:
            logger.warning(f"[{cfg.name}] Could not fetch lookback bars for measured-move range: {exc}")
            return
        pct = self.bias.compute_measured_move_pct(lookback_bars, window_start, window_end)
        if pct is None:
            logger.warning(f"[{cfg.name}] No bars in measured-move lookback window — TP will be unavailable until captured")
            return
        self.state.set(**{f"{key}_range_pct": pct})
        logger.info(
            f"[{cfg.name}] Measured-move range captured: "
            f"{window_start.date()} 00:00 UTC → {window_end.strftime('%H:%M UTC')} = {pct*100:.3f}%"
        )

    # ──────────────────────────────────────────
    # Session routing
    # ──────────────────────────────────────────

    def _run_sessions(self, bar: Bar, now: datetime) -> None:
        hour = now.hour
        lon  = config.SESSIONS[0]
        ny   = config.SESSIONS[1]

        lon_bias = resolve_bias(self.state.london_bias_bearish, self.state.london_bias_override)
        if lon.orb_start_h <= hour < lon.session_close_h:
            if self._london_sm and lon_bias is not None:
                self._apply_bias(self._london_sm, "london", lon_bias, self._applied_london_bias)
                self._applied_london_bias = lon_bias
                self._process_sm(self._london_sm, bar)

        ny_bias = resolve_bias(self.state.ny_bias_bearish, self.state.ny_bias_override)
        if ny.orb_start_h <= hour < ny.session_close_h:
            if self._ny_sm and ny_bias is not None:
                self._apply_bias(self._ny_sm, "ny", ny_bias, self._applied_ny_bias)
                self._applied_ny_bias = ny_bias
                self._process_sm(self._ny_sm, bar)

    def _apply_bias(self, sm: SessionStateMachine, key: str, bias: bool, previously_applied: Optional[bool]) -> None:
        """Push the effective bias to the session SM, and (re)apply the measured-move TP
        range if needed — required so a manual override still gets a usable TP even
        before the natural ORB-start capture."""
        if bias != previously_applied:
            sm.set_bias(bias)
        if getattr(sm, "prev_day_range_pct", None) is None:
            range_pct = getattr(self.state, f"{key}_range_pct")
            if range_pct is not None:
                sm.set_prev_day_range_pct(range_pct)

    def _process_sm(self, sm: SessionStateMachine, bar: Bar) -> None:
        signal = sm.on_bar(bar)
        # Persist ORB levels and phase to state.json for the dashboard
        key = sm.cfg.name.lower()
        update: dict = {
            f"{key}_phase":          sm.phase.name,
            "active_session":        sm.cfg.name,
        }
        if sm.orb.complete:
            update[f"{key}_orb_high"] = sm.orb.high
            update[f"{key}_orb_low"]  = sm.orb.low
        if sm.breakout_level is not None:
            update[f"{key}_breakout_level"] = sm.breakout_level
        self.state.set(**update)
        if signal:
            self._on_signal(signal, bar)

    # ──────────────────────────────────────────
    # Signal handling — open trade
    # ──────────────────────────────────────────

    def _on_signal(self, signal: TradeSignal, bar: Bar) -> None:
        logger.info(
            f"TRADE SIGNAL (pending limit) | {signal.session} | {signal.direction.upper()} | "
            f"limit={signal.entry_price:.4f} sl={signal.sl_price:.4f} tp={signal.tp_price:.4f}"
        )

        # Fetch current balance
        balance = self.broker.get_account_balance()

        if balance <= 0:
            logger.warning("Zero balance — skipping trade")
            return

        # Position size from scaling ladder + boost detection
        _recent   = journal_db.get_closed_trades()
        _pos_usdt, _boosted = get_active_position_usdt(balance, _recent)
        _rung     = get_current_rung(balance)
        _step     = config.CONTRACT_SIZE
        _raw      = _pos_usdt * config.LEVERAGE / signal.entry_price
        contracts = max(_step, round(_raw / _step) * _step)
        logger.info(
            f"Ladder level {_rung.level} | {'BOOSTED' if _boosted else 'default'} "
            f"→ {_pos_usdt} USDT × {config.LEVERAGE}× = {contracts:.4f} {config.SYMBOL[:3]}"
        )
        if contracts <= 0:
            logger.warning("Sizing returned 0 contracts — skipping trade")
            return

        entry_side = "sell" if signal.direction == "short" else "buy"

        # Resting LIMIT order at signal.entry_price, not a market fill. SL/TP
        # bracket orders are only placed once this actually fills (see
        # _activate_filled_trade) — presetting them against an order that may
        # never fill would leave orphaned trigger orders on Bitget.
        entry_id = self.broker.place_limit_order(
            side=entry_side, size=contracts, price=signal.entry_price,
            client_id=f"entry_{signal.session}",
        )
        if not entry_id:
            logger.error("Limit entry order failed — no order placed")
            return

        self._pending_entry = {
            "signal": signal.__dict__,
            "contracts": contracts,
            "entry_order_id": entry_id,
        }
        self.state.set(pending_entry=self._pending_entry)

        logger.info(
            f"LIMIT ENTRY PLACED | session={signal.session} dir={signal.direction} "
            f"price={signal.entry_price:.4f} contracts={contracts:.4f} order_id={entry_id}"
        )

    # ──────────────────────────────────────────
    # Monitor a resting (unfilled) limit entry
    # ──────────────────────────────────────────

    def _monitor_pending_entry(self, bar: Bar) -> None:
        """
        Each tick while a limit entry is resting: check if it filled (a
        matching position now exists on the exchange), and if not, check
        whether it should be cancelled (invalidated or entry cutoff reached).
        """
        pe        = self._pending_entry
        sig       = pe["signal"]
        direction = sig["direction"]
        session   = sig["session"]
        hold_side = "short" if direction == "short" else "long"
        sm        = self._london_sm if session == "London" else self._ny_sm

        try:
            positions = self.broker.get_open_positions()
        except Exception as exc:
            logger.warning(f"Could not fetch positions while monitoring pending entry: {exc}")
            return

        filled_pos = next(
            (p for p in positions if p.side == hold_side and p.symbol == config.SYMBOL), None
        )
        if filled_pos:
            logger.info(f"LIMIT ENTRY FILLED | {session} {direction} @ {filled_pos.entry_price:.4f}")
            self._activate_filled_trade(pe, filled_pos, sm)
            return

        reason = sm.check_pending_invalidation(bar) if sm else None
        if reason:
            logger.info(f"[{session}] Cancelling unfilled limit entry: {reason}")
            try:
                self.broker.cancel_order(pe["entry_order_id"])
            except Exception as exc:
                logger.warning(f"Failed to cancel pending entry order {pe['entry_order_id']}: {exc}")
            if sm:
                sm.cancel_pending_entry(reason)
            self._pending_entry = None
            self.state.set(pending_entry=None)

    def _activate_filled_trade(self, pe: dict, filled_pos, sm: Optional[SessionStateMachine]) -> None:
        """Limit entry has filled on the exchange — place SL/TP brackets and open the trade for real."""
        sig        = pe["signal"]
        direction  = sig["direction"]
        contracts  = pe["contracts"]
        entry_price = filled_pos.entry_price or sig["entry_price"]
        # Bitget's hedge-mode `side` for SL/TP plan orders must match the
        # POSITION's direction (buy=long, sell=short) — same value used for
        # the entry order — not the literal action needed to close it.
        # Sending the flipped action side (the old bug) makes Bitget label a
        # short's bracket orders as "Close Long".
        bracket_side = "sell" if direction == "short" else "buy"

        sl_id = self.broker.place_stop_market_order(
            side=bracket_side, size=contracts,
            stop_price=sig["sl_price"],
            client_id=f"sl_{sig['session']}",
        )
        tp_id = self.broker.place_take_profit_order(
            side=bracket_side, size=contracts,
            limit_price=sig["tp_price"],
            client_id=f"tp_{sig['session']}",
        )

        self._open_trade = OpenTrade(
            signal=sig,
            contracts=contracts,
            entry_order_id=pe["entry_order_id"],
            sl_order_id=sl_id,
            tp_order_id=tp_id,
        )
        self._pending_entry = None
        self.state.set(open_trade=self._open_trade.__dict__, pending_entry=None)
        if sm:
            sm.pending_signal = None
            sm.phase = SessionPhase.TRADE_OPEN

        journal_db.open_trade(
            trade_id=pe["entry_order_id"],
            symbol=config.SYMBOL,
            side=direction,
            entry_price=entry_price,
            size_btc=contracts,
            notional_usdt=contracts * entry_price,
            session=sig["session"],
            source="algo",
            leverage=config.LEVERAGE,
        )

        logger.info(
            f"TRADE OPEN | session={sig['session']} | dir={direction} "
            f"| contracts={contracts:.4f} | entry={entry_price:.4f} "
            f"| entry_id={pe['entry_order_id']} | sl_id={sl_id} | tp_id={tp_id}"
        )

        self.notifier.trade_entry(
            session=sig["session"],
            direction=direction,
            entry_price=entry_price,
            sl_price=sig["sl_price"],
            tp_price=sig["tp_price"],
            contracts=contracts,
            usdt_size=contracts * entry_price,
            entry_id=pe["entry_order_id"],
        )

    # ──────────────────────────────────────────
    # Monitor open trade
    # ──────────────────────────────────────────

    def _monitor_open_trade(self, bar: Bar) -> None:
        """
        Cross-check bar against SL/TP prices, and force-close at the owning
        session's close time if neither has hit yet (e.g. a London trade must
        exit when the London session ends, even mid-position, to free the slot
        up for NY — Entry A only ever holds one trade at a time).
        """
        sig = self._open_trade.signal
        direction  = sig["direction"]
        sl_price   = sig["sl_price"]
        tp_price   = sig["tp_price"]
        hold_side  = "short" if direction == "short" else "long"

        # Reconcile a position closed OUTSIDE the bot's own SL/TP monitor —
        # manually on Bitget, via the dashboard's generic "Close" button, etc.
        # Without this, the bot's own SL/TP bracket orders are left orphaned
        # on the exchange and the journal/state.json never get updated.
        try:
            still_open = any(
                p.side == hold_side and p.symbol == config.SYMBOL
                for p in self.broker.get_open_positions()
            )
        except Exception:
            still_open = True  # don't misfire on a transient API hiccup
        if not still_open:
            logger.warning(
                f"[{sig['session']}] Position closed externally — reconciling "
                f"(cancelling leftover bracket orders, updating journal)"
            )
            self._on_trade_close("manual_close", bar.close, bar)
            return

        sl_hit = (direction == "short" and bar.high >= sl_price) or \
                 (direction == "long"  and bar.low  <= sl_price)
        tp_hit = (direction == "short" and bar.low  <= tp_price) or \
                 (direction == "long"  and bar.high >= tp_price)

        if sl_hit or tp_hit:
            result = "sl_hit" if sl_hit else "tp_hit"
            exit_price = sl_price if sl_hit else tp_price
            logger.info(
                f"TRADE {result.upper()} | {sig['session']} | "
                f"dir={direction} | entry={sig['entry_price']:.4f} | "
                f"sl={sl_price:.4f} | tp={tp_price:.4f}"
            )
            self._on_trade_close(result, exit_price, bar)
            return

        session_cfg = next((s for s in config.SESSIONS if s.name == sig["session"]), None)
        if session_cfg:
            close_t = datetime.combine(
                bar.timestamp.date(),
                dtime(session_cfg.session_close_h, session_cfg.session_close_m),
                tzinfo=timezone.utc,
            )
            if bar.timestamp >= close_t:
                logger.info(
                    f"TRADE TIME EXIT | {sig['session']} session closed at "
                    f"{session_cfg.session_close_h:02d}:{session_cfg.session_close_m:02d} UTC "
                    f"— closing at market | dir={direction} entry={sig['entry_price']:.4f}"
                )
                hold_side = "short" if direction == "short" else "long"
                self.broker.flash_close(hold_side)
                self._on_trade_close("time_exit", bar.close, bar)

    def _on_trade_close(self, result: str, exit_price: float, bar: Bar) -> None:
        sig = self._open_trade.signal

        # Cancel whichever bracket order(s) didn't trigger. sl_hit/tp_hit leave the
        # other one still live; time_exit means neither fired (we closed manually).
        if result != "sl_hit" and self._open_trade.sl_order_id:
            self.broker.cancel_order(self._open_trade.sl_order_id)
        if result != "tp_hit" and self._open_trade.tp_order_id:
            self.broker.cancel_order(self._open_trade.tp_order_id)

        # Mark the owning session done — on_bar() only self-corrects TRADE_OPEN -> DONE
        # via its own close-time check, which only actually fires for a time_exit
        # (by definition already past close_t). An SL/TP hit can happen well before
        # close_t, so without this the dashboard would keep showing "Trade Open" for
        # a session that's actually finished for the day.
        owning_sm = self._london_sm if sig["session"] == "London" else self._ny_sm
        if owning_sm:
            owning_sm.phase = SessionPhase.DONE

        # Fetch actual net P&L (and fees, exit price) from Bitget history if possible
        pnl_usdt: Optional[float] = None
        fees_usdt: float = 0.0
        try:
            closed_data = self.broker.get_closed_position_data(
                hold_side="short" if sig["direction"] == "short" else "long"
            )
            if closed_data:
                pnl_usdt   = closed_data["net_pnl"]
                fees_usdt  = closed_data.get("fees", 0.0) or 0.0
                exit_price = closed_data.get("exit_price") or exit_price
        except Exception:
            pass

        # Calculate approximate P&L % for risk tracking
        entry      = sig["entry_price"]
        direction  = sig["direction"]
        pnl_pct = (
            (entry - exit_price) / entry if direction == "short"
            else (exit_price - entry) / entry
        ) * config.LEVERAGE

        daily  = self.state.daily_pnl_pct  + pnl_pct
        weekly = self.state.weekly_pnl_pct + pnl_pct
        self.state.set(
            daily_pnl_pct=daily,
            weekly_pnl_pct=weekly,
            open_trade=None,
        )

        journal_db.close_trade(
            trade_id  = self._open_trade.entry_order_id,
            exit_price = exit_price,
            pnl_usdt   = pnl_usdt if pnl_usdt is not None else 0.0,
            pnl_pct    = pnl_pct * 100,
            fees_usdt  = fees_usdt,
        )

        self._open_trade.status = result
        self._open_trade = None

        logger.info(
            f"TRADE CLOSED | {result} | pnl={pnl_pct*100:+.2f}% | "
            f"daily={daily*100:+.2f}% | weekly={weekly*100:+.2f}%"
        )

        self.notifier.trade_closed(
            result=result,
            session=sig["session"],
            direction=direction,
            entry_price=entry,
            exit_price=exit_price,
            pnl_usdt=pnl_usdt,
            pnl_pct=pnl_pct,
            daily_pnl_pct=daily,
        )

    # ──────────────────────────────────────────
    # Scaling ladder boost detection
    # ──────────────────────────────────────────

    def _check_ladder_boost(self) -> None:
        try:
            balance = self.broker.get_account_balance()
        except Exception:
            return
        rung = get_current_rung(balance)
        if self._last_rung_level is None:
            self._last_rung_level = rung.level
            return
        if rung.level > self._last_rung_level:
            old_rung = next((r for r in SCALING_LADDER if r.level == self._last_rung_level), rung)
            logger.info(
                f"POSITION BOOST | Level {old_rung.level} → {rung.level} | "
                f"${old_rung.default_usdt:,.0f} → ${rung.default_usdt:,.0f} USDT | equity=${balance:.2f}"
            )
            journal_db.log_boost(
                old_level=old_rung.level,
                new_level=rung.level,
                old_position_usdt=old_rung.default_usdt,
                new_position_usdt=rung.default_usdt,
                equity=balance,
                risk_pct=0.0,
            )
            self.notifier._send({
                "color": 0xF1C40F,
                "title": "🎉  Ladder Level Up!",
                "fields": [
                    {"name": "Level",         "value": f"{old_rung.level} → {rung.level}",                                   "inline": True},
                    {"name": "Default Size",  "value": f"${old_rung.default_usdt:,.0f} → ${rung.default_usdt:,.0f} USDT",   "inline": True},
                    {"name": "Boosted Size",  "value": f"${old_rung.boosted_usdt:,.0f} → ${rung.boosted_usdt:,.0f} USDT",   "inline": True},
                    {"name": "Equity",        "value": f"${balance:,.2f}",                                                   "inline": True},
                ],
            })
            self._last_rung_level = rung.level

    # ──────────────────────────────────────────
    # Heartbeat
    # ──────────────────────────────────────────

    def _maybe_send_heartbeat(self, now: datetime, price: float) -> None:
        interval = config.DISCORD_HEARTBEAT_INTERVAL_MINS
        if interval <= 0:
            return
        if self._last_heartbeat is not None:
            elapsed = (now - self._last_heartbeat).total_seconds() / 60
            if elapsed < interval:
                return
        self._last_heartbeat = now

        # Determine active session and phase for the embed — use configured
        # session hours (from .env), not hardcoded ones, so this tracks
        # LONDON_ORB_START_H / NY_ORB_START_H changes instead of drifting.
        hour = now.hour
        lon_cfg, ny_cfg = config.SESSIONS[0], config.SESSIONS[1]
        active_session = None
        phase = "Between sessions"
        if lon_cfg.orb_start_h <= hour < lon_cfg.session_close_h and self._london_sm:
            active_session = "London"
            phase = self._london_sm.phase.name.replace("_", " ").title()
        elif ny_cfg.orb_start_h <= hour < ny_cfg.session_close_h and self._ny_sm:
            active_session = "NY"
            phase = self._ny_sm.phase.name.replace("_", " ").title()

        self.notifier.heartbeat(
            session=active_session,
            phase=phase,
            daily_pnl_pct=float(self.state.daily_pnl_pct or 0),
            weekly_pnl_pct=float(self.state.weekly_pnl_pct or 0),
            open_trade=self._open_trade is not None,
            price=price,
        )

    # ──────────────────────────────────────────
    # Timing
    # ──────────────────────────────────────────

    def _sleep_until_next_bar(self) -> None:
        """Sleep until 5 seconds past the next minute boundary."""
        now = datetime.now(tz=timezone.utc)
        seconds_into_minute = now.second + now.microsecond / 1e6
        sleep_s = (60 - seconds_into_minute) + 5  # +5s for bar to be confirmed
        logger.debug(f"Sleeping {sleep_s:.1f}s until next bar…")
        time.sleep(sleep_s)


# ──────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Entry A Bitget Futures Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without placing real orders")
    args = parser.parse_args()

    setup_logger(config.LOG_DIR, config.LOG_LEVEL)
    logger.info("=" * 60)
    logger.info("Entry A Bitget Bot starting")
    logger.info(f"  Symbol : {config.SYMBOL}")
    logger.info(f"  Mode   : {config.TRADING_MODE}")
    logger.info(f"  DryRun : {args.dry_run or config.DRY_RUN}")
    logger.info("=" * 60)

    bot = EntryABot(dry_run=args.dry_run or config.DRY_RUN)
    bot.notifier.bot_started(dry_run=args.dry_run or config.DRY_RUN)

    # Reconcile any open exchange positions on startup
    try:
        positions = bot.broker.get_open_positions()
        if positions:
            logger.warning(f"Found {len(positions)} open position(s) on startup — will monitor them")
        else:
            logger.info("No open positions on exchange — clean start")
    except Exception as exc:
        logger.warning(f"Could not fetch open positions on startup: {exc} — continuing")

    bot.run()


if __name__ == "__main__":
    main()