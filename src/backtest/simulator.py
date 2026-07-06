"""
Offline replay engine for Entry A.

Runs the exact same `SessionStateMachine` used by the live bot (main.py) over
a full day of historical bars, bar by bar, so the strategy's ORB/breakout/
confirm/entry logic can be tested and played back without touching the
exchange. Mirrors main.py's tick ordering precisely:
  1. capture bias reference prices (every bar, regardless of open trade)
  2. if a trade is open, only monitor it for SL/TP/time-exit — session state
     machines are not driven while a trade is open ("one trade at a time")
  3. otherwise drive both session state machines, which may emit a signal

A trade is force-closed at its owning session's close time even if neither SL
nor TP has hit — Entry A only ever holds one trade at a time, so a London
trade must exit when London closes to free the slot up for NY.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as date_cls, datetime, timedelta, timezone, time as dtime
from typing import Optional

import config
from src.data.feed import Bar, BitgetFeed
from src.strategy.entry_a import BiasCalculator, SessionPhase, SessionStateMachine, TradeSignal


@dataclass
class SimSessionSnapshot:
    phase: str
    orb_high: Optional[float]
    orb_low: Optional[float]
    breakout_level: Optional[float]
    bias_bearish: Optional[bool]


@dataclass
class BarSnapshot:
    bar: Bar
    london: SimSessionSnapshot
    ny: SimSessionSnapshot
    open_trade: Optional[dict]
    events: list = field(default_factory=list)


@dataclass
class SimTrade:
    session: str
    direction: str
    entry_time: datetime
    entry_price: float
    sl_price: float
    tp_price: float
    orb_high: float
    orb_low: float
    breakout_level: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    result: str = "open"  # "open" | "tp_hit" | "sl_hit" | "still_open"
    pnl_pct: Optional[float] = None


@dataclass
class SimulationResult:
    day: date_cls
    bars: list
    snapshots: list
    trades: list
    yesterday_open: Optional[float]
    prev_day_high: Optional[float]
    prev_day_low: Optional[float]
    price_at_09: Optional[float] = None
    price_at_ny_open: Optional[float] = None
    london_bias: Optional[bool] = None
    ny_bias: Optional[bool] = None


def fetch_day_bars(feed: BitgetFeed, day: date_cls) -> list:
    """Fetch every 1m bar for `day` (00:00 UTC to now/midnight), paginating past
    Bitget's 1000-candles-per-call cap (a day needs up to 1440)."""
    start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    end   = start + timedelta(days=1)
    now   = datetime.now(timezone.utc)
    if end > now:
        end = now

    all_bars: list = []
    cursor_end = end
    for _ in range(4):  # 4 x 1000min ~= 66h of headroom, comfortably covers one day
        batch = feed.fetch_ohlcv(
            config.SYMBOL, "1m", count=1000,
            from_ts=int(start.timestamp() * 1000),
            to_ts=int(cursor_end.timestamp() * 1000),
        )
        if not batch:
            break
        all_bars = batch + all_bars
        oldest = batch[0].timestamp
        if oldest <= start:
            break
        cursor_end = oldest - timedelta(minutes=1)

    seen = set()
    deduped = []
    for b in sorted(all_bars, key=lambda b: b.timestamp):
        if b.timestamp in seen or not (start <= b.timestamp < end):
            continue
        seen.add(b.timestamp)
        deduped.append(b)
    return deduped


def fetch_reference_prices(feed: BitgetFeed, day: date_cls) -> dict:
    """Yesterday's daily candle (open/high/low), same source main.py now uses."""
    yesterday = day - timedelta(days=1)
    daily = feed.fetch_ohlcv(config.SYMBOL, "1d", count=5)
    y_bar = next((b for b in daily if b.timestamp.date() == yesterday), None)
    if y_bar is None and daily:
        y_bar = next((b for b in daily if b.timestamp.date() < day), None)
    return {
        "yesterday_open": y_bar.open if y_bar else None,
        "prev_day_high":  y_bar.high if y_bar else None,
        "prev_day_low":   y_bar.low  if y_bar else None,
    }


