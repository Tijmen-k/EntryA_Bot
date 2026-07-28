"""/chart <symbol> — professional dark-theme candlestick chart, exported as PNG."""
from __future__ import annotations

import asyncio
import io
from datetime import datetime, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.config as bot_config
from discord_bot.charts.candles import render_candles, TradeMarker
from discord_bot.services import account_service, feed_service
from discord_bot.utils.embeds import base_embed, Status
from src.journal import db as journal_db


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class Charts(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="chart", description="Generate a candlestick chart with entries, exits, SL, and TP.")
    @app_commands.describe(
        symbol="Symbol, e.g. ETHUSDT",
        resolution="Candle timeframe",
        bars="Number of candles (default 150, max 500)",
    )
    async def chart(
        self,
        interaction: discord.Interaction,
        symbol: str = bot_config.SYMBOL,
        resolution: Literal["1m", "5m", "15m", "1h", "4h", "1d"] = "15m",
        bars: app_commands.Range[int, 20, 500] = 150,
    ) -> None:
        logger.info(f"/chart {symbol} {resolution} {bars} invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)

        candles = await feed_service.get_ohlcv_cached(symbol, resolution, bars)
        if not candles:
            await interaction.followup.send(embed=base_embed("Chart", Status.WARNING, f"No candle data for `{symbol}`."))
            return

        window_start = candles[0].timestamp

        all_trades = await asyncio.to_thread(journal_db.get_all_trades)
        markers = [
            TradeMarker(
                entry_time=_parse_iso(t["entry_time"]),
                entry_price=t["entry_price"],
                exit_time=_parse_iso(t["exit_time"]),
                exit_price=t["exit_price"],
                side=t["side"],
                pnl_usdt=t["pnl_usdt"],
            )
            for t in all_trades
            if t["symbol"].upper() == symbol.upper()
            and t["entry_price"]
            and _parse_iso(t["entry_time"])
            and _parse_iso(t["entry_time"]) >= window_start
        ]

        current_price = await feed_service.get_current_price_cached(symbol)
        detail = await account_service.get_position_detail(symbol)
        sl_price = detail.attached_sl.stop_price if detail and detail.attached_sl else None
        tp_price = detail.attached_tp.limit_price if detail and detail.attached_tp else None

        png_bytes = render_candles(
            candles, symbol, resolution,
            trades=markers, current_price=current_price, sl_price=sl_price, tp_price=tp_price,
        )

        file = discord.File(io.BytesIO(png_bytes), filename=f"{symbol}_{resolution}.png")
        embed = base_embed(f"{symbol} — {resolution}", Status.INFO)
        if current_price:
            embed.add_field(name="Current Price", value=f"{current_price:,.2f}", inline=True)
        embed.set_image(url=f"attachment://{symbol}_{resolution}.png")
        await interaction.followup.send(embed=embed, file=file)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Charts(bot))
