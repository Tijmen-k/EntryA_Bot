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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from loguru import logger

import config
from src.utils.logger import setup_logger
from src.data.feed import KrakenFeed, Bar
from src.broker.kraken import KrakenBroker, Position
from src.strategy.entry_a import (
    SessionStateMachine, BiasCalculator, TradeSignal, OpenTrade, SessionPhase
)
from src.risk.sizing import PositionSizer


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
        "daily_pnl_pct": 0.0,
        "weekly_pnl_pct": 0.0,
        "london_bias_bearish": None,
        "ny_bias_bearish": None,
        "price_at_09": None,
        "price_at_14": None,
        "yesterday_open": None,
        "prev_day_high": None,
        "prev_day_low": None,
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
            # reload config constant
            config.DRY_RUN = True

        self.feed   = KrakenFeed()
        self.broker = KrakenBroker()
        self.sizer  = PositionSizer()
        self.bias   = BiasCalculator()
        self.state  = BotState(config.STATE_FILE)

        self._london_sm: Optional[SessionStateMachine] = None
        self._ny_sm:     Optional[SessionStateMachine] = None
        self._active_sm: Optional[SessionStateMachine] = None
        self._open_trade: Optional[OpenTrade] = None

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
                sys.exit(0)
            except Exception as exc:
                logger.exception(f"Unhandled exception in tick: {exc}")
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

        # ── Daily loss limit guard ─────────────
        if self.state.daily_pnl_pct <= -config.MAX_DAILY_LOSS_PCT:
            logger.warning(f"Daily loss limit reached ({self.state.daily_pnl_pct*100:.1f}%) — no more entries today")
            return

        # ── Fetch latest bars ───────────────────
        bars = self.feed.fetch_ohlcv(config.SYMBOL, config.RESOLUTION, count=350)
        if not bars:
            logger.warning("No bars received — skipping tick")
            return

        last_bar = bars[-1]
        logger.debug(f"Latest bar: {last_bar.timestamp.isoformat()} "
                     f"O={last_bar.open} H={last_bar.high} L={last_bar.low} C={last_bar.close}")

        # ── Capture key reference prices ───────
        self._update_reference_prices(bars, now)

        # ── Monitor open trade if any ───────────
        if self._open_trade and self._open_trade.status == "open":
            self._monitor_open_trade(last_bar)
            return  # one active trade at a time

        # ── Run session state machines ──────────
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

        logger.info("Session state machines reset for new day")

    # ──────────────────────────────────────────
    # Reference price tracking
    # ──────────────────────────────────────────

    def _update_reference_prices(self, bars: list[Bar], now: datetime) -> None:
        """
        Capture prices at key timestamps for bias calculation.
        Fires once per day per checkpoint.
        """
        today = now.date()

        # Yesterday's open: first bar of yesterday (00:00 UTC)
        if self.state.yesterday_open is None:
            yesterday = today - timedelta(days=1)
            for b in bars:
                if b.timestamp.date() == yesterday and b.timestamp.hour == 0 and b.timestamp.minute == 0:
                    self.state.set(yesterday_open=b.open)
                    logger.info(f"Yesterday open captured: {b.open:.4f}")
                    break

        # Previous day high/low for measured move
        if self.state.prev_day_high is None:
            yesterday = today - timedelta(days=1)
            yest_bars = [b for b in bars if b.timestamp.date() == yesterday]
            if yest_bars:
                ph = max(b.high for b in yest_bars)
                pl = min(b.low  for b in yest_bars)
                self.state.set(prev_day_high=ph, prev_day_low=pl)
                logger.info(f"Prev day range: H={ph:.4f} L={pl:.4f}")

        # Price at 09:00 UTC (London session open / bias endpoint 1)
        if self.state.price_at_09 is None:
            ref_09 = self._find_bar_at(bars, today, 9, 0)
            if ref_09:
                self.state.set(price_at_09=ref_09.close)
                logger.info(f"Price at 09:00 UTC captured: {ref_09.close:.4f}")
                # Calculate London bias now
                if self.state.yesterday_open:
                    is_bearish = self.bias.compute_london_bias(
                        self.state.yesterday_open, ref_09.close
                    )
                    self.state.set(london_bias_bearish=is_bearish)
                    if self._london_sm:
                        self._london_sm.set_bias(is_bearish)
                    if self._london_sm and self.state.prev_day_high and self.state.prev_day_low:
                        pdr = self.bias.compute_prev_day_range_pct(
                            self.state.prev_day_high, self.state.prev_day_low
                        )
                        self._london_sm.set_prev_day_range_pct(pdr)

        # Price at 14:00 UTC (NY session open / bias endpoint 2)
        if self.state.price_at_14 is None:
            ref_14 = self._find_bar_at(bars, today, 14, 0)
            if ref_14:
                self.state.set(price_at_14=ref_14.close)
                logger.info(f"Price at 14:00 UTC captured: {ref_14.close:.4f}")
                # Calculate NY bias
                if self.state.price_at_09:
                    is_bearish = self.bias.compute_ny_bias(
                        self.state.price_at_09, ref_14.close
                    )
                    self.state.set(ny_bias_bearish=is_bearish)
                    if self._ny_sm:
                        self._ny_sm.set_bias(is_bearish)
                    if self._ny_sm and self.state.prev_day_high and self.state.prev_day_low:
                        pdr = self.bias.compute_prev_day_range_pct(
                            self.state.prev_day_high, self.state.prev_day_low
                        )
                        self._ny_sm.set_prev_day_range_pct(pdr)

    @staticmethod
    def _find_bar_at(bars: list[Bar], date, hour: int, minute: int) -> Optional[Bar]:
        from datetime import time as dtime
        target_time = dtime(hour, minute)
        for b in bars:
            if b.timestamp.date() == date and b.timestamp.hour == hour and b.timestamp.minute == minute:
                return b
        return None

    # ──────────────────────────────────────────
    # Session routing
    # ──────────────────────────────────────────

    def _run_sessions(self, bar: Bar, now: datetime) -> None:
        hour = now.hour

        # London: 09:00–14:00 UTC
        if 9 <= hour < 14:
            if self._london_sm and self.state.london_bias_bearish is not None:
                self._process_sm(self._london_sm, bar)

        # NY: 14:00–23:00 UTC
        elif 14 <= hour < 23:
            if self._ny_sm and self.state.ny_bias_bearish is not None:
                self._process_sm(self._ny_sm, bar)

    def _process_sm(self, sm: SessionStateMachine, bar: Bar) -> None:
        signal = sm.on_bar(bar)
        if signal:
            self._on_signal(signal, bar)

    # ──────────────────────────────────────────
    # Signal handling — open trade
    # ──────────────────────────────────────────

    def _on_signal(self, signal: TradeSignal, bar: Bar) -> None:
        logger.info(
            f"TRADE SIGNAL | {signal.session} | {signal.direction.upper()} | "
            f"entry={signal.entry_price:.4f} sl={signal.sl_price:.4f} tp={signal.tp_price:.4f}"
        )

        # Fetch current balance and price
        balance = self.broker.get_account_balance()
        price   = self.feed.get_current_price(config.SYMBOL) or bar.close

        if balance <= 0:
            logger.warning("Zero balance — skipping trade")
            return

        contracts = self.sizer.calculate(balance, price, signal.sl_price / signal.entry_price - 1
                                         if signal.direction == "short"
                                         else 1 - signal.sl_price / signal.entry_price)
        if contracts <= 0:
            logger.warning("Sizing returned 0 contracts — skipping trade")
            return

        # Determine order sides
        entry_side = "sell" if signal.direction == "short" else "buy"
        exit_side  = "buy"  if signal.direction == "short" else "sell"

        # Place entry market order
        entry_id = self.broker.place_market_order(
            side=entry_side, size=contracts, client_id=f"entry_{signal.session}"
        )
        if not entry_id:
            logger.error("Entry order failed — no trade opened")
            return

        # Place SL stop order
        sl_id = self.broker.place_stop_market_order(
            side=exit_side, size=contracts,
            stop_price=signal.sl_price,
            client_id=f"sl_{signal.session}",
        )

        # Place TP limit order
        tp_id = self.broker.place_take_profit_order(
            side=exit_side, size=contracts,
            limit_price=signal.tp_price,
            client_id=f"tp_{signal.session}",
        )

        self._open_trade = OpenTrade(
            signal=signal.__dict__,
            contracts=contracts,
            entry_order_id=entry_id,
            sl_order_id=sl_id,
            tp_order_id=tp_id,
        )
        self.state.set(open_trade=self._open_trade.__dict__)

        logger.info(
            f"TRADE OPEN | session={signal.session} | dir={signal.direction} "
            f"| contracts={contracts:.4f} | entry_id={entry_id} | sl_id={sl_id} | tp_id={tp_id}"
        )

    # ──────────────────────────────────────────
    # Monitor open trade
    # ──────────────────────────────────────────

    def _monitor_open_trade(self, bar: Bar) -> None:
        """
        Cross-check bar against SL/TP prices.
        Kraken will have already triggered the exchange orders, but this
        catches the result so we can update state and cancel the other order.
        """
        sig = self._open_trade.signal
        direction  = sig["direction"]
        sl_price   = sig["sl_price"]
        tp_price   = sig["tp_price"]

        sl_hit = (direction == "short" and bar.high >= sl_price) or \
                 (direction == "long"  and bar.low  <= sl_price)
        tp_hit = (direction == "short" and bar.low  <= tp_price) or \
                 (direction == "long"  and bar.high >= tp_price)

        if sl_hit or tp_hit:
            result = "sl_hit" if sl_hit else "tp_hit"
            logger.info(
                f"TRADE {result.upper()} | {sig['session']} | "
                f"dir={direction} | entry={sig['entry_price']:.4f} | "
                f"sl={sl_price:.4f} | tp={tp_price:.4f}"
            )
            self._on_trade_close(result, bar)

    def _on_trade_close(self, result: str, bar: Bar) -> None:
        sig = self._open_trade.signal

        # Cancel the surviving bracket order
        if result == "sl_hit" and self._open_trade.tp_order_id:
            self.broker.cancel_order(self._open_trade.tp_order_id)
        elif result == "tp_hit" and self._open_trade.sl_order_id:
            self.broker.cancel_order(self._open_trade.sl_order_id)

        # Calculate rough P&L for risk tracking
        entry = sig["entry_price"]
        exit_price = (
            sig["sl_price"] if result == "sl_hit" else sig["tp_price"]
        )
        direction = sig["direction"]
        pnl_pct = (
            (entry - exit_price) / entry if direction == "short"
            else (exit_price - entry) / entry
        ) * config.LEVERAGE

        daily = self.state.daily_pnl_pct + pnl_pct
        weekly = self.state.weekly_pnl_pct + pnl_pct
        self.state.set(
            daily_pnl_pct=daily,
            weekly_pnl_pct=weekly,
            open_trade=None,
        )

        self._open_trade.status = result
        self._open_trade = None

        logger.info(
            f"TRADE CLOSED | {result} | pnl={pnl_pct*100:+.2f}% | "
            f"daily={daily*100:+.2f}% | weekly={weekly*100:+.2f}%"
        )

        if daily <= -config.MAX_DAILY_LOSS_PCT:
            logger.warning(f"Daily loss limit breached ({daily*100:.1f}%) — trading halted for today")

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
    parser = argparse.ArgumentParser(description="Entry A Kraken Futures Bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="Simulate trades without placing real orders")
    args = parser.parse_args()

    setup_logger(config.LOG_DIR, config.LOG_LEVEL)
    logger.info("=" * 60)
    logger.info("Entry A Kraken Bot starting")
    logger.info(f"  Symbol : {config.SYMBOL}")
    logger.info(f"  Mode   : {config.TRADING_MODE}")
    logger.info(f"  DryRun : {args.dry_run or config.DRY_RUN}")
    logger.info("=" * 60)

    bot = EntryABot(dry_run=args.dry_run or config.DRY_RUN)

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
