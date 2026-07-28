"""Entrypoint: `python -m discord_bot`."""
from __future__ import annotations

import sys

from loguru import logger

import discord_bot.config as bot_config
from discord_bot.bot import create_bot


def main() -> None:
    if not bot_config.DISCORD_BOT_TOKEN:
        logger.error("DISCORD_BOT_TOKEN is not set — add it to .env before running the Discord bot.")
        sys.exit(1)

    bot = create_bot()
    bot.run(bot_config.DISCORD_BOT_TOKEN, log_handler=None)  # loguru already handles logging


if __name__ == "__main__":
    main()
