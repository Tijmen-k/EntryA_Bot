"""
Discord webhook notifier for EntryA-Bot.

All methods are fire-and-forget: failures are logged but never raise,
so a Discord outage can never crash the trading loop.

Usage:
    from src.notifications.discord import DiscordNotifier
    notifier = DiscordNotifier(config.DISCORD_WEBHOOK_URL)
    notifier.trade_entry(signal, contracts, entry_id)
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import requests
from loguru import logger

import config

# ── Discord embed colour palette ───────────────────────────────────────────────
_GREEN  = 0x2ECC71   # profit / bullish / ok
_RED    = 0xE74C3C   # loss / bearish / error
_ORANGE = 0xE67E22   # warning / limit hit
_BLUE   = 0x3498DB   # informational / heartbeat
_GREY   = 0x95A5A6   # neutral / stopped


class DiscordNotifier:
    """Send structured embeds to a Discord webhook."""

    def __init__(self, webhook_url: str) -> None:
        self._url     = webhook_url.strip() if webhook_url else ""
        self._enabled = bool(self._url)
        if not self._enabled:
            logger.info("Discord notifier disabled (DISCORD_WEBHOOK_URL not set)")

    # ── Public alert methods ───────────────────────────────────────────────────

    def bot_started(self, dry_run: bool) -> None:
        mode = f"{config.TRADING_MODE.upper()}{'  |  DRY RUN' if dry_run else ''}"
        self._send({
            "color": _GREEN,
            "title": "Bot Started",
            "fields": [
                {"name": "Symbol",   "value": config.SYMBOL,        "inline": True},
                {"name": "Mode",     "value": mode,                 "inline": True},
                {"name": "Leverage", "value": f"{config.LEVERAGE}×", "inline": True},
            ],
        })

    def bot_stopped(self, reason: str = "Keyboard interrupt") -> None:
        self._send({
            "color": _GREY,
            "title": "Bot Stopped",
            "description": reason,
        })

    def bias_set(
        self,
        session: str,
        is_bearish: bool,
        price: float,
        ref_price: float,
    ) -> None:
        direction = "BEARISH" if is_bearish else "BULLISH"
        color     = _RED if is_bearish else _GREEN
        ref_label = "Yesterday Open" if session == "London" else "Price @ 09:00 UTC"
        self._send({
            "color": color,
            "title": f"🧭  {session} Bias Set — {direction}",
            "fields": [
                {"name": "Session Price",  "value": f"${price:,.2f}",     "inline": True},
                {"name": ref_label,        "value": f"${ref_price:,.2f}", "inline": True},
                {"name": "Direction",      "value": direction,            "inline": True},
            ],
        })

    def trade_entry(
        self,
        session: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        contracts: float,
        usdt_size: float,
        entry_id: str,
    ) -> None:
        is_long = direction == "long"
        color   = _GREEN if is_long else _RED
        arrow   = "📈" if is_long else "📉"
        sl_pct  = abs(entry_price - sl_price) / entry_price * 100
        tp_pct  = abs(tp_price - entry_price) / entry_price * 100
        rr      = tp_pct / sl_pct if sl_pct else 0
        return self._send({
            "color": color,
            "title": f"{arrow}  Trade Entry — {session} {direction.upper()}",
            "fields": [
                {"name": "Entry",     "value": f"${entry_price:,.2f}",   "inline": True},
                {"name": "Stop Loss", "value": f"${sl_price:,.2f}  ({sl_pct:.2f}%)", "inline": True},
                {"name": "Take Profit","value": f"${tp_price:,.2f}  ({tp_pct:.2f}%)", "inline": True},
                {"name": "Size",      "value": f"{contracts:.4f} BTC  (~${usdt_size:,.0f})", "inline": True},
                {"name": "R:R",       "value": f"1 : {rr:.2f}",          "inline": True},
                {"name": "Order ID",  "value": f"`{entry_id}`",          "inline": True},
            ],
        })

    def trade_closed(
        self,
        result: str,
        session: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        pnl_usdt: Optional[float],
        pnl_pct: float,
        daily_pnl_pct: float,
    ) -> None:
        if result == "tp_hit":
            color, emoji, label = _GREEN, "✅", "TP Hit"
        elif result == "sl_hit":
            color, emoji, label = _RED, "❌", "SL Hit 🔻"
        else:  # time_exit — session ended before either SL or TP was reached
            color = _GREEN if pnl_pct >= 0 else _RED
            emoji, label = "⏱️", "Session Time Exit"
        pnl_str = f"${pnl_usdt:+,.2f}" if pnl_usdt is not None else f"{pnl_pct*100:+.2f}%"
        return self._send({
            "color": color,
            "title": f"{emoji}  Trade Closed — {label}",
            "fields": [
                {"name": "Session",    "value": session,                      "inline": True},
                {"name": "Direction",  "value": direction.upper(),            "inline": True},
                {"name": "Entry → Exit","value": f"${entry_price:,.2f} → ${exit_price:,.2f}", "inline": True},
                {"name": "P&L",        "value": pnl_str,                      "inline": True},
                {"name": "Daily P&L",  "value": f"{daily_pnl_pct*100:+.2f}%", "inline": True},
            ],
        })

    def daily_limit_hit(self, daily_pnl_pct: float) -> None:
        self._send({
            "color": _ORANGE,
            "title": "Daily Loss Limit Hit — Trading Halted",
            "description": (
                f"Daily drawdown reached **{daily_pnl_pct*100:.2f}%** "
                f"(limit: {config.MAX_DAILY_LOSS_PCT*100:.1f}%).\n"
                "No further entries will be taken today."
            ),
        })

    def weekly_limit_hit(self, weekly_pnl_pct: float) -> None:
        self._send({
            "color": _RED,
            "title": "Weekly Loss Limit Hit — Trading Halted",
            "description": (
                f"Weekly drawdown reached **{weekly_pnl_pct*100:.2f}%** "
                f"(limit: {config.MAX_WEEKLY_LOSS_PCT*100:.1f}%).\n"
                "Bot will not trade until the new week."
            ),
        })

    def signal_missed(
        self,
        session: str,
        direction: str,
        entry_price: float,
        at_time: datetime,
    ) -> None:
        self._send({
            "color": _ORANGE,
            "title": f"⚠️  {session} Signal Missed — Bot Restart Gap",
            "description": (
                f"A **{direction.upper()}** signal would have fired at "
                f"**{at_time.strftime('%H:%M UTC')}** (entry ≈ ${entry_price:,.2f}) while the bot "
                "was offline. Not acted on since price has already moved on — "
                f"the **{session}** session is marked done for today."
            ),
        })

    def error(self, exc: Exception, context: str = "") -> None:
        desc = f"**{type(exc).__name__}**: `{exc}`"
        if context:
            desc = f"**Context**: {context}\n{desc}"
        self._send({
            "color": _RED,
            "title": "Unhandled Error",
            "description": desc,
        })

    def heartbeat(
        self,
        session: Optional[str],
        phase: str,
        daily_pnl_pct: float,
        weekly_pnl_pct: float,
        open_trade: bool,
        price: float,
    ) -> None:
        status = "📊 Trade Open" if open_trade else "💤 Flat"
        self._send({
            "color": _BLUE,
            "title": "Heartbeat",
            "fields": [
                {"name": "Price",       "value": f"${price:,.2f}",             "inline": True},
                {"name": "Session",     "value": session or "Between sessions", "inline": True},
                {"name": "Phase",       "value": phase,                        "inline": True},
                {"name": "Status",      "value": status,                       "inline": True},
                {"name": "Daily P&L",   "value": f"{daily_pnl_pct*100:+.2f}%", "inline": True},
                {"name": "Weekly P&L",  "value": f"{weekly_pnl_pct*100:+.2f}%","inline": True},
            ],
        })

    # ── Internal ───────────────────────────────────────────────────────────────

    def _send(self, embed: dict) -> bool:
        """Send embed to Discord. Returns True on success, False on any failure."""
        if not self._enabled:
            logger.debug("Discord notifier disabled — skipping send")
            return False
        try:
            now = datetime.now(timezone.utc)
            embed.setdefault("timestamp", now.isoformat())
            embed.setdefault("footer", {
                "text": f"EntryA-Bot  •  {config.SYMBOL}  •  {config.TRADING_MODE.upper()}"
            })
            resp = requests.post(
                self._url,
                json={"embeds": [embed]},
                timeout=10,
            )
            if resp.status_code not in (200, 204):
                logger.warning(
                    f"Discord webhook returned {resp.status_code}: {resp.text[:300]}"
                )
                return False
            return True
        except Exception as exc:
            logger.warning(f"Discord notification failed: {exc}")
            return False
