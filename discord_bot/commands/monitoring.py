"""/ping /status /health — pure monitoring, zero mutating calls."""
from __future__ import annotations

import time

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from discord_bot.services import health_service, monitoring_service
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import pct, usdt


class Monitoring(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Show Discord/exchange/API latency and bot response time.")
    async def ping(self, interaction: discord.Interaction) -> None:
        logger.info(f"/ping invoked by {interaction.user}")
        start = time.monotonic()
        await interaction.response.defer(thinking=True)
        response_ms = (time.monotonic() - start) * 1000

        report = await health_service.build_health_report()
        exchange_str = (
            f"{report.exchange_latency_ms:.0f} ms" if report.exchange_ok and report.exchange_latency_ms is not None
            else "unreachable"
        )

        embed = base_embed("Latency", Status.INFO)
        embed.add_field(name="Discord Gateway", value=f"{round(self.bot.latency * 1000)} ms", inline=True)
        embed.add_field(name="Exchange API", value=exchange_str, inline=True)
        embed.add_field(name="Bot Response", value=f"{response_ms:.0f} ms", inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="status", description="Show trading engine status, mode, session, and positions.")
    async def status(self, interaction: discord.Interaction) -> None:
        logger.info(f"/status invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        snap = await monitoring_service.get_status_snapshot()

        if snap.trading_halted:
            status = Status.WARNING
        elif not snap.engine_running:
            status = Status.NEUTRAL
        else:
            status = Status.OK

        embed = base_embed("Engine Status", status)
        embed.add_field(name="Engine", value="Running" if snap.engine_running else "Stopped", inline=True)
        embed.add_field(name="Mode", value=f"{snap.trading_mode.upper()}{' (DRY RUN)' if snap.dry_run else ''}", inline=True)
        embed.add_field(name="Symbol", value=snap.symbol, inline=True)
        embed.add_field(
            name="Trading",
            value=f"HALTED ({snap.halted_reason})" if snap.trading_halted else "Active",
            inline=True,
        )
        embed.add_field(name="Session", value=snap.active_session or "Between sessions", inline=True)
        embed.add_field(name="Phase", value=snap.phase or "—", inline=True)
        embed.add_field(
            name="Position",
            value="Open" if snap.has_open_position else ("Pending Entry" if snap.has_pending_entry else "Flat"),
            inline=True,
        )
        embed.add_field(name="Daily P&L", value=pct(snap.daily_pnl_pct, already_pct=False), inline=True)
        embed.add_field(name="Weekly P&L", value=pct(snap.weekly_pnl_pct, already_pct=False), inline=True)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="health", description="Show system diagnostics: CPU, RAM, disk, DB, exchange, version.")
    async def health(self, interaction: discord.Interaction) -> None:
        logger.info(f"/health invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        report = await health_service.build_health_report()

        if report.last_exception or not report.db_ok or not report.exchange_ok:
            status = Status.CRITICAL if (not report.db_ok or not report.exchange_ok) else Status.WARNING
        else:
            status = Status.OK

        embed = base_embed("System Health", status)
        embed.add_field(name="CPU", value=f"{report.cpu_pct:.1f}%", inline=True)
        embed.add_field(name="RAM", value=f"{report.ram_pct:.1f}%", inline=True)
        embed.add_field(name="Disk", value=f"{report.disk_pct:.1f}%", inline=True)
        embed.add_field(name="Database", value="OK" if report.db_ok else "UNREACHABLE", inline=True)
        embed.add_field(
            name="Exchange API",
            value=f"OK ({report.exchange_latency_ms:.0f} ms)" if report.exchange_ok and report.exchange_latency_ms else "UNREACHABLE",
            inline=True,
        )
        embed.add_field(name="Engine Process", value="Running" if report.engine_running else "Stopped", inline=True)
        embed.add_field(name="Git Commit", value=f"`{report.git_commit}`" if report.git_commit else "unknown", inline=True)
        embed.add_field(
            name="Last Exception",
            value=f"{report.last_exception}\n({report.last_exception_at})" if report.last_exception else "None recorded",
            inline=False,
        )
        await interaction.followup.send(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Monitoring(bot))
