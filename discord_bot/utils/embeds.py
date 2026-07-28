"""
Embed builder shared by every cog — enforces one consistent look across the
whole bot: dark/professional styling, no emojis or unicode icons, every embed
carries a title, a timestamp, and a footer with the bot version.

Color palette matches src/notifications/discord.py's existing webhook alerts
(same hex values) so the two Discord surfaces read as one system.
"""
from __future__ import annotations

from enum import Enum

import discord

import discord_bot.config as bot_config


class Status(Enum):
    """Semantic status used to pick an embed color — keeps color choice
    consistent instead of each cog picking a raw color constant ad hoc."""
    OK       = "ok"
    INFO     = "info"
    WARNING  = "warning"
    CRITICAL = "critical"
    NEUTRAL  = "neutral"


_COLOR_BY_STATUS: dict[Status, int] = {
    Status.OK:       0x2ECC71,  # green  — healthy / profit
    Status.INFO:     0x3498DB,  # blue   — informational
    Status.WARNING:  0xE67E22,  # amber  — warning
    Status.CRITICAL: 0xE74C3C,  # red    — critical / loss
    Status.NEUTRAL:  0x95A5A6,  # grey   — neutral / stopped
}


def color_for(status: Status) -> int:
    return _COLOR_BY_STATUS[status]


def color_for_pnl(value: float) -> int:
    """Green if >= 0, red if negative — for PnL-driven embeds."""
    return _COLOR_BY_STATUS[Status.OK] if value >= 0 else _COLOR_BY_STATUS[Status.CRITICAL]


def base_embed(
    title: str,
    status: Status = Status.INFO,
    description: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=title,
        description=description,
        color=color_for(status),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text=bot_config.FOOTER_TEXT)
    return embed


def error_embed(message: str) -> discord.Embed:
    return base_embed("Error", Status.CRITICAL, message)
