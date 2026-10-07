"""Console and rotating local logs, with application-only debug output."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class RedactingFormatter(logging.Formatter):
    def __init__(self, token):
        super().__init__("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        self.token = token

    def format(self, record):
        output = super().format(record)
        return output.replace(self.token, "[REDACTED]") if self.token else output


def configure_logging():
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    if level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL。")
    directory = Path("data/logs")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    path = directory / "amadeus.log"
    handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    path.chmod(0o600)
    formatter = RedactingFormatter(os.getenv("DISCORD_TOKEN", ""))
    console = logging.StreamHandler()
    for target in (handler, console):
        target.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=[console, handler], force=True)
    logging.getLogger("amadeus").setLevel(level)
    # Protocol DEBUG dumps can include session secrets. Keep library logs at INFO or above.
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.ext.voice_recv").setLevel(logging.WARNING)
    logging.getLogger("mlx_audio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return path
