"""
WARDOGS Server Monitor Cog
Мониторинг одного WARDOGS-сервера через HTTP RCON API (порт 7776).
Один экземпляр бота = один сервер.
"""

import asyncio
import aiohttp
import discord
from discord.ext import commands, tasks
from discord import app_commands
from datetime import datetime, timezone
from typing import Optional
import logging

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  Клиент API
# ─────────────────────────────────────────────

class WDRconClient:
    TIMEOUT = aiohttp.ClientTimeout(total=8)

    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, password: str):
        self._session = session
        self._base    = f"http://{host}:{port}"
        self._headers = {"Authorization": f"Bearer {password}"}

    async def _get(self, path: str) -> Optional[dict]:
        try:
            async with self._session.get(
                f"{self._base}{path}", headers=self._headers, timeout=self.TIMEOUT
            ) as r:
                if r.status == 200:
                    return await r.json()
                log.warning("GET %s → HTTP %s", path, r.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.debug("GET %s failed: %s", path, e)
        return None

    async def status(self) -> Optional[dict]:
        return await self._get("/v1/status")

    async def players(self) -> Optional[list]:
        data = await self._get("/v1/players")
        return data.get("players") if data else None

    async def full_info(self) -> Optional[dict]:
        """Статус + игроки. None = сервер недоступен."""
        status = await self.status()
        if status is None:
            return None
        players = await self.players() or []
        return {**status, "playerList": players}


# ─────────────────────────────────────────────
#  Карты WARDOGS
# ─────────────────────────────────────────────

MAP_INFO: dict[str, dict] = {
    "Bakurani": {
        "emoji": "🏭",
        "region": "Кавказ",
        "mode": "King of the Hill",
        "desc": "Речная долина с заводским районом и лесистыми хребтами по бокам.",
    },
    "Ozeti": {
        "emoji": "⛪",
        "region": "Европа",
        "mode": "King of the Hill",
        "desc": "Холмистый городок с футбольным стадионом, церковью на возвышенности и рекой на юге.",
    },
    "Zestafona": {
        "emoji": "🌾",
        "region": "Северная Америка",
        "mode": "King of the Hill",
        "desc": "Открытая местность: городок у изгиба реки, фермерские угодья и складские районы.",
    },
    "Jutari": {
        "emoji": "🎯",
        "region": "Кавказ (окраины Бакурани)",
        "mode": "Sniper Showdown",
        "desc": "Компактная арена для снайперских дуэлей.",
    },
    "Saburtulo": {
        "emoji": "🔒",
        "region": "Россия",
        "mode": "King of the Hill",
        "desc": "В разработке — карта ещё недоступна для игры.",
    },
}

def get_map_info(map_id: str) -> dict:
    """Ищет карту по точному совпадению или по вхождению подстроки."""
    if map_id in MAP_INFO:
        return MAP_INFO[map_id]
    map_id_lower = map_id.lower()
    for key, info in MAP_INFO.items():
        if key.lower() in map_id_lower or map_id_lower in key.lower():
            return info
    return {"emoji": "🗺️", "region": "—", "mode": "—", "desc": ""}


# ─────────────────────────────────────────────
#  Embed-строители
# ─────────────────────────────────────────────

COLOR_ONLINE  = discord.Color.green()
COLOR_OFFLINE = discord.Color.red()
WARDOGS_COLOR = discord.Color.from_rgb(200, 40, 40)


def _faction_bar(factions: list[dict], max_score: int) -> str:
    if not factions or max_score <= 0:
        return ""
    parts = []
    for f in factions:
        pct = int(f["score"] / max_score * 100)
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        parts.append(f"**{f['name']}** `{bar}` {f['score']}/{max_score}")
    return "\n".join(parts)


def build_status_embed(server_name: str, data: Optional[dict]) -> discord.Embed:
    """Публичная панель. Без IP, порта, пароля и списка игроков."""
    ts = f"<t:{int(datetime.now(timezone.utc).timestamp())}:R>"

    if data is None:
        embed = discord.Embed(
            title=f"🔴 {server_name}",
            description="Сервер **недоступен** или не отвечает.",
            color=COLOR_OFFLINE,
        )
        embed.set_footer(text=f"Обновлено {ts}")
        return embed

    cur      = data.get("players", {}).get("current", 0)
    mx       = data.get("players", {}).get("max", 0)
    map_     = data.get("map", "—")
    lighting = data.get("lighting", "—")

    minfo       = get_map_info(map_)
    map_display = f"{minfo['emoji']} **{map_}**\n`{minfo['mode']}` · {minfo['region']}"
    if minfo["desc"]:
        map_display += f"\n*{minfo['desc']}*"

    factions    = data.get("factionScores", [])
    score_cap   = data.get("scoreCap", 0)
    faction_str = _faction_bar(factions, score_cap)

    match_sec = data.get("matchSeconds", 0)
    m, s = divmod(int(match_sec), 60)
    h, m = divmod(m, 60)
    time_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    embed = discord.Embed(title=f"🟢 {server_name}", color=COLOR_ONLINE)
    embed.add_field(name="👥 Игроки",    value=f"**{cur} / {mx}**", inline=True)
    embed.add_field(name="⏱️ Матч",      value=f"`{time_str}`",     inline=True)
    embed.add_field(name="💡 Освещение", value=f"`{lighting}`",     inline=True)
    embed.add_field(name="🗺️ Карта",     value=map_display,         inline=False)
    if faction_str:
        embed.add_field(name="⚔️ Счёт фракций", value=faction_str, inline=False)
    embed.set_footer(text=f"Обновлено {ts}")
    return embed


def build_alert_embed(server_name: str, went_online: bool, data: Optional[dict] = None) -> discord.Embed:
    if went_online:
        cur = data.get("players", {}).get("current", 0) if data else 0
        mx  = data.get("players", {}).get("max", 0) if data else 0
        return discord.Embed(
            title=f"✅ {server_name} — онлайн",
            description=f"Сервер **поднялся**. Игроков: **{cur}/{mx}**",
            color=COLOR_ONLINE,
            timestamp=datetime.now(timezone.utc),
        )
    return discord.Embed(
        title=f"❌ {server_name} — оффлайн",
        description="Сервер **упал** или не отвечает.",
        color=COLOR_OFFLINE,
        timestamp=datetime.now(timezone.utc),
    )


# ─────────────────────────────────────────────
#  Cog
# ─────────────────────────────────────────────

class ServerMonitor(commands.Cog):
    """
    Мониторинг одного WARDOGS-сервера.
    Настройте SERVER_* константы под каждого бота.
    """

    # ── Настройки ── задайте индивидуально для каждого бота ─────────────────
    SERVER_NAME       = "WARDOGS #1"         # отображаемое имя
    SERVER_HOST       = "192.168.1.100"      # IP сервера
    SERVER_PORT       = 7776                 # RCON порт
    SERVER_PASSWORD   = "your_rcon_pass"     # пароль из ServerSettings.ini

    STATUS_CHANNEL_ID = 123456789012345678   # канал для живой панели
    ALERT_CHANNEL_ID  = 987654321098765432   # канал для уведомлений (или None)

    POLL_INTERVAL     = 30   # секунд между опросами
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[WDRconClient] = None
        self._online: Optional[bool] = None   # None = ещё не проверяли
        self._last_data: Optional[dict] = None
        self._status_message_id: Optional[int] = None

    # ── Жизненный цикл ───────────────────────────────────────────────────────

    async def cog_load(self):
        self._session = aiohttp.ClientSession()
        self._client = WDRconClient(
            self._session,
            self.SERVER_HOST,
            self.SERVER_PORT,
            self.SERVER_PASSWORD,
        )
        self.poll_loop.change_interval(seconds=self.POLL_INTERVAL)
        self.poll_loop.start()
        log.info("[%s] ServerMonitor запущен", self.SERVER_NAME)

    async def cog_unload(self):
        self.poll_loop.cancel()
        if self._session:
            await self._session.close()
        await self.bot.change_presence(activity=None)

    # ── Главный цикл ─────────────────────────────────────────────────────────

    @tasks.loop(seconds=30)
    async def poll_loop(self):
        data = await self._client.full_info()
        is_online = data is not None

        # Уведомление при смене статуса
        if self._online is not None and self._online != is_online:
            await self._send_alert(went_online=is_online, data=data)

        self._online    = is_online
        self._last_data = data

        await self._update_status_message(data)
        await self._update_bot_status(data)

    @poll_loop.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ── Статус бота ──────────────────────────────────────────────────────────

    async def _update_bot_status(self, data: Optional[dict]):
        if data is None:
            text   = f"🔴 {self.SERVER_NAME} — оффлайн"
            status = discord.Status.do_not_disturb
        else:
            cur  = data.get("players", {}).get("current", 0)
            mx   = data.get("players", {}).get("max", 0)
            map_ = data.get("map", "?")
            text   = f"🟢 {cur}/{mx} · {map_}"
            status = discord.Status.online

        await self.bot.change_presence(
            activity=discord.Game(name=text),
            status=status,
        )

    # ── Статус-панель ────────────────────────────────────────────────────────

    async def _update_status_message(self, data: Optional[dict]):
        channel = self.bot.get_channel(self.STATUS_CHANNEL_ID)
        if channel is None:
            return

        embed = build_status_embed(self.SERVER_NAME, data)

        if self._status_message_id:
            try:
                msg = await channel.fetch_message(self._status_message_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                self._status_message_id = None

        try:
            msg = await channel.send(embed=embed)
            self._status_message_id = msg.id
        except discord.HTTPException as e:
            log.error("[%s] Не удалось отправить панель: %s", self.SERVER_NAME, e)

    # ── Уведомления ──────────────────────────────────────────────────────────

    async def _send_alert(self, went_online: bool, data: Optional[dict]):
        ch_id   = self.ALERT_CHANNEL_ID or self.STATUS_CHANNEL_ID
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            return
        embed = build_alert_embed(self.SERVER_NAME, went_online, data)
        try:
            await channel.send(embed=embed)
        except discord.HTTPException as e:
            log.error("[%s] Не удалось отправить уведомление: %s", self.SERVER_NAME, e)

    # ── Slash-команды ────────────────────────────────────────────────────────

    @app_commands.command(name="status", description="Текущий статус сервера")
    async def cmd_status(self, interaction: discord.Interaction):
        await interaction.response.defer()
        data  = await self._client.full_info()
        embed = build_status_embed(self.SERVER_NAME, data)
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="players", description="Список игроков на сервере")
    async def cmd_players(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        players = await self._client.players()

        if players is None:
            await interaction.followup.send(f"❌ **{self.SERVER_NAME}** недоступен.", ephemeral=True)
            return
        if not players:
            await interaction.followup.send("На сервере нет игроков.", ephemeral=True)
            return

        # Только ник и фракция, без пинга, K/D и SteamID. Ответ виден только вызвавшему.
        lines = [
            f"{p.get('name', '?')[:20]:20s}  {p.get('faction', '?')}"
            for p in players[:50]
        ]
        if len(players) > 50:
            lines.append(f"… и ещё {len(players) - 50}")
        embed = discord.Embed(
            title=f"👥 Игроки на {self.SERVER_NAME} ({len(players)})",
            description="```" + "\n".join(lines) + "```",
            color=WARDOGS_COLOR,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="refresh", description="Принудительно обновить панель")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def cmd_refresh(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        data = await self._client.full_info()
        self._online    = data is not None
        self._last_data = data
        await self._update_status_message(data)
        await self._update_bot_status(data)
        await interaction.followup.send("✅ Панель обновлена.", ephemeral=True)


# ── Точка входа ──────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    await bot.add_cog(ServerMonitor(bot))
