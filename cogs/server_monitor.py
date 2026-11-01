"""
WARDOGS Server Monitor Cog
Мониторинг одного WARDOGS-сервера через HTTP RCON API (порт 7776).
Один экземпляр бота = один сервер.
"""

import asyncio
import os
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
        """Статус + текст ротации. None = сервер недоступен."""
        status = await self.status()
        if status is None:
            return None

        # Индексы ротации из статуса → названия карт из /v1/rotation
        rot_idx  = status.get("rotation") or {}
        rot      = await self._get("/v1/rotation") or {}
        entries  = rot.get("entries", [])

        def map_at(i) -> Optional[str]:
            if isinstance(i, int) and 0 <= i < len(entries):
                return entries[i].get("map")
            return None

        now_map  = map_at(rot_idx.get("nowIndex"))
        next_map = map_at(rot_idx.get("nextIndex"))
        rotation_text = None
        if now_map or next_map:
            rotation_text = f"сейчас **{now_map or '—'}** · следующая **{next_map or '—'}**"

        return {**status, "rotationText": rotation_text}


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

# Баннеры карт лежат в assets/maps/ рядом с репозиторием и прикрепляются к сообщению.
ASSETS_MAPS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "maps")

MAP_IMAGE_FILES: dict[str, str] = {
    "Bakurani":   "bakurani.png",
    "Ozeti":      "ozeti.png",
    "Zestafona":  "zestafona.png",
    "Jutari":     "jutari.png",
    "Saburtulo":  "saburtulo.png",
}


def map_image_name(map_id: Optional[str]) -> Optional[str]:
    """Имя файла баннера для карты, если он существует на диске."""
    if not map_id:
        return None
    fname = MAP_IMAGE_FILES.get(map_id)
    if fname and os.path.exists(os.path.join(ASSETS_MAPS_DIR, fname)):
        return fname
    return None


def make_map_file(map_id: Optional[str]) -> Optional[discord.File]:
    """Свежий discord.File на каждый вызов (файл после отправки закрывается)."""
    fname = map_image_name(map_id)
    if fname is None:
        return None
    return discord.File(os.path.join(ASSETS_MAPS_DIR, fname), filename=fname)


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
    now = datetime.now(timezone.utc)

    if data is None:
        embed = discord.Embed(
            title=f"📡 {server_name} — статус сервера",
            description="Сервер **недоступен** или не отвечает.",
            color=COLOR_OFFLINE,
            timestamp=now,
        )
        embed.set_footer(text="Автосводка")
        return embed

    cur      = data.get("players", {}).get("current", 0)
    mx       = data.get("players", {}).get("max", 0)
    map_     = data.get("map", "—")
    exps     = ", ".join(data.get("experiences", [])) or "—"
    lighting = data.get("lighting", "—")

    minfo = get_map_info(map_)

    factions = data.get("factionScores", [])
    faction_str = "\n".join(
        f"• **{f.get('name', '?')}** — {f.get('score', 0)}" for f in factions
    ) or "—"

    rotation_text = data.get("rotationText") or "—"

    embed = discord.Embed(
        title=f"📡 {server_name} — статус сервера",
        description=f"*{minfo['region']}*" if minfo["region"] != "—" else None,
        color=COLOR_ONLINE,
        timestamp=now,
    )
    embed.add_field(name="Карта",        value=f"**{map_}**",    inline=True)
    embed.add_field(name="Свет",         value=lighting,         inline=True)
    embed.add_field(name="Игроки",       value=f"{cur}/{mx}",    inline=True)
    embed.add_field(name="Режим",        value=exps,             inline=False)
    embed.add_field(name="Счёт фракций", value=faction_str,      inline=False)
    embed.add_field(name="Ротация",      value=rotation_text,    inline=False)

    fname = map_image_name(map_)
    if fname:
        embed.set_image(url=f"attachment://{fname}")

    embed.set_footer(text="Автосводка")
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


