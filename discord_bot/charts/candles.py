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
    entry_price: Optional[float] = None,
    side: Optional[str] = None,
    pnl_usdt: Optional[float] = None,
) -> bytes:
    """Render a dark-theme candlestick chart with trade markers and SL/TP lines.

    When `entry_price`/`side` are given (an open position on this symbol), the region
    between entry and the current price is shaded green/red by profit direction and the
    live P&L (USDT) is annotated — mirroring the in-position view on the Streamlit dashboard.
    Returns PNG bytes.
    """
    trades = trades or []
    fig = Figure(figsize=(11, 7), dpi=150)
    canvas = FigureCanvasAgg(fig)
    ax_price = fig.add_axes((0.07, 0.10, 0.90, 0.84))

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

    for t in trades:
        entry_color = style.ENTRY_MARKER
        marker = "^" if t.side == "long" else "v"
        ax_price.scatter([mdates.date2num(t.entry_time)], [t.entry_price],
                          color=entry_color, marker=marker, s=90, zorder=5, label="_nolegend_")
        if t.exit_time and t.exit_price is not None:
            exit_color = style.BULLISH if (t.pnl_usdt or 0) >= 0 else style.BEARISH
            ax_price.scatter([mdates.date2num(t.exit_time)], [t.exit_price],
                              color=exit_color, marker="x", s=90, zorder=5, label="_nolegend_")

    if entry_price is not None and side is not None and current_price is not None:
        in_profit = (side == "long" and current_price > entry_price) or \
                    (side == "short" and current_price < entry_price)
        fill_color = style.BULLISH if in_profit else style.BEARISH
        ax_price.axhspan(
            min(entry_price, current_price), max(entry_price, current_price),
            facecolor=fill_color, alpha=0.12, zorder=1,
        )
        entry_color = style.BULLISH if side == "long" else style.BEARISH
        ax_price.axhline(entry_price, color=entry_color, linewidth=2,
                          label=f"{side.upper()} entry {entry_price:,.2f}")

        xs_last = xs[-1] if len(xs) else mdates.date2num(bars[-1].timestamp)
        pnl_label = f"${pnl_usdt:+,.2f}" if pnl_usdt is not None else ("+" if in_profit else "-")
        ax_price.annotate(
            pnl_label, xy=(xs_last, current_price), xytext=(8, 0),
            textcoords="offset points", va="center", fontsize=11, fontweight="bold",
            color=style.BULLISH if in_profit else style.BEARISH,
        )
    elif entry_price is not None and side is not None:
        entry_color = style.BULLISH if side == "long" else style.BEARISH
        ax_price.axhline(entry_price, color=entry_color, linewidth=2,
                          label=f"{side.upper()} entry {entry_price:,.2f}")

    if sl_price is not None:
        ax_price.axhline(sl_price, color=style.SL_LINE, linestyle="--", linewidth=1.2, label=f"SL {sl_price:,.2f}")
    if tp_price is not None:
        ax_price.axhline(tp_price, color=style.TP_LINE, linestyle="--", linewidth=1.2, label=f"TP {tp_price:,.2f}")
    if current_price is not None:
        ax_price.axhline(current_price, color=style.TEXT_MUTED, linestyle=":", linewidth=1.0,
                          label=f"Current {current_price:,.2f}")

    if sl_price is not None or tp_price is not None or current_price is not None or entry_price is not None:
        legend = ax_price.legend(loc="upper left", frameon=False, fontsize=8)
        for text in legend.get_texts():
            text.set_color(style.TEXT)

    ax_price.set_title(f"{symbol}  —  {resolution}", color=style.TEXT, fontsize=13, loc="left", pad=10)
    ax_price.set_ylabel("Price")
    ax_price.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for label in ax_price.get_xticklabels():
        label.set_rotation(30)
        label.set_ha("right")

    for spine in ax_price.spines.values():
        spine.set_color(style.GRID)

    buf = io.BytesIO()
    canvas.print_png(buf)
    buf.seek(0)
    return buf.read()
