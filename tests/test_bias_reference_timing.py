"""
Regression test: London's bias reference price used to be hardcoded to look
for the bar at literal 09:00 UTC, while NY's already correctly followed its
own configured orb_start_h/m. Since LONDON_ORB_START_H is 7 in this project's
.env, that hardcoding meant London's bias was measured 1.5h after its own ORB
actually built — silently out of sync with the configured ORB start, and out
of sync with the simulator (which always followed orb_start_h). Both session
reference prices must now follow their own configured orb_start_h/m.
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
from src.strategy.entry_a import BiasCalculator

_LON = config.SESSIONS[0]
_NY = config.SESSIONS[1]


class FakeState:
    def __init__(self, **kwargs):
        self._data = dict(kwargs)

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self._data.get(key)

    def set(self, **kwargs):
        self._data.update(kwargs)


class FakeNotifier:
    def bias_set(self, *args, **kwargs):
        pass


def _bar(day, hour, minute, close):
    ts = datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=hour, minutes=minute)
    return Bar(timestamp=ts, open=close, high=close, low=close, close=close, volume=1.0)


class BiasReferenceTimingTest(unittest.TestCase):
    def setUp(self):
        self.bot = EntryABot.__new__(EntryABot)
        self.bot.bias = BiasCalculator()
        self.bot.notifier = FakeNotifier()
        self.day = datetime(2026, 7, 2).date()

    def test_london_bias_reads_orb_start_not_hardcoded_nine(self):
        # A bar at London's real configured orb_start_h (7 in this project's .env,
        # not 9) must be the one that gets captured and drives the bias calc.
        self.bot.state = FakeState(yesterday_open=100.0, prev_day_high=105.0, prev_day_low=95.0)
        bars = [_bar(self.day, _LON.orb_start_h, _LON.orb_start_m, 102.0)]

        self.bot._update_reference_prices(bars, datetime.combine(self.day, datetime.min.time(), tzinfo=timezone.utc))

        self.assertEqual(self.bot.state.price_at_09, 102.0)
        self.assertFalse(self.bot.state.london_bias_bearish)  # 102 > 100 -> bullish

    def test_no_bar_at_hardcoded_nine_no_longer_blocks_capture(self):
        # If LONDON_ORB_START_H != 9, a bar sitting at literal hour=9 (and none at
        # the real orb_start_h) must NOT be what the old hardcoded logic looked for.
        if _LON.orb_start_h == 9:
            self.skipTest("This project's LONDON_ORB_START_H is 9 — nothing to distinguish here")
        self.bot.state = FakeState(yesterday_open=100.0, prev_day_high=105.0, prev_day_low=95.0)
        bars = [_bar(self.day, 9, 0, 999.0)]  # only the OLD hardcoded hour, not the real one

        self.bot._update_reference_prices(bars, datetime.combine(self.day, datetime.min.time(), tzinfo=timezone.utc))

        self.assertIsNone(self.bot.state.price_at_09)  # must not have been captured from the wrong hour

    def test_ny_bias_reads_its_own_orb_start(self):
        self.bot.state = FakeState(
            yesterday_open=100.0, prev_day_high=105.0, prev_day_low=95.0, price_at_09=100.0,
        )
        bars = [_bar(self.day, _NY.orb_start_h, _NY.orb_start_m, 98.0)]

        self.bot._update_reference_prices(bars, datetime.combine(self.day, datetime.min.time(), tzinfo=timezone.utc))

        self.assertEqual(self.bot.state.price_at_14, 98.0)
        self.assertTrue(self.bot.state.ny_bias_bearish)  # 98 < 100 -> bearish


if __name__ == "__main__":
    unittest.main()
