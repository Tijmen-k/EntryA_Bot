"""
Permission gating for admin-tier commands.

Every admin command gets BOTH:
  - `app_commands.default_permissions(administrator=True)` — a sane default in
    Discord's own command-permissions UI.
  - `@administrator_required` — a runtime check inside the command body, since
    a server owner can reassign the UI default per-role/per-user; the runtime
    check is the actual enforcement, not the UI default.
"""
from __future__ import annotations

import functools
from typing import Callable, TypeVar

import discord

T = TypeVar("T")


def is_administrator(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return False  # admin commands are meaningless/unsafe in DMs
    perms = interaction.user.guild_permissions if isinstance(interaction.user, discord.Member) else None
    return bool(perms and perms.administrator)


def administrator_required(func: Callable[..., T]) -> Callable[..., T]:
    """Decorator for cog command methods: `self, interaction, ...`."""

    @functools.wraps(func)
    async def wrapper(self, interaction: discord.Interaction, *args, **kwargs):
        if not is_administrator(interaction):
            from discord_bot.utils.embeds import base_embed, Status
            embed = base_embed(
                "Permission Denied",
                Status.CRITICAL,
                "This command requires Administrator permission.",
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        return await func(self, interaction, *args, **kwargs)

    return wrapper
