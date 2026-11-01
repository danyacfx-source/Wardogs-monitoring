"""
WARDOGS Monitor Bot — точка входа.
Все настройки берутся из переменных окружения.
"""

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

from cogs.server_monitor import ServerMonitor

# ── Логирование ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("wardogs")


# ── Конфиг из env ────────────────────────────────────────────────────────────

def require_env(key: str) -> str:
    value = os.getenv(key)
    if not value:
        log.critical("Переменная окружения %s не задана!", key)
        sys.exit(1)
    return value


TOKEN             = require_env("BOT_TOKEN")
SERVER_NAME       = require_env("SERVER_NAME")
SERVER_HOST       = require_env("SERVER_HOST")
SERVER_PORT       = int(os.getenv("SERVER_PORT", "7776"))
SERVER_PASSWORD   = require_env("SERVER_PASSWORD")
STATUS_CHANNEL_ID = int(require_env("STATUS_CHANNEL_ID"))
ALERT_CHANNEL_ID  = int(os.getenv("ALERT_CHANNEL_ID", "0")) or None
POLL_INTERVAL     = int(os.getenv("POLL_INTERVAL", "30"))


# ── Конфигурация кога через env ──────────────────────────────────────────────

class EnvServerMonitor(ServerMonitor):
    SERVER_NAME       = globals()["SERVER_NAME"]
    SERVER_HOST       = globals()["SERVER_HOST"]
    SERVER_PORT       = globals()["SERVER_PORT"]
    SERVER_PASSWORD   = globals()["SERVER_PASSWORD"]
    STATUS_CHANNEL_ID = globals()["STATUS_CHANNEL_ID"]
    ALERT_CHANNEL_ID  = globals()["ALERT_CHANNEL_ID"]
    POLL_INTERVAL     = globals()["POLL_INTERVAL"]


# ── Бот ──────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix=commands.when_mentioned,
    intents=intents,
    help_command=None,
)


@bot.event
async def on_ready():
    log.info("Бот запущен как %s (ID: %s)", bot.user, bot.user.id)
    log.info("Мониторинг: %s @ %s:%s", SERVER_NAME, SERVER_HOST, SERVER_PORT)
    try:
        synced = await bot.tree.sync()
        log.info("Slash-команды синхронизированы: %d", len(synced))
    except Exception as e:
        log.error("Ошибка синхронизации команд: %s", e)


async def main():
    async with bot:
        await bot.add_cog(EnvServerMonitor(bot))
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
