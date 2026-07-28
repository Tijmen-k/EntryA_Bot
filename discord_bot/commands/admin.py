"""/killswitch /restart /risk /config — administrator-tier commands."""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.db as bot_db
from discord_bot.services import account_service, config_service, process_service, trading_control_service
from discord_bot.services.config_service import RiskEditError
from discord_bot.utils.confirm import ConfirmView
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import code_table, usdt
from discord_bot.utils.permissions import administrator_required


def _audit(interaction: discord.Interaction, command: str, params: str, result: str) -> None:
    bot_db.log_command(
        guild_id=str(interaction.guild_id) if interaction.guild_id else None,
        user_id=str(interaction.user.id),
        username=str(interaction.user),
        command=command,
        params=params,
        result=result,
    )


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="killswitch", description="Emergency shutdown: flatten all positions and halt trading. Administrator only.")
    @app_commands.default_permissions(administrator=True)
    @administrator_required
    async def killswitch(self, interaction: discord.Interaction) -> None:
        logger.warning(f"/killswitch invoked by {interaction.user}")
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=base_embed(
                "Confirm Killswitch", Status.CRITICAL,
                "This will immediately market-close ALL open positions, cancel ALL orders, "
                "and halt trading until `/resume` is issued. This action is not automatically reversible.",
            ),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            await view.interaction.followup.send(embed=base_embed("Cancelled", Status.INFO, "Killswitch cancelled."))
            return

        result = await trading_control_service.killswitch(username=str(interaction.user))
        _audit(interaction, "killswitch", "",
               f"closed={result.positions_closed} failed={result.positions_failed}")

        lines = [
            f"Positions closed: {result.positions_closed}",
            f"Positions failed to close: {result.positions_failed}",
            f"Orders cancelled: {'yes' if result.orders_cancelled else 'no'}",
            f"Pending entry cancelled: {'yes' if result.pending_entry_cancelled else 'n/a'}",
            "",
            "Trading is now HALTED. Use `/resume` to resume.",
        ]
        status = Status.CRITICAL if result.positions_failed else Status.WARNING
        await view.interaction.followup.send(embed=base_embed("Killswitch Activated", status, "\n".join(lines)))

    @app_commands.command(name="restart", description="Restart the trading engine process. Administrator only.")
    @app_commands.default_permissions(administrator=True)
    @administrator_required
    async def restart(self, interaction: discord.Interaction) -> None:
        logger.warning(f"/restart invoked by {interaction.user}")
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=base_embed("Confirm Restart", Status.WARNING, "This will stop and relaunch the trading engine process."),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            await view.interaction.followup.send(embed=base_embed("Cancelled", Status.INFO, "Restart cancelled."))
            return

        result = await process_service.restart_engine()
        _audit(interaction, "restart", "", f"started={result.started} pid={result.new_pid}")
        status = Status.OK if result.started else Status.CRITICAL
        await view.interaction.followup.send(embed=base_embed(
            "Engine Restarted" if result.started else "Restart Failed", status,
            f"Was running: {result.was_running}\nNew PID: {result.new_pid or 'none'}",
        ))

    risk_group = app_commands.Group(name="risk", description="View or edit risk configuration.", default_permissions=discord.Permissions(administrator=True))

    @risk_group.command(name="view", description="Show current risk configuration.")
    @administrator_required
    async def risk_view(self, interaction: discord.Interaction) -> None:
        logger.info(f"/risk view invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        balance = await account_service.get_balance()
        snap = await config_service.get_risk_snapshot(balance.balance_usdt)

        embed = base_embed("Risk Configuration", Status.INFO)
        embed.add_field(name="Risk per Trade", value=f"{snap.risk_per_trade_pct*100:.2f}%", inline=True)
        embed.add_field(name="Leverage", value=f"{snap.leverage:.0f}x", inline=True)
        embed.add_field(name="Ladder Scale Factor", value=f"{snap.ladder_scale_factor:.4f}", inline=True)
        embed.add_field(name="London SL", value=f"{snap.london_sl_pct*100:.2f}%", inline=True)
        embed.add_field(name="NY SL", value=f"{snap.ny_sl_pct*100:.2f}%", inline=True)
        embed.add_field(name="Ladder Level", value=str(snap.ladder_level), inline=True)
        embed.add_field(name="Default Size", value=usdt(snap.ladder_default_usdt), inline=True)
        embed.add_field(name="Boosted Size", value=usdt(snap.ladder_boosted_usdt), inline=True)
        await interaction.followup.send(embed=embed)

    @risk_group.command(name="edit", description="Edit a risk parameter (writes .env; requires an engine restart to take effect).")
    @app_commands.describe(field=f"One of: {', '.join(config_service.RISK_FIELD_NAMES)}", value="New value")
    @administrator_required
    async def risk_edit(self, interaction: discord.Interaction, field: str, value: str) -> None:
        logger.warning(f"/risk edit {field}={value} invoked by {interaction.user}")
        try:
            env_key, _, formatted = config_service.validate_risk_edit(field, value)
        except RiskEditError as exc:
            await interaction.response.send_message(embed=base_embed("Invalid Value", Status.WARNING, str(exc)))
            return

        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=base_embed(
                "Confirm Risk Edit", Status.WARNING,
                f"Set **{env_key}** to **{formatted}**?\n\n"
                "This writes `.env` only — the running trading engine will not pick this up "
                "until it is restarted (`/restart`).",
            ),
            view=view,
        )
        await view.wait()
        if not view.confirmed:
            await view.interaction.followup.send(embed=base_embed("Cancelled", Status.INFO, "Edit cancelled."))
            return

        env_key, formatted = await config_service.write_risk_field(field, value)
        _audit(interaction, "risk edit", f"{field}={value}", "success")
        await view.interaction.followup.send(embed=base_embed(
            "Risk Parameter Updated", Status.OK,
            f"**{env_key}** set to **{formatted}** in `.env`. Run `/restart` for it to take effect.",
        ))

    @app_commands.command(name="config", description="Show read-only strategy configuration. Administrator only.")
    @app_commands.default_permissions(administrator=True)
    @administrator_required
    async def config(self, interaction: discord.Interaction) -> None:
        logger.info(f"/config invoked by {interaction.user}")
        public_config = config_service.get_public_config()
        rows = [[key, value] for key, value in public_config.items()]
        table = code_table(["Setting", "Value"], rows)
        await interaction.response.send_message(embed=base_embed("Strategy Configuration", Status.INFO, table))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
