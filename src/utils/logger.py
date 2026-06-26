"""
Logging setup using loguru with daily file rotation.
Call setup_logger() once at startup.
"""
import sys
from pathlib import Path
from loguru import logger


def setup_logger(log_dir: str = "logs", level: str = "INFO") -> None:
    Path(log_dir).mkdir(exist_ok=True)

    logger.remove()

    # Console: coloured, concise
    logger.add(
        sys.stdout,
        level=level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        colorize=True,
    )

    # File: full detail, daily rotation, 14-day retention
    logger.add(
        f"{log_dir}/bot_{{time:YYYY-MM-DD}}.log",
        level="DEBUG",
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{line} | {message}",
        rotation="00:00",
        retention="14 days",
        compression="gz",
    )

    # Separate trade log — one line per trade event, never compressed away
    logger.add(
        f"{log_dir}/trades.log",
        level="INFO",
        format="{time:YYYY-MM-DD HH:mm:ss} | {message}",
        filter=lambda r: "TRADE" in r["message"],
        rotation="1 week",
        retention="52 weeks",
    )