class JoinView(discord.ui.View):
    """Кнопка «Подключиться». Адрес виден только нажавшему, в ответе ephemeral."""

    def __init__(self, cog: "ServerMonitor"):
        super().__init__(timeout=None)   # persistent: работает и после перезапуска
        self.cog = cog

    @discord.ui.button(
        label="Подключиться",
        style=discord.ButtonStyle.success,
        emoji="🎮",
        custom_id="wardogs_join",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        addr = self.cog.JOIN_ADDRESS
        if not addr:
            await interaction.response.send_message(
                "Адрес подключения пока не настроен.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"Подключиться: steam://connect/{addr}", ephemeral=True
        )


def _format_duration(total_sec: int) -> str:
    h, rem = divmod(int(total_sec), 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч {m}м {s}с"
    return f"{m}м {s}с"


def match_snapshot(data: dict) -> dict:
    """Снимок состояния матча, который сохраняем между опросами."""
    return {
        "map":          data.get("map", "—"),
        "exps":         ", ".join(data.get("experiences", [])) or "—",
        "lighting":     data.get("lighting", "—"),
        "matchSeconds": int(data.get("matchSeconds", 0) or 0),
        "players":      data.get("players", {}).get("current", 0),
        "factions":     data.get("factionScores", []),
    }


def build_match_summary_embed(snap: dict) -> discord.Embed:
    """Итоги завершённого матча по последнему снимку."""
    factions = snap["factions"]
    top_score = max((f.get("score", 0) for f in factions), default=0)
    leaders = [f for f in factions if f.get("score", 0) == top_score and top_score > 0]
    if len(leaders) == 1:
        winner = leaders[0].get("name", "?")
    elif leaders:
        winner = "Ничья"
    else:
        winner = "—"

    max_score = max(top_score, 1)
    lines = []
    for f in factions:
        score = f.get("score", 0)
        blocks = round(score / max_score * 10) if score > 0 else 0
        blocks = max(blocks, 1) if score > 0 else 0
        bar = "█" * blocks
        lines.append(f"• **{f.get('name', '?')}** — {score} {bar}")

    embed = discord.Embed(
        title="🏁 Матч завершён",
        color=WARDOGS_COLOR,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Карта",        value=f"**{snap['map']}**",              inline=True)
    embed.add_field(name="Режим",        value=snap["exps"],                      inline=True)
    embed.add_field(name="Свет",         value=snap["lighting"],                  inline=True)
    embed.add_field(name="Игроков",      value=str(snap["players"]),              inline=True)
    embed.add_field(name="Длительность", value=_format_duration(snap["matchSeconds"]), inline=True)
    embed.add_field(name="Победитель",   value=f"**{winner}**",                   inline=True)
    embed.add_field(name="Счёт фракций", value="\n".join(lines) or "—",           inline=False)
    embed.set_footer(text="Итоги матча")
    return embed


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
    JOIN_ADDRESS      = ""   # адрес для кнопки «Подключиться», например "1.2.3.4:27015"
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[WDRconClient] = None
        self._online: Optional[bool] = None   # None = ещё не проверяли
        self._last_data: Optional[dict] = None
        self._status_message_id: Optional[int] = None
        self._prev_match: Optional[dict] = None   # снимок последнего опроса для итогов матча
        self._panel_searched: bool = False        # искали ли уже панель в канале после старта

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
        # Регистрируем persistent view, чтобы кнопка работала на старых сообщениях после перезапуска
        self.bot.add_view(JoinView(self))
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

        # Итоги матча: смена карты/режима или сброс таймера = предыдущий матч закончился
        if data is not None:
            snap = match_snapshot(data)
            prev = self._prev_match
            if prev and self._match_ended(prev, snap):
                await self._send_match_summary(prev)
            self._prev_match = snap

        await self._update_status_message(data)
        await self._update_bot_status(data)

    @staticmethod
    def _match_ended(prev: dict, cur: dict) -> bool:
        if prev["matchSeconds"] < 60:   # шум и короткие разогревы не считаем
            return False
        return (
            prev["map"] != cur["map"]
            or prev["exps"] != cur["exps"]
            or cur["matchSeconds"] < prev["matchSeconds"]
        )

    async def _send_match_summary(self, snap: dict):
        ch_id   = self.ALERT_CHANNEL_ID or self.STATUS_CHANNEL_ID
        channel = self.bot.get_channel(ch_id)
        if channel is None:
            return
        try:
            await channel.send(embed=build_match_summary_embed(snap))
        except discord.HTTPException as e:
            log.error("[%s] Не удалось отправить итоги матча: %s", self.SERVER_NAME, e)

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

        # После перезапуска ID панели неизвестен: ищем свою панель и редактируем её
        if self._status_message_id is None and not self._panel_searched:
            self._panel_searched = True
            self._status_message_id = await self._find_existing_panel(channel)

        embed = build_status_embed(self.SERVER_NAME, data)
        file  = make_map_file(data.get("map") if data else None)
        files = [file] if file else []

        if self._status_message_id:
            try:
                msg = await channel.fetch_message(self._status_message_id)
                # attachments=[] снимает старый баннер, если карта сменилась
                await msg.edit(embed=embed, attachments=files, view=JoinView(self))
                return
            except (discord.NotFound, discord.HTTPException):
                self._status_message_id = None

        try:
            msg = await channel.send(embed=embed, files=files, view=JoinView(self))
            self._status_message_id = msg.id
        except discord.HTTPException as e:
            log.error("[%s] Не удалось отправить панель: %s", self.SERVER_NAME, e)

    async def _find_existing_panel(self, channel) -> Optional[int]:
        """Ищет последнюю панель статуса этого сервера, отправленную ботом."""
        try:
            async for msg in channel.history(limit=100):
                if msg.author.id != self.bot.user.id:
                    continue
                for e in msg.embeds:
                    if e.title and e.title.endswith("— статус сервера") and self.SERVER_NAME in e.title:
                        log.info("[%s] Найдена существующая панель: %s", self.SERVER_NAME, msg.id)
                        return msg.id
        except discord.HTTPException as e:
            log.error("[%s] Не удалось поискать панель: %s", self.SERVER_NAME, e)
        return None

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
        file  = make_map_file(data.get("map") if data else None)
        await interaction.followup.send(embed=embed, files=[file] if file else [], view=JoinView(self))

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