def run_simulation(
    day: date_cls,
    bars: list,
    yesterday_open: Optional[float],
    prev_day_high: Optional[float],
    prev_day_low: Optional[float],
) -> SimulationResult:
    bias_calc = BiasCalculator()
    london_cfg, ny_cfg = config.SESSIONS[0], config.SESSIONS[1]
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc)
    london_sm = SessionStateMachine(london_cfg, day_start)
    ny_sm     = SessionStateMachine(ny_cfg, day_start)

    if prev_day_high and prev_day_low:
        pdr = bias_calc.compute_prev_day_range_pct(prev_day_high, prev_day_low)
        london_sm.set_prev_day_range_pct(pdr)
        ny_sm.set_prev_day_range_pct(pdr)

    price_at_09: Optional[float] = None
    price_at_ny_open: Optional[float] = None
    london_bias: Optional[bool] = None
    ny_bias: Optional[bool] = None

    snapshots: list = []
    trades: list = []
    open_trade: Optional[SimTrade] = None
    open_sm_key: Optional[str] = None

    for bar in bars:
        hour, minute = bar.timestamp.hour, bar.timestamp.minute
        events: list = []

        # ── Bias reference prices — captured every bar, same as main.py ──
        if price_at_09 is None and hour == london_cfg.orb_start_h and minute == london_cfg.orb_start_m:
            price_at_09 = bar.close
            if yesterday_open:
                london_bias = bias_calc.compute_london_bias(yesterday_open, price_at_09)
                london_sm.set_bias(london_bias)
                events.append(f"London bias set: {'BEARISH' if london_bias else 'BULLISH'}")

        if price_at_ny_open is None and hour == ny_cfg.orb_start_h and minute == ny_cfg.orb_start_m:
            price_at_ny_open = bar.close
            if price_at_09 is not None:
                ny_bias = bias_calc.compute_ny_bias(price_at_09, price_at_ny_open)
                ny_sm.set_bias(ny_bias)
                events.append(f"NY bias set: {'BEARISH' if ny_bias else 'BULLISH'}")

        # ── Drive session state machines — only when no trade is open, mirroring
        #    main.py's "one trade at a time" gate in _tick() ──
        if open_trade is None:
            for key, sm, cfg in (("london", london_sm, london_cfg), ("ny", ny_sm, ny_cfg)):
                if not (cfg.orb_start_h <= hour < cfg.session_close_h):
                    continue
                if sm.bias_is_bearish is None:
                    continue
                before_phase = sm.phase
                signal = sm.on_bar(bar)
                if sm.phase != before_phase:
                    events.append(f"{cfg.name}: {before_phase.name} -> {sm.phase.name}")
                if signal:
                    open_trade = SimTrade(
                        session=signal.session, direction=signal.direction,
                        entry_time=bar.timestamp, entry_price=signal.entry_price,
                        sl_price=signal.sl_price, tp_price=signal.tp_price,
                        orb_high=signal.orb_high, orb_low=signal.orb_low,
                        breakout_level=signal.breakout_level,
                    )
                    open_sm_key = key
                    events.append(f"{cfg.name}: SIGNAL {signal.direction.upper()} @ {signal.entry_price:.2f}")
                    break  # one trade at a time — don't let the other session also fire this bar

        # ── Monitor the open trade for SL/TP/time-exit ──
        elif open_trade is not None:
            sm = london_sm if open_sm_key == "london" else ny_sm
            owning_cfg = london_cfg if open_sm_key == "london" else ny_cfg
            fake_signal = TradeSignal(
                session=open_trade.session, direction=open_trade.direction,
                entry_price=open_trade.entry_price, sl_price=open_trade.sl_price,
                tp_price=open_trade.tp_price, orb_high=open_trade.orb_high,
                orb_low=open_trade.orb_low, breakout_level=open_trade.breakout_level,
            )
            sltp = sm.check_sl_tp(bar, fake_signal)

            result: Optional[str] = None
            exit_price: Optional[float] = None
            if sltp == "sl":
                result, exit_price = "sl_hit", open_trade.sl_price
            elif sltp == "tp":
                result, exit_price = "tp_hit", open_trade.tp_price
            else:
                close_t = datetime.combine(
                    day, dtime(owning_cfg.session_close_h, owning_cfg.session_close_m), tzinfo=timezone.utc
                )
                if bar.timestamp >= close_t:
                    result, exit_price = "time_exit", bar.close

            if result:
                open_trade.exit_time = bar.timestamp
                open_trade.exit_price = exit_price
                open_trade.result = result
                entry, d = open_trade.entry_price, open_trade.direction
                open_trade.pnl_pct = ((entry - exit_price) / entry if d == "short" else (exit_price - entry) / entry) * 100
                events.append(f"{open_trade.session}: {result.upper()} @ {exit_price:.2f} ({open_trade.pnl_pct:+.2f}%)")
                sm.phase = SessionPhase.DONE  # session finished for the day, even if closed well before its own close_t
                trades.append(open_trade)
                open_trade = None
                open_sm_key = None

        snapshots.append(BarSnapshot(
            bar=bar,
            london=SimSessionSnapshot(
                phase=london_sm.phase.name,
                orb_high=(london_sm.orb.high if london_sm.orb.complete else None),
                orb_low=(london_sm.orb.low if london_sm.orb.complete else None),
                breakout_level=london_sm.breakout_level,
                bias_bearish=london_sm.bias_is_bearish,
            ),
            ny=SimSessionSnapshot(
                phase=ny_sm.phase.name,
                orb_high=(ny_sm.orb.high if ny_sm.orb.complete else None),
                orb_low=(ny_sm.orb.low if ny_sm.orb.complete else None),
                breakout_level=ny_sm.breakout_level,
                bias_bearish=ny_sm.bias_is_bearish,
            ),
            open_trade=(open_trade.__dict__.copy() if open_trade else None),
            events=events,
        ))

    if open_trade is not None:
        open_trade.result = "still_open"
        trades.append(open_trade)

    return SimulationResult(
        day=day, bars=bars, snapshots=snapshots, trades=trades,
        yesterday_open=yesterday_open, prev_day_high=prev_day_high, prev_day_low=prev_day_low,
        price_at_09=price_at_09, price_at_ny_open=price_at_ny_open,
        london_bias=london_bias, ny_bias=ny_bias,
    )
