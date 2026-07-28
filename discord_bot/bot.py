"""
Discord bot client — cog loader, command-tree sync, and a global error handler
so every command (and every error) is logged, matching the rest of the
codebase's loguru-everywhere convention.
"""
from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

import discord_bot.config as bot_config
import discord_bot.db as bot_db
from discord_bot.services import health_service
from discord_bot.utils.embeds import base_embed, Status

_COGS = (
    "discord_bot.commands.monitoring",
    "discord_bot.commands.account",
    "discord_bot.commands.performance",
    "discord_bot.commands.system",
    "discord_bot.commands.trading",
    "discord_bot.commands.admin",
    "discord_bot.commands.charts",
)


class EntryACommandTree(app_commands.CommandTree):
    """Global error handler + optional channel restriction for every slash command."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not bot_config.DISCORD_COMMAND_CHANNEL_ID:
            return True
        if str(interaction.channel_id) == bot_config.DISCORD_COMMAND_CHANNEL_ID:
            return True
        embed = base_embed(
            "Wrong Channel", Status.WARNING,
            "This bot only responds in its designated command channel.",
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return False

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        command_name = interaction.command.name if interaction.command else "unknown"
        logger.exception(f"/{command_name} raised: {error}")
        health_service.record_exception(error)

        embed = base_embed(
            "Command Failed", Status.CRITICAL,
            f"`/{command_name}` failed: {error}",
        )
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except discord.HTTPException:
            pass  # interaction already expired — nothing more we can do


class EntryABotClient(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix="!",  # unused — slash commands only, kept for library compatibility
            intents=intents,
            tree_cls=EntryACommandTree,
        )

    async def setup_hook(self) -> None:
        bot_db.init_db()
        for cog in _COGS:
            await self.load_extension(cog)
            logger.info(f"Loaded cog: {cog}")

        if bot_config.DISCORD_GUILD_ID:
            guild = discord.Object(id=int(bot_config.DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info(f"Synced commands to guild {bot_config.DISCORD_GUILD_ID}")
        else:
            await self.tree.sync()
            logger.info("Synced commands globally (may take up to an hour to propagate)")

    async def on_ready(self) -> None:
        logger.info(f"Discord bot ready — logged in as {self.user} (id={self.user.id if self.user else '?'})")
        health_service.record_ok()


def create_bot() -> EntryABotClient:
    return EntryABotClient()
