"""
Regression test for the missing session time-exit rule: Entry A only ever
holds one trade at a time, so a London trade must be force-closed when the
London session ends (session_close_h/m) even if neither SL nor TP has hit yet
— otherwise it would sit open forever, blocking NY from ever getting a turn.
Previously nothing enforced this: SessionStateMachine.on_bar() marked the
session DONE at close time but never generated an actual exit, and
main.py's trade monitor only checked SL/TP.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from src.data.feed import Bar
from src.strategy.entry_a import TradeSignal
from src.backtest.simulator import run_simulation

_LON = config.SESSIONS[0]
_NY  = config.SESSIONS[1]


class FakeBroker:
    def __init__(self):
        self.flash_closed = []
        self.cancelled = []

    def flash_close(self, hold_side):
        self.flash_closed.append(hold_side)
        return True

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def get_closed_position_data(self, hold_side):
        return None


class FakeNotifier:
    def __init__(self):
        self.closed = []

    def trade_closed(self, **kwargs):
        self.closed.append(kwargs)


class FakeState:
    def __init__(self):
        self.daily_pnl_pct = 0.0
        self.weekly_pnl_pct = 0.0
        self._data = {}

    def set(self, **kwargs):
        self._data.update(kwargs)
        for k, v in kwargs.items():
            setattr(self, k, v)


def _bar(day, hour, minute, close, o=None, h=None, l=None):
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)
    o = o if o is not None else close
    h = h if h is not None else close
    l = l if l is not None else close
    return Bar(timestamp=ts, open=o, high=h, low=l, close=close, volume=1.0)


class MainPyTimeExitTest(unittest.TestCase):
    """Exercises the real main.py _monitor_open_trade/_on_trade_close path."""

    def setUp(self):
        import main
        self.main = main
        self.bot = main.EntryABot.__new__(main.EntryABot)
        self.bot.broker = FakeBroker()
        self.bot.notifier = FakeNotifier()
        self.bot.state = FakeState()
        self.bot._london_sm = None
        self.bot._ny_sm = None

        from src.strategy.entry_a import OpenTrade
        self.day = datetime(2026, 7, 2).date()
        signal = {
            "session": "London", "direction": "long", "entry_price": 58572.6,
            "sl_price": 58133.31, "tp_price": 61480.9, "orb_high": 58806.8,
            "orb_low": 58571.2, "breakout_level": 58571.2,
        }
        self.bot._open_trade = OpenTrade(
            signal=signal, contracts=0.01,
            entry_order_id="e1", sl_order_id="sl1", tp_order_id="tp1", status="open",
        )

    def test_no_exit_before_sl_tp_or_close_time(self):
        bar = _bar(self.day, _LON.session_close_h - 1, 0, close=59000.0)
        self.bot._monitor_open_trade(bar)
        self.assertIsNotNone(self.bot._open_trade)
        self.assertEqual(self.bot.broker.flash_closed, [])

    def test_force_closes_at_session_close_time_with_no_sl_tp_hit(self):
        bar = _bar(self.day, _LON.session_close_h, _LON.session_close_m, close=59500.0)
        self.bot._monitor_open_trade(bar)

        self.assertIsNone(self.bot._open_trade)
        self.assertEqual(self.bot.broker.flash_closed, ["long"])
        self.assertIn("sl1", self.bot.broker.cancelled)
        self.assertIn("tp1", self.bot.broker.cancelled)
        self.assertEqual(len(self.bot.notifier.closed), 1)
        closed = self.bot.notifier.closed[0]
        self.assertEqual(closed["result"], "time_exit")
        self.assertEqual(closed["exit_price"], 59500.0)

    def test_sl_hit_takes_priority_over_time_exit_on_the_same_bar(self):
        bar = _bar(self.day, _LON.session_close_h, _LON.session_close_m,
                   close=57900.0, l=57900.0, h=58900.0)
        self.bot._monitor_open_trade(bar)
        self.assertEqual(self.bot.notifier.closed[0]["result"], "sl_hit")
        self.assertEqual(self.bot.broker.flash_closed, [])  # SL bracket order handled it, no flash close needed


class SimulatorTimeExitTest(unittest.TestCase):
    """Exercises the simulator's mirrored time-exit logic."""

    def test_ny_gets_its_own_orb_and_trade_after_london_closes(self):
        # NY's configured ORB window (12:00-12:30) falls *before* London's
        # configured session close (14:00) in this project's real .env — so to
        # test "does NY get a clean turn" without also exercising that overlap
        # edge case, close London's trade early via a genuine TP hit, well
        # before NY's own ORB window begins.
        day = datetime(2026, 7, 2).date()
        bars = []

        for m in range(0, 31):
            bars.append(_bar(day, _LON.orb_start_h, m, close=100.5, o=100.5, h=101.0, l=100.0))
        bars.append(_bar(day, _LON.orb_start_h, 40, close=99.9, l=99.8, h=100.0))   # sweep below ORB low
        bars.append(_bar(day, _LON.orb_start_h, 45, close=100.2, l=99.9, h=100.3))  # reclaim -> long entry
        bars.append(_bar(day, _LON.orb_start_h + 1, 0, close=111.0, h=111.0, l=100.3))  # TP hit, closes London early

        # Kept close to London's price level (unlike the earlier ~200 draft) so NY's
        # bias — price_at_ny_open vs price_at_09(=100.5) — comes out BEARISH, matching
        # this short setup (sweep above ORB high, reclaim back below).
        for m in range(0, 31):
            bars.append(_bar(day, _NY.orb_start_h, m, close=95.5, o=95.5, h=96.0, l=95.0))
        bars.append(_bar(day, _NY.orb_start_h, 40, close=96.5, h=96.6, l=96.3))  # sweep above ORB high
        bars.append(_bar(day, _NY.orb_start_h, 45, close=95.8, h=96.4, l=95.7))  # reclaim -> short entry

        close_t = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(
            hours=_NY.session_close_h, minutes=_NY.session_close_m)
        bars.append(Bar(timestamp=close_t, open=95.8, high=96.0, low=95.6, close=95.9, volume=1.0))

        bars.sort(key=lambda b: b.timestamp)

        result = run_simulation(day, bars, yesterday_open=100.0, prev_day_high=105.0, prev_day_low=95.0)

        self.assertEqual(len(result.trades), 2)
        london_trade, ny_trade = result.trades[0], result.trades[1]
        self.assertEqual(london_trade.session, "London")
        self.assertEqual(london_trade.result, "tp_hit")

        self.assertEqual(ny_trade.session, "NY")
        self.assertEqual(ny_trade.direction, "short")
        self.assertEqual(ny_trade.result, "time_exit")
        self.assertEqual(ny_trade.exit_time, close_t)


if __name__ == "__main__":
    unittest.main()
