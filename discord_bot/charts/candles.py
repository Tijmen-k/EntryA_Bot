"""
Candlestick chart renderer for /chart and /dailyreport.

Uses matplotlib's object-oriented API exclusively (Figure + FigureCanvasAgg) —
never `pyplot.figure()`/`plt.plot()` — since pyplot's global state is not
thread-safe across the concurrent worker threads asyncio.to_thread dispatches
renders onto. Each call builds its own Figure/Axes with no shared state.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import matplotlib.dates as mdates
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Rectangle

from discord_bot.charts import style
from src.data.feed import Bar


@dataclass
class TradeMarker:
    entry_time: datetime
    entry_price: float
    exit_time: Optional[datetime]
    exit_price: Optional[float]
    side: str          # "long" | "short"
    pnl_usdt: Optional[float]


def render_candles(
    bars: list[Bar],
    symbol: str,
    resolution: str,
    trades: Optional[list[TradeMarker]] = None,
    current_price: Optional[float] = None,
    sl_price: Optional[float] = None,
    tp_price: Optional[float] = None,
) -> bytes:
    """Render a dark-theme candlestick chart with volume, trade markers, and SL/TP lines. Returns PNG bytes."""
    trades = trades or []
    fig = Figure(figsize=(11, 7), dpi=150)
    canvas = FigureCanvasAgg(fig)
    ax_price = fig.add_axes((0.07, 0.28, 0.90, 0.66))
    ax_vol   = fig.add_axes((0.07, 0.08, 0.90, 0.16), sharex=ax_price)

    xs = mdates.date2num([b.timestamp for b in bars])
    if len(xs) > 1:
        bar_width = (xs[1] - xs[0]) * 0.7
    else:
        bar_width = 0.0006

    for x, bar in zip(xs, bars):
        bullish = bar.close >= bar.open
        color = style.BULLISH if bullish else style.BEARISH
        ax_price.add_line(Line2D([x, x], [bar.low, bar.high], color=color, linewidth=0.8, zorder=2))
        body_low = min(bar.open, bar.close)
        body_h = max(abs(bar.close - bar.open), (bar.high - bar.low) * 0.002)
        ax_price.add_patch(Rectangle(
            (x - bar_width / 2, body_low), bar_width, body_h,
            facecolor=color, edgecolor=color, zorder=3,
        ))
        ax_vol.bar(x, bar.volume, width=bar_width, color=color, alpha=0.6)

    for t in trades:
        entry_color = style.ENTRY_MARKER
        marker = "^" if t.side == "long" else "v"
        ax_price.scatter([mdates.date2num(t.entry_time)], [t.entry_price],
                          color=entry_color, marker=marker, s=90, zorder=5, label="_nolegend_")
        if t.exit_time and t.exit_price is not None:
            exit_color = style.BULLISH if (t.pnl_usdt or 0) >= 0 else style.BEARISH
            ax_price.scatter([mdates.date2num(t.exit_time)], [t.exit_price],
                              color=exit_color, marker="x", s=90, zorder=5, label="_nolegend_")

    if sl_price is not None:
        ax_price.axhline(sl_price, color=style.SL_LINE, linestyle="--", linewidth=1.2, label=f"SL {sl_price:,.2f}")
    if tp_price is not None:
        ax_price.axhline(tp_price, color=style.TP_LINE, linestyle="--", linewidth=1.2, label=f"TP {tp_price:,.2f}")
    if current_price is not None:
        ax_price.axhline(current_price, color=style.TEXT_MUTED, linestyle=":", linewidth=1.0,
                          label=f"Current {current_price:,.2f}")

    if sl_price is not None or tp_price is not None or current_price is not None:
        legend = ax_price.legend(loc="upper left", frameon=False, fontsize=8)
        for text in legend.get_texts():
            text.set_color(style.TEXT)

    ax_price.set_title(f"{symbol}  —  {resolution}", color=style.TEXT, fontsize=13, loc="left", pad=10)
    ax_price.set_ylabel("Price")
    ax_vol.set_ylabel("Volume")
    ax_price.tick_params(labelbottom=False)
    ax_vol.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for label in ax_vol.get_xticklabels():
        label.set_rotation(30)
        label.set_ha("right")

    for ax in (ax_price, ax_vol):
        for spine in ax.spines.values():
            spine.set_color(style.GRID)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()
