"""
resolve_bias() backs the manual bias-override control on the dashboard: an
override set from app.py must always win over the auto-computed session bias,
and clearing it (None) must fall back to whatever was computed naturally.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import resolve_bias


class ResolveBiasTest(unittest.TestCase):
    def test_override_wins_over_computed(self):
        self.assertTrue(resolve_bias(computed=False, override=True))
        self.assertFalse(resolve_bias(computed=True, override=False))

    def test_no_override_falls_back_to_computed(self):
        self.assertTrue(resolve_bias(computed=True, override=None))
        self.assertFalse(resolve_bias(computed=False, override=None))

    def test_neither_set_is_none(self):
        self.assertIsNone(resolve_bias(computed=None, override=None))


if __name__ == "__main__":
    unittest.main()
