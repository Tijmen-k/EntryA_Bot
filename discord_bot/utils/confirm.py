"""Generic Confirm/Cancel dialog reused by every mutating command that needs one."""
from __future__ import annotations

from typing import Optional

import discord

import discord_bot.config as bot_config
from discord_bot.utils.embeds import base_embed, Status


class ConfirmView(discord.ui.View):
    """
    Usage:
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(embed=..., view=view)
        await view.wait()
        if view.confirmed:
            ...
    """

    def __init__(self, author_id: int, timeout: int = bot_config.CONFIRM_TIMEOUT_S) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.confirmed: Optional[bool] = None
        self.interaction: Optional[discord.Interaction] = None

    async def _finish(self, interaction: discord.Interaction, confirmed: bool) -> None:
        self.confirmed = confirmed
        self.interaction = interaction
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        # Disable buttons immediately, before the caller runs the actual action,
        # so a double-click can't fire the underlying action twice.
        await interaction.response.edit_message(view=self)
        self.stop()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                embed=base_embed("Not Your Confirmation", Status.WARNING,
                                  "Only the user who invoked this command can confirm it."),
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)
