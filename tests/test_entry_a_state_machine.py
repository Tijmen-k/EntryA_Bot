"""
Regression test for the exact scenario reported as "entry not working": bullish
bias, price wicks/moves below the ORB low, then closes back inside a few bars
later — Entry A must fire a long signal on that close, with no buffer/slippage
padding on the entry price (per user request: enter instantly at the
confirming bar's close).
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.feed import Bar
from src.strategy.entry_a import SessionStateMachine, SessionPhase
from config import SessionConfig


def _bar(minute: int, o, h, l, c, day=None):
    day = day or datetime(2026, 7, 2, tzinfo=timezone.utc).date()
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=9, minutes=minute)
    return Bar(timestamp=ts, open=o, high=h, low=l, close=c, volume=1.0)


def _london_cfg() -> SessionConfig:
    return SessionConfig(
        name="London", orb_start_h=9, orb_start_m=0, orb_end_h=9, orb_end_m=30,
        session_close_h=14, session_close_m=0, sl_pct=0.0075,
    )


class SweepAndReclaimFiresLongTest(unittest.TestCase):
    def setUp(self):
        self.sm = SessionStateMachine(_london_cfg(), datetime(2026, 7, 2, tzinfo=timezone.utc))
        self.sm.set_bias(False)  # bullish -> fade long on a sweep below ORB low
        self.sm.set_prev_day_range_pct(0.02)

        # Build a simple ORB: low=100, high=101 across the 09:00-09:30 window
        # (the bar at minute 30 == orb_end is what actually triggers completion)
        for m in range(0, 31):
            self.sm.on_bar(_bar(m, 100.5, 101.0, 100.0, 100.5))
        self.assertTrue(self.sm.orb.complete)
        self.assertEqual(self.sm.phase, SessionPhase.WAITING_FOR_BREAKOUT)

    def test_wick_below_orb_low_then_later_close_back_inside_fires_long(self):
        # Sweep bar: wicks below ORB low (99.8 < 100.0) but doesn't close back inside yet
        sig = self.sm.on_bar(_bar(31, 100.0, 100.1, 99.8, 99.9))
        self.assertIsNone(sig)
        self.assertEqual(self.sm.phase, SessionPhase.WAITING_FOR_CONFIRM)

        # Still below the breakout level a couple bars later — no signal yet
        sig = self.sm.on_bar(_bar(32, 99.9, 99.95, 99.85, 99.9))
        self.assertIsNone(sig)
        self.assertEqual(self.sm.phase, SessionPhase.WAITING_FOR_CONFIRM)

        # Price closes back above the ORB low a few minutes later -> long signal, no buffer
        confirm_bar = _bar(35, 99.9, 100.3, 99.9, 100.2)
        sig = self.sm.on_bar(confirm_bar)
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "long")
        self.assertEqual(sig.entry_price, confirm_bar.close)
        self.assertEqual(self.sm.phase, SessionPhase.TRADE_OPEN)

    def test_single_bar_wick_and_reclaim_fires_immediately(self):
        # A single candle that wicks below and closes back inside must not wait for the next bar
        sig = self.sm.on_bar(_bar(31, 100.0, 100.3, 99.8, 100.2))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "long")
        self.assertEqual(sig.entry_price, 100.2)

    def test_price_moving_through_without_a_wick_still_counts_as_breakout(self):
        # Body fully below the ORB low (not just a wick) also qualifies as a sweep
        sig = self.sm.on_bar(_bar(31, 99.9, 99.95, 99.7, 99.8))
        self.assertIsNone(sig)
        self.assertEqual(self.sm.phase, SessionPhase.WAITING_FOR_CONFIRM)

        sig = self.sm.on_bar(_bar(32, 99.8, 100.4, 99.8, 100.3))
        self.assertIsNotNone(sig)
        self.assertEqual(sig.direction, "long")


if __name__ == "__main__":
    unittest.main()
