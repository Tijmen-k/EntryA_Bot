"""Equity-curve line chart for /dailyreport. Same OO-API-only, thread-safe pattern as candles.py."""
from __future__ import annotations

import io

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure

from discord_bot.charts import style


def render_equity_curve(pnls: list[float]) -> bytes:
    """`pnls` is a list of realised P&L per trade, oldest first. Returns PNG bytes."""
    fig = Figure(figsize=(9, 4), dpi=150)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes((0.09, 0.14, 0.88, 0.78))

    equity = [0.0]
    for p in pnls:
        equity.append(equity[-1] + p)

    color = style.BULLISH if equity[-1] >= 0 else style.BEARISH
    ax.plot(range(len(equity)), equity, color=color, linewidth=1.6)
    ax.axhline(0, color=style.GRID, linewidth=0.8)
    ax.fill_between(range(len(equity)), equity, 0, color=color, alpha=0.12)

    ax.set_title("Equity Curve", color=style.TEXT, fontsize=12, loc="left")
    ax.set_xlabel("Trade #")
    ax.set_ylabel("Cumulative P&L (USDT)")
    for spine in ax.spines.values():
        spine.set_color(style.GRID)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()
