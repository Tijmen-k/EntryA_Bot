"""/logs /alerts /help — log tailing, alert subscription management, and command help."""
from __future__ import annotations

from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from discord_bot.services import alert_service, log_service
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

    alerts_group = app_commands.Group(name="alerts", description="Manage which events post to this channel.")

    @alerts_group.command(name="list", description="List active alert subscriptions for this server.")
    async def alerts_list(self, interaction: discord.Interaction) -> None:
        logger.info(f"/alerts list invoked by {interaction.user}")
        if not interaction.guild_id:
            await interaction.response.send_message(embed=base_embed("Alerts", Status.WARNING, "This command must be used in a server."))
            return
        subs = await alert_service.list_subscriptions(str(interaction.guild_id))
        if not subs:
            await interaction.response.send_message(embed=base_embed("Alert Subscriptions", Status.INFO, "No active subscriptions."))
            return
        lines = "\n".join(f"- {s['event_type']}  →  <#{s['channel_id']}>" for s in subs)
        await interaction.response.send_message(embed=base_embed("Alert Subscriptions", Status.INFO, lines))

    @alerts_group.command(name="subscribe", description="Subscribe this channel to an event type.")
    @app_commands.describe(event_type="Which event to receive alerts for")
    async def alerts_subscribe(
        self,
        interaction: discord.Interaction,
        event_type: Literal["trade_open", "trade_close", "killswitch", "ladder_boost", "error", "daily_report"],
    ) -> None:
        logger.info(f"/alerts subscribe {event_type} invoked by {interaction.user}")
        if not interaction.guild_id:
            await interaction.response.send_message(embed=base_embed("Alerts", Status.WARNING, "This command must be used in a server."))
            return
        await alert_service.subscribe(str(interaction.guild_id), str(interaction.channel_id), event_type)
        await interaction.response.send_message(
            embed=base_embed("Subscribed", Status.OK, f"This channel will now receive `{event_type}` alerts.")
        )

    @alerts_group.command(name="unsubscribe", description="Unsubscribe this channel from an event type.")
    @app_commands.describe(event_type="Which event to stop receiving alerts for")
    async def alerts_unsubscribe(
        self,
        interaction: discord.Interaction,
        event_type: Literal["trade_open", "trade_close", "killswitch", "ladder_boost", "error", "daily_report"],
    ) -> None:
        logger.info(f"/alerts unsubscribe {event_type} invoked by {interaction.user}")
        if not interaction.guild_id:
            await interaction.response.send_message(embed=base_embed("Alerts", Status.WARNING, "This command must be used in a server."))
            return
        await alert_service.unsubscribe(str(interaction.guild_id), str(interaction.channel_id), event_type)
        await interaction.response.send_message(
            embed=base_embed("Unsubscribed", Status.OK, f"This channel will no longer receive `{event_type}` alerts.")
        )

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
