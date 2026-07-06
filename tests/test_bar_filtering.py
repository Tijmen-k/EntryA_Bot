"""
Regression test for the still-forming-candle bug: Bitget's candle endpoint
returns the currently-open candle as the last entry, which main._tick() used
to feed straight into the strategy state machine as if it were closed. That
made real sweep/reclaim moves invisible because the actual closed bar
(bars[-2]) was discarded every tick in favour of a near-empty forming bar.
"""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.data.feed import Bar
from main import drop_unclosed_bar


def _bar(minutes_ago: int, now: datetime) -> Bar:
    ts = now.replace(second=0, microsecond=0) - timedelta(minutes=minutes_ago)
    return Bar(timestamp=ts, open=1.0, high=1.0, low=1.0, close=1.0, volume=0.0)


class DropUnclosedBarTest(unittest.TestCase):
    def test_drops_still_forming_last_bar(self):
        now = datetime(2026, 7, 1, 8, 44, 34, tzinfo=timezone.utc)
        bars = [_bar(2, now), _bar(1, now), _bar(0, now)]  # last one just opened
        result = drop_unclosed_bar(bars, now)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1].timestamp, bars[-2].timestamp)

    def test_keeps_last_bar_once_fully_closed(self):
        now = datetime(2026, 7, 1, 8, 45, 5, tzinfo=timezone.utc)
        bars = [_bar(2, now), _bar(1, now)]  # open at 08:44:00, closed by 08:45:00
        result = drop_unclosed_bar(bars, now)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[-1].timestamp, bars[-1].timestamp)

    def test_empty_input(self):
        self.assertEqual(drop_unclosed_bar([], datetime.now(timezone.utc)), [])

    def test_all_bars_dropped_leaves_empty_list(self):
        now = datetime(2026, 7, 1, 8, 44, 34, tzinfo=timezone.utc)
        bars = [_bar(0, now)]
        self.assertEqual(drop_unclosed_bar(bars, now), [])


if __name__ == "__main__":
    unittest.main()
