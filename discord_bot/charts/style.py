"""
Shared dark, publication-quality matplotlib style.

Sets the Agg backend once, at import time, before any Figure is created —
required since chart renders are dispatched onto worker threads via
asyncio.to_thread, and the interactive backends matplotlib defaults to are
not usable outside the main thread.
"""
from __future__ import annotations

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as _plt_module  # noqa: E402  (import after backend selection)

# Palette — reuses the same hex values as discord_bot/utils/embeds.py so charts
# and embeds read as one visual system.
BACKGROUND   = "#0B0E14"
PANEL        = "#12161F"
GRID         = "#232838"
TEXT         = "#D7DCE5"
TEXT_MUTED   = "#7A8296"
BULLISH      = "#2ECC71"
BEARISH      = "#E74C3C"
ENTRY_MARKER = "#3498DB"
EXIT_MARKER  = "#E67E22"
SL_LINE      = "#E74C3C"
TP_LINE      = "#2ECC71"

RC_PARAMS: dict = {
    "figure.facecolor":  BACKGROUND,
    "axes.facecolor":    PANEL,
    "axes.edgecolor":    GRID,
    "axes.labelcolor":   TEXT,
    "axes.grid":         True,
    "grid.color":        GRID,
    "grid.linewidth":    0.5,
    "text.color":        TEXT,
    "xtick.color":       TEXT_MUTED,
    "ytick.color":       TEXT_MUTED,
    "font.family":       "sans-serif",
    "font.size":         10,
    "legend.facecolor":  PANEL,
    "legend.edgecolor":  GRID,
    "savefig.facecolor": BACKGROUND,
}


def apply_style() -> None:
    _plt_module.rcParams.update(RC_PARAMS)


apply_style()
