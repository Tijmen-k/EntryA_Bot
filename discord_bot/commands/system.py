"""/logs /help — log tailing and command help."""
from __future__ import annotations

from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from discord_bot.services import log_service
from discord_bot.utils.embeds import base_embed, Status
from discord_bot.utils.formatting import truncate

_COG_LABELS = {
    "Monitoring":  "Monitoring",
    "Account":     "Account",
    "Performance": "Performance",
    "Trading":     "Trading Controls",
    "Admin":       "Administrator",
    "Charts":      "Charts",
    "System":      "System",
}


class System(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="logs", description="Show the latest log entries.")
    @app_commands.describe(category="Log category to show", keyword="Filter to lines containing this text", count="Number of lines (default 15, max 40)")
    async def logs(
        self,
        interaction: discord.Interaction,
        category: Literal["errors", "warnings", "trades", "system"] = "system",
        keyword: str | None = None,
        count: app_commands.Range[int, 1, 40] = 15,
    ) -> None:
        logger.info(f"/logs {category} invoked by {interaction.user}")
        await interaction.response.defer(thinking=True)
        lines = await log_service.tail_logs(category, keyword, count)

        if not lines:
            await interaction.followup.send(embed=base_embed(f"Logs — {category}", Status.INFO, "No matching log lines."))
            return

        body = truncate("\n".join(lines), 3800)
        status = Status.CRITICAL if category == "errors" else (Status.WARNING if category == "warnings" else Status.INFO)
        embed = base_embed(f"Logs — {category} (last {len(lines)})", status, f"```\n{body}\n```")
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="help", description="Show all available commands.")
    async def help(self, interaction: discord.Interaction) -> None:
        logger.info(f"/help invoked by {interaction.user}")
        embed = base_embed("EntryA Control — Commands", Status.INFO)
        by_cog: dict[str, list[str]] = {}
        for cmd in self.bot.tree.walk_commands():
            cog_name = getattr(cmd, "binding", None)
            label = _COG_LABELS.get(type(cog_name).__name__, "Other") if cog_name else "Other"
            by_cog.setdefault(label, []).append(f"`/{cmd.qualified_name}` — {cmd.description}")

        for label, lines in sorted(by_cog.items()):
            embed.add_field(name=label, value="\n".join(sorted(lines)), inline=False)
        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(System(bot))
