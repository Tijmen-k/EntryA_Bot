"""/performance /history /dailyreport — all read-only, backed by src/journal/db.py."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.config as bot_config
from discord_bot.services import performance_service, report_service
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import code_table, usdt


class Performance(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="performance", description="Show trading performance statistics.")
    async def performance(self, interaction: discord.Interaction) -> None:
        logger.info(f"/performance invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        s = await performance_service.get_summary()

        embed = base_embed("Performance", color_for_pnl_status(s.total_pnl))
        embed.add_field(name="Total Trades", value=str(s.total_trades), inline=True)
        embed.add_field(name="Win Rate", value=f"{s.win_rate:.1f}%", inline=True)
        embed.add_field(name="Wins / Losses", value=f"{s.wins} / {s.losses}", inline=True)
        embed.add_field(name="Profit Factor", value=f"{s.profit_factor:.2f}", inline=True)
        embed.add_field(name="Expectancy", value=usdt(s.expectancy, signed=True), inline=True)
        embed.add_field(name="Max Drawdown", value=usdt(s.max_drawdown), inline=True)
        embed.add_field(name="Best Trade", value=usdt(s.best_trade, signed=True), inline=True)
        embed.add_field(name="Worst Trade", value=usdt(s.worst_trade, signed=True), inline=True)
        embed.add_field(name="Total Fees", value=usdt(s.total_fees), inline=True)
        embed.add_field(name="Daily P&L", value=usdt(s.daily_pnl_usdt, signed=True), inline=True)
        embed.add_field(name="Weekly P&L", value=usdt(s.weekly_pnl_usdt, signed=True), inline=True)
        embed.add_field(name="Total P&L", value=usdt(s.total_pnl, signed=True), inline=True)
        boost_str = f"Active (wins in last 6: {s.wins_in_last_6})" if s.boost_active else f"Inactive (wins in last 6: {s.wins_in_last_6})"
        embed.add_field(name="Boost Status", value=boost_str, inline=False)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="history", description="Show recent completed trades.")
    @app_commands.describe(count="Number of trades to show (default 10, max 25)")
    async def history(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 25] = 10) -> None:
        logger.info(f"/history {count} invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        trades = await performance_service.get_history(count)

        if not trades:
            await interaction.followup.send(embed=base_embed("Trade History", Status.INFO, "No completed trades yet."))
            return

        rows = [
            [
                (t["entry_time"] or "")[:10],
                t["session"] or "-",
                (t["side"] or "-").upper(),
                f"{t['entry_price']:,.2f}" if t["entry_price"] else "-",
                f"{t['exit_price']:,.2f}" if t["exit_price"] else "-",
                f"{t['pnl_usdt']:+,.2f}" if t["pnl_usdt"] is not None else "-",
                f"{t['pnl_pct']*100:+.2f}%" if t["pnl_pct"] is not None else "-",
            ]
            for t in trades
        ]
        table = code_table(["Date", "Session", "Side", "Entry", "Exit", "PnL$", "PnL%"], rows)
        total_pnl = sum(t["pnl_usdt"] or 0 for t in trades)
        embed = base_embed(f"Trade History (last {len(trades)})", color_for_pnl_status(total_pnl), table)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="dailyreport", description="Generate and export a full end-of-day PDF report.")
    async def dailyreport(self, interaction: discord.Interaction) -> None:
        logger.info(f"/dailyreport invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)

        pdf_bytes, summary = await report_service.build_daily_report()
        if pdf_bytes is None:
            await interaction.followup.send(
                embed=base_embed("Daily Report", Status.INFO, "No trades recorded today — nothing to report.")
            )
            return

        embed = base_embed("Daily Report", color_for_pnl_status(summary.total_pnl))
        embed.add_field(name="Trades Today", value=str(summary.total_trades), inline=True)
        embed.add_field(name="Net Return", value=usdt(summary.total_pnl, signed=True), inline=True)
        embed.add_field(name="Win Rate", value=f"{summary.win_rate:.1f}%", inline=True)

        file = discord.File(pdf_bytes, filename="daily_report.pdf")
        await interaction.followup.send(embed=embed, file=file)


def color_for_pnl_status(value: float) -> Status:
    return Status.OK if value >= 0 else Status.CRITICAL


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Performance(bot))
