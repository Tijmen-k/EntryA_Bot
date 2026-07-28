"""/pause /resume /close /closeall — trading-tier controls (no admin gate; close/closeall require confirmation)."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.db as bot_db
from discord_bot.services import account_service, trading_control_service
from discord_bot.utils.confirm import ConfirmView
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import usdt


def _audit(interaction: discord.Interaction, command: str, params: str, result: str) -> None:
    bot_db.log_command(
        guild_id=str(interaction.guild_id) if interaction.guild_id else None,
        user_id=str(interaction.user.id),
        username=str(interaction.user),
        command=command,
        params=params,
        result=result,
    )


class Trading(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="pause", description="Pause opening new positions. Existing positions keep being managed.")
    async def pause(self, interaction: discord.Interaction) -> None:
        logger.info(f"/pause invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        await trading_control_service.set_halted(True, reason="manual_pause", username=str(interaction.user))
        _audit(interaction, "pause", "", "success")
        await interaction.followup.send(embed=base_embed(
            "Trading Paused", Status.WARNING,
            "No new positions will be opened. Any existing position keeps being monitored normally.\n"
            "Use `/resume` to resume.",
        ))

    @app_commands.command(name="resume", description="Resume opening new positions (clears pause or killswitch).")
    async def resume(self, interaction: discord.Interaction) -> None:
        logger.info(f"/resume invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        await trading_control_service.set_halted(False)
        _audit(interaction, "resume", "", "success")
        await interaction.followup.send(embed=base_embed(
            "Trading Resumed", Status.OK, "New signals will be acted on again from the engine's next tick.",
        ))

    @app_commands.command(name="close", description="Market-close a position. Requires confirmation.")
    @app_commands.describe(symbol="Symbol to close, e.g. ETHUSDT")
    async def close(self, interaction: discord.Interaction, symbol: str) -> None:
        logger.info(f"/close {symbol} invoked by {interaction.user}")
        detail = await account_service.get_position_detail(symbol)
        if not detail:
            await interaction.response.send_message(
                embed=base_embed("Close Position", Status.WARNING, f"No open position for `{symbol}`.")
            )
            return

        p = detail.position
        view = ConfirmView(author_id=interaction.user.id)
        confirm_embed = base_embed(
            "Confirm Close", Status.WARNING,
            f"Market-close **{p.side.upper()} {p.size:.5f} {symbol}** "
            f"(entry {p.entry_price:,.2f}, uPnL {usdt(p.unrealised_pnl, signed=True)})?",
        )
        await interaction.response.send_message(embed=confirm_embed, view=view)
        await view.wait()
        if not view.confirmed:
            await view.interaction.followup.send(embed=base_embed("Cancelled", Status.INFO, "Close cancelled."))
            return

        result = await trading_control_service.close_symbol(symbol)
        _audit(interaction, "close", symbol, "success" if result.success else f"error: {result.message}")
        status = Status.OK if result.success else Status.CRITICAL
        pnl_line = f"\nRealised P&L: {usdt(result.pnl_usdt, signed=True)}" if result.pnl_usdt is not None else ""
        await view.interaction.followup.send(embed=base_embed(
            "Position Closed" if result.success else "Close Failed", status, result.message + pnl_line,
        ))

    @app_commands.command(name="closeall", description="Market-close every open position. Requires confirmation.")
    async def closeall(self, interaction: discord.Interaction) -> None:
        logger.info(f"/closeall invoked by {interaction.user}")
        positions = await account_service.get_positions()
        if not positions:
            await interaction.response.send_message(embed=base_embed("Close All", Status.INFO, "No open positions."))
            return

        view = ConfirmView(author_id=interaction.user.id)
        confirm_embed = base_embed(
            "Confirm Close All", Status.WARNING,
            f"Market-close all **{len(positions)}** open position(s)?",
        )
        await interaction.response.send_message(embed=confirm_embed, view=view)
        await view.wait()
        if not view.confirmed:
            await view.interaction.followup.send(embed=base_embed("Cancelled", Status.INFO, "Close-all cancelled."))
            return

        results = await trading_control_service.close_all()
        succeeded = sum(1 for r in results if r.success)
        _audit(interaction, "closeall", "", f"{succeeded}/{len(results)} closed")
        lines = "\n".join(
            f"- {r.symbol} {r.side.upper()}: {'Closed' if r.success else f'Failed ({r.message})'}"
            for r in results
        )
        status = Status.OK if succeeded == len(results) else Status.WARNING
        await view.interaction.followup.send(embed=base_embed(
            f"Closed {succeeded}/{len(results)} Positions", status, lines,
        ))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Trading(bot))
