"""/balance /positions /position /orders — all read-only, backed by src/broker/bitget.py and src/risk/sizing.py."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.config as bot_config
from discord_bot.services import account_service
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import code_table, hhmm, pct, progress_bar, usdt


class Account(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="balance", description="Show account equity and current position-sizing ladder level.")
    async def balance(self, interaction: discord.Interaction) -> None:
        logger.info(f"/balance invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        snap = await account_service.get_balance()
        ladder = snap.ladder

        embed = base_embed("Account Balance", Status.OK)
        embed.add_field(name="Equity", value=usdt(snap.balance_usdt), inline=True)
        embed.add_field(name="Ladder Level", value=f"{ladder.level} / 17", inline=True)
        embed.add_field(name="Boost", value="Active" if ladder.boost_active else "Inactive", inline=True)
        embed.add_field(name="Default Size", value=usdt(ladder.default_usdt), inline=True)
        embed.add_field(name="Boosted Size", value=usdt(ladder.boosted_usdt), inline=True)
        embed.add_field(name="Active Size", value=usdt(ladder.active_size_usdt), inline=True)
        if ladder.next_level_equity is not None:
            embed.add_field(
                name="Progress to Next Level",
                value=f"`{progress_bar(ladder.progress_pct)}`\nNext at {usdt(ladder.next_level_equity)}",
                inline=False,
            )
        else:
            embed.add_field(name="Progress", value="Maximum ladder level reached", inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="positions", description="Show all open positions.")
    async def positions(self, interaction: discord.Interaction) -> None:
        logger.info(f"/positions invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        positions = await account_service.get_positions()

        if not positions:
            await interaction.followup.send(embed=base_embed("Open Positions", Status.INFO, "No open positions."))
            return

        rows = [
            [p.symbol, p.side.upper(), f"{p.size:.5f}", f"{p.entry_price:,.2f}", f"{p.unrealised_pnl:+,.2f}"]
            for p in positions
        ]
        table = code_table(["Symbol", "Side", "Size", "Entry", "uPnL"], rows)
        total_upnl = sum(p.unrealised_pnl for p in positions)
        status = Status.OK if total_upnl >= 0 else Status.CRITICAL
        embed = base_embed("Open Positions", status, table)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="position", description="Show detailed info for one open position.")
    @app_commands.describe(symbol="Position symbol, e.g. ETHUSDT")
    async def position(self, interaction: discord.Interaction, symbol: str) -> None:
        logger.info(f"/position {symbol} invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        detail = await account_service.get_position_detail(symbol)

        if not detail:
            await interaction.followup.send(
                embed=base_embed("Position Not Found", Status.WARNING, f"No open position for `{symbol}`.")
            )
            return

        p = detail.position
        status = Status.OK if p.unrealised_pnl >= 0 else Status.CRITICAL
        embed = base_embed(f"Position — {p.symbol}", status)
        embed.add_field(name="Side", value=p.side.upper(), inline=True)
        embed.add_field(name="Size", value=f"{p.size:.5f} {bot_config.BASE_CCY}", inline=True)
        embed.add_field(name="Entry Price", value=f"{p.entry_price:,.2f}", inline=True)
        embed.add_field(name="Mark Price", value=f"{detail.mark_price:,.2f}" if detail.mark_price else "—", inline=True)
        embed.add_field(name="Unrealized P&L", value=usdt(p.unrealised_pnl, signed=True), inline=True)
        embed.add_field(name="Unrealized %", value=pct(detail.unrealised_pnl_pct), inline=True)
        embed.add_field(
            name="Stop Loss",
            value=f"{detail.sl_price:,.2f}" if detail.sl_price else "None",
            inline=True,
        )
        embed.add_field(
            name="Take Profit",
            value=f"{detail.tp_price:,.2f}" if detail.tp_price else "None",
            inline=True,
        )
        embed.add_field(name="Session", value=detail.session or "—", inline=True)
        embed.add_field(name="Entry Time", value=hhmm(detail.entry_time), inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="orders", description="Show all pending (unfilled) orders.")
    async def orders(self, interaction: discord.Interaction) -> None:
        logger.info(f"/orders invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        orders = await account_service.get_orders()

        if not orders:
            await interaction.followup.send(embed=base_embed("Pending Orders", Status.INFO, "No pending orders."))
            return

        rows = [
            [o.order_id, o.side.upper(), o.order_type, f"{o.size:.5f}",
             f"{o.limit_price:,.2f}" if o.limit_price else "—", o.status]
            for o in orders
        ]
        table = code_table(["Order ID", "Side", "Type", "Size", "Price", "Status"], rows)
        await interaction.followup.send(embed=base_embed("Pending Orders", Status.INFO, table))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Account(bot))
