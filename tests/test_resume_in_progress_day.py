"""
Regression test for the "SM stays None forever after a same-day restart" bug:
main.py only creates the session state machines inside _day_rollover(), which
only fires on an actual date change. Any restart that happens later the same
day left _london_sm/_ny_sm as None for the rest of the process's life, silently
disabling the entire trading engine with no error logged. _resume_in_progress_day()
must rebuild them from persisted state (bias, prev-day range, any in-flight
trade) and replay today's bars so ORB/breakout progress isn't lost.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
import main
from main import EntryABot
from src.data.feed import Bar
from src.strategy.entry_a import BiasCalculator, SessionPhase

_LON = config.SESSIONS[0]  # read the real configured ORB window rather than assuming 09:00


class FakeState:
    def __init__(self, **kwargs):
        self.london_bias_bearish  = kwargs.get("london_bias_bearish")
        self.ny_bias_bearish      = kwargs.get("ny_bias_bearish")
        self.london_bias_override = kwargs.get("london_bias_override")
        self.ny_bias_override     = kwargs.get("ny_bias_override")
        self.prev_day_high        = kwargs.get("prev_day_high")
        self.prev_day_low         = kwargs.get("prev_day_low")
        self.open_trade           = kwargs.get("open_trade")


class FakeNotifier:
    def __init__(self):
        self.missed_signals = []

    def signal_missed(self, session, direction, entry_price, at_time):
        self.missed_signals.append((session, direction, entry_price, at_time))


def _bare_bot(state: FakeState) -> EntryABot:
    """Construct an EntryABot without running __init__ (which touches real
    network/state/db paths) — only the attributes _resume_in_progress_day needs."""
    bot = EntryABot.__new__(EntryABot)
    bot.state = state
    bot.bias = BiasCalculator()
    bot.notifier = FakeNotifier()
    bot._london_sm = None
    bot._ny_sm = None
    bot._open_trade = None
    bot._applied_london_bias = None
    bot._applied_ny_bias = None
    return bot


def _bar(day, hour, minute, o, h, l, c):
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=1.0)


class ResumeInProgressDayTest(unittest.TestCase):
    def test_session_machines_are_no_longer_none_after_resume(self):
        state = FakeState(london_bias_bearish=False, prev_day_high=61330.8, prev_day_low=59562.4)
        bot = _bare_bot(state)
        now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
        bot._resume_in_progress_day(now, bars=[])
        self.assertIsNotNone(bot._london_sm)
        self.assertIsNotNone(bot._ny_sm)

    def test_replays_todays_bars_to_rebuild_orb_after_restart(self):
        # Bias already known (as if captured before the restart), ORB window
        # already fully in the past relative to "now" — a fresh SM with no
        # replay would immediately go DONE with no ORB ever built.
        state = FakeState(london_bias_bearish=False, prev_day_high=61330.8, prev_day_low=59562.4)
        bot = _bare_bot(state)
        day = datetime(2026, 7, 2).date()
        h0 = _LON.orb_start_h
        now = datetime(2026, 7, 2, h0 + 1, 0, tzinfo=timezone.utc)

        bars = [_bar(day, h0, m, 100.5, 101.0, 100.0, 100.5) for m in range(0, 31)]
        bars.append(_bar(day, h0, 45, 100.6, 100.8, 100.4, 100.6))  # a bar after ORB, still no sweep

        bot._resume_in_progress_day(now, bars)

        self.assertTrue(bot._london_sm.orb.complete)
        self.assertEqual(bot._london_sm.orb.high, 101.0)
        self.assertEqual(bot._london_sm.orb.low, 100.0)
        self.assertEqual(bot._london_sm.phase, SessionPhase.WAITING_FOR_BREAKOUT)

    def test_signal_during_offline_gap_is_not_acted_on_but_alerted(self):
        state = FakeState(london_bias_bearish=False, prev_day_high=61330.8, prev_day_low=59562.4)
        bot = _bare_bot(state)
        day = datetime(2026, 7, 2).date()
        h0 = _LON.orb_start_h
        now = datetime(2026, 7, 2, h0 + 1, 0, tzinfo=timezone.utc)

        bars = [_bar(day, h0, m, 100.5, 101.0, 100.0, 100.5) for m in range(0, 31)]
        bars.append(_bar(day, h0, 40, 100.0, 100.1, 99.8, 99.9))    # sweep below ORB low
        bars.append(_bar(day, h0, 45, 99.9, 100.3, 99.9, 100.2))    # reclaim -> would-be signal

        bot._resume_in_progress_day(now, bars)

        self.assertIsNone(bot._open_trade)  # never actually opened
        self.assertEqual(len(bot.notifier.missed_signals), 1)
        session, direction, entry_price, _ = bot.notifier.missed_signals[0]
        self.assertEqual(session, "London")
        self.assertEqual(direction, "long")
        self.assertEqual(bot._london_sm.phase, SessionPhase.DONE)

    def test_restores_in_flight_trade_instead_of_replaying_over_it(self):
        open_trade = {
            "signal": {
                "session": "London", "direction": "long", "entry_price": 58572.6,
                "sl_price": 58133.31, "tp_price": 61480.9, "orb_high": 58806.8,
                "orb_low": 58571.2, "breakout_level": 58571.2,
            },
            "contracts": 0.01, "entry_order_id": "abc", "sl_order_id": "def",
            "tp_order_id": "ghi", "status": "open",
        }
        state = FakeState(london_bias_bearish=False, prev_day_high=61330.8,
                           prev_day_low=59562.4, open_trade=open_trade)
        bot = _bare_bot(state)
        now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)

        bot._resume_in_progress_day(now, bars=[])

        self.assertIsNotNone(bot._open_trade)
        self.assertEqual(bot._open_trade.status, "open")
        self.assertEqual(bot._london_sm.phase, SessionPhase.TRADE_OPEN)
        self.assertEqual(bot._london_sm.signal.entry_price, 58572.6)
        self.assertEqual(len(bot.notifier.missed_signals), 0)


if __name__ == "__main__":
    unittest.main()
