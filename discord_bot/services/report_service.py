"""
End-of-day PDF report for /dailyreport.

Built with reportlab (pure Python, no system deps like wkhtmltopdf/cairo —
unlike weasyprint, it needs nothing beyond what pip already installs).
"""
from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from discord_bot.charts.equity import render_equity_curve
from src.journal import db as journal_db


@dataclass
class DailyReportSummary:
    total_trades: int
    total_pnl: float
    win_rate: float


def _today_closed_trades() -> list[dict]:
    today = datetime.now(timezone.utc).date().isoformat()
    return [
        t for t in journal_db.get_closed_trades()
        if t["exit_time"] and t["exit_time"][:10] == today
    ]


def _avg_hold_minutes(trades: list[dict]) -> Optional[float]:
    durations = []
    for t in trades:
        if not (t["entry_time"] and t["exit_time"]):
            continue
        try:
            start = datetime.fromisoformat(t["entry_time"])
            end = datetime.fromisoformat(t["exit_time"])
            durations.append((end - start).total_seconds() / 60)
        except ValueError:
            continue
    return sum(durations) / len(durations) if durations else None


def _build_pdf(trades: list[dict]) -> io.BytesIO:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, topMargin=1.5 * cm, bottomMargin=1.5 * cm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("ReportTitle", parent=styles["Title"], textColor=colors.HexColor("#0B0E14"))
    heading_style = ParagraphStyle("ReportHeading", parent=styles["Heading2"], textColor=colors.HexColor("#12161F"))

    pnls = [t["pnl_usdt"] or 0.0 for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    win_rate = len(wins) / len(trades) * 100 if trades else 0.0
    best = max(pnls) if pnls else 0.0
    worst = min(pnls) if pnls else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    avg_hold = _avg_hold_minutes(trades)

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    elements = [
        Paragraph(f"EntryA — Daily Report — {today_str}", title_style),
        Spacer(1, 0.4 * cm),
        Paragraph("Summary", heading_style),
    ]

    summary_rows = [
        ["Total Trades", str(len(trades))],
        ["Net Return", f"${total_pnl:+,.2f}"],
        ["Win Rate", f"{win_rate:.1f}%"],
        ["Wins / Losses", f"{len(wins)} / {len(losses)}"],
        ["Best Trade", f"${best:+,.2f}"],
        ["Worst Trade", f"${worst:+,.2f}"],
        ["Largest Drawdown", f"${max_dd:,.2f}"],
        ["Average Hold Time", f"{avg_hold:.0f} min" if avg_hold is not None else "—"],
    ]
    table = Table(summary_rows, colWidths=[6 * cm, 6 * cm])
    table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor("#12161F")),
        ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.HexColor("#CCCCCC")),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    elements.append(table)
    elements.append(Spacer(1, 0.6 * cm))

    if pnls:
        chart_bytes = render_equity_curve(pnls)
        elements.append(Paragraph("Equity Curve", heading_style))
        elements.append(Image(io.BytesIO(chart_bytes), width=16 * cm, height=7.1 * cm))
        elements.append(Spacer(1, 0.6 * cm))

    elements.append(Paragraph("Trades", heading_style))
    trade_rows = [["Time", "Session", "Side", "Entry", "Exit", "P&L"]]
    for t in trades:
        trade_rows.append([
            (t["exit_time"] or "")[11:19],
            t["session"] or "-",
            (t["side"] or "-").upper(),
            f"{t['entry_price']:,.2f}" if t["entry_price"] else "-",
            f"{t['exit_price']:,.2f}" if t["exit_price"] else "-",
            f"{(t['pnl_usdt'] or 0):+,.2f}",
        ])
    trades_table = Table(trade_rows, colWidths=[2.5 * cm, 2.5 * cm, 2 * cm, 3 * cm, 3 * cm, 3 * cm])
    trades_table.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#12161F")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("TEXTCOLOR", (0, 1), (-1, -1), colors.HexColor("#12161F")),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    elements.append(trades_table)

    doc.build(elements)
    buf.seek(0)
    return buf


async def build_daily_report() -> tuple[Optional[io.BytesIO], Optional[DailyReportSummary]]:
    trades = await asyncio.to_thread(_today_closed_trades)
    if not trades:
        return None, None

    pdf_buf = await asyncio.to_thread(_build_pdf, trades)
    total_pnl = sum(t["pnl_usdt"] or 0.0 for t in trades)
    wins = sum(1 for t in trades if (t["pnl_usdt"] or 0) > 0)
    summary = DailyReportSummary(
        total_trades=len(trades),
        total_pnl=total_pnl,
        win_rate=wins / len(trades) * 100,
    )
    return pdf_buf, summary
