"""
auto_translate.py — Auto-translates non-English messages using Google Translate.

Two modes per guild (default: live):
  live        — one embed per channel that edits itself as translations accumulate
  individual  — each non-English message gets its own inline reply

Commands:
  !translate                   — show current mode
  !translate mode live         — switch to live embed mode
  !translate mode individual   — switch to reply-per-message mode
  (Requires Manage Server)

No API key required — uses the free unofficial Google Translate endpoint.
"""

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

SESSION_TTL  = 300   # seconds of inactivity before a live embed session expires
MAX_LINES    = 10    # max entries in the live embed before old ones roll off
MIN_LEN      = 6     # ignore messages shorter than this (after stripping noise)
MIN_WORDS    = 2     # need at least this many words to trust language detection
MIN_CONF     = 0.75  # Google Translate confidence threshold (0–1)

# Only translate from languages people actually speak — blocks obscure detections
# that fire on abbreviations, slang, or short English words.
_ALLOWED_LANGS = {
    "af", "ar", "bg", "bn", "ca", "zh-CN", "zh-TW", "zh", "hr", "cs", "da",
    "nl", "fi", "fr", "de", "el", "gu", "he", "hi", "hu", "id", "it", "ja",
    "ko", "lt", "lv", "ml", "mr", "ms", "no", "pl", "pt", "ro", "ru", "sk",
    "sl", "es", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi",
}

_STRIP_RE = re.compile(
    r"<[^>]+>"          # Discord mentions / channel refs
    r"|https?://\S+"    # URLs
    r"|```[\s\S]*?```"  # code blocks
    r"|`[^`]+`",        # inline code
    re.MULTILINE,
)

_LANG_NAMES: dict[str, str] = {
    "af": "Afrikaans", "ar": "Arabic",   "bg": "Bulgarian", "ca": "Catalan",
    "zh-CN": "Chinese", "zh-TW": "Chinese (Traditional)",   "hr": "Croatian",
    "cs": "Czech",      "da": "Danish",  "nl": "Dutch",     "fi": "Finnish",
    "fr": "French",     "de": "German",  "el": "Greek",     "hi": "Hindi",
    "hu": "Hungarian",  "id": "Indonesian", "it": "Italian","ja": "Japanese",
    "ko": "Korean",     "lv": "Latvian", "lt": "Lithuanian","ms": "Malay",
    "no": "Norwegian",  "pl": "Polish",  "pt": "Portuguese","ro": "Romanian",
    "ru": "Russian",    "sk": "Slovak",  "sl": "Slovenian", "es": "Spanish",
    "sv": "Swedish",    "th": "Thai",    "tr": "Turkish",   "uk": "Ukrainian",
    "vi": "Vietnamese",
}


@dataclass
class _Session:
    """Tracks the live translation embed for one channel."""
    message:     discord.Message | None = None
    entries:     list[str]              = field(default_factory=list)
    last_active: float                  = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.last_active > SESSION_TTL

    def touch(self):
        self.last_active = time.monotonic()

    def add(self, author: str, original: str, translation: str, src_lang: str):
        lang = _LANG_NAMES.get(src_lang, src_lang.upper())
        self.entries.append(
            f"**{discord.utils.escape_markdown(author)}:** {original}\n"
            f"↳ *{translation}* `[{lang}]`"
        )
        self.touch()

    def build_embed(self) -> discord.Embed:
        visible = self.entries[-MAX_LINES:]
        embed = discord.Embed(
            title="🌐 Live Translation",
            description="\n\n".join(visible),
            color=0x5865F2,
        )
        embed.set_footer(text="Google Translate • Updates as the conversation continues")
        return embed


class AutoTranslateCog(commands.Cog, name="AutoTranslate"):
    """Translates non-English messages automatically using Google Translate."""

    def __init__(self, bot: commands.Bot):
        self.bot      = bot
        self._sessions: dict[int, _Session] = {}  # channel_id → live session
        self._lock    = asyncio.Lock()
        self._modes:  dict[int, str] = {}          # guild_id → "live" | "individual"

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    def _mode(self, guild_id: int) -> str:
        return self._modes.get(guild_id, "live")

    @staticmethod
    def _has_real_text(content: str) -> bool:
        cleaned    = _STRIP_RE.sub("", content).strip()
        word_chars = re.sub(r"[^\w\s]", "", cleaned, flags=re.UNICODE).strip()
        return len(word_chars) >= MIN_LEN

    async def _translate(self, text: str) -> tuple[str, str] | None:
        """
        Call the free unofficial Google Translate endpoint.
        Returns (english_text, src_lang_code) or None if already English / error.
        """
        cleaned = _STRIP_RE.sub("", text).strip()
        if not cleaned:
            return None
        if len(cleaned.split()) < MIN_WORDS:
            return None

        params = {"client": "gtx", "sl": "auto", "tl": "en", "dt": "t", "q": cleaned}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://translate.googleapis.com/translate_a/single",
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status != 200:
                        log.warning("Google Translate returned HTTP %d", r.status)
                        return None
                    data = await r.json(content_type=None)

            src_lang = data[2] if len(data) > 2 and isinstance(data[2], str) else "unknown"
            if src_lang == "en":
                return None
            if src_lang not in _ALLOWED_LANGS:
                return None

            # Check detection confidence (available at data[8][0][2][0])
            try:
                confidence = float(data[8][0][2][0])
            except (IndexError, TypeError, ValueError):
                confidence = 1.0
            if confidence < MIN_CONF:
                return None

            translation = "".join(seg[0] for seg in data[0] if seg and seg[0])
            return (translation, src_lang) if translation else None

        except asyncio.TimeoutError:
            log.warning("Google Translate timed out")
            return None
        except Exception as e:
            log.warning("Translation error: %s", e)
            return None

    # ── Message listener ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if not self._has_real_text(message.content):
            return

        result = await self._translate(message.content)
        if result is None:
            return

        translation, src_lang = result

        if self._mode(message.guild.id) == "individual":
            lang  = _LANG_NAMES.get(src_lang, src_lang.upper())
            embed = discord.Embed(description=f"↳ *{translation}*", color=0x5865F2)
            embed.set_footer(text=f"🌐 Translated from {lang} via Google Translate")
            try:
                await message.reply(embed=embed, mention_author=False)
            except discord.HTTPException as e:
                log.warning("Individual translate reply failed: %s", e)
            return

        # Live mode — single updating embed per channel
        async with self._lock:
            session = self._sessions.get(message.channel.id)
            if session is None or session.expired():
                session = _Session()
                self._sessions[message.channel.id] = session

            session.add(message.author.display_name, message.content, translation, src_lang)
            embed = session.build_embed()

            try:
                if session.message is None:
                    session.message = await message.channel.send(embed=embed)
                else:
                    await session.message.edit(embed=embed)
            except discord.NotFound:
                session.message = await message.channel.send(embed=embed)
            except discord.HTTPException as e:
                log.warning("Translate embed update failed: %s", e)
                session.message = None

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="translate",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Show or change the translation mode",
    )
    @commands.guild_only()
    async def translate_group(self, ctx: commands.Context):
        assert ctx.guild is not None
        mode = self._mode(ctx.guild.id)
        await ctx.send(
            f"🌐 Translation mode: **{mode}**\n"
            "`!translate mode live` — one shared embed that updates in place\n"
            "`!translate mode individual` — reply to each foreign message separately"
        )

    @translate_group.command(
        name="mode",
        brief="Set translation mode to 'live' or 'individual'",
    )
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_mode(self, ctx: commands.Context, mode: str):
        assert ctx.guild is not None
        mode = mode.lower()
        if mode not in ("live", "individual"):
            await ctx.send("❌ Valid modes: `live`, `individual`")
            return
        self._modes[ctx.guild.id] = mode
        # Clear any stale live sessions when switching modes
        if mode == "individual":
            self._sessions.pop(ctx.channel.id, None)
        await ctx.send(f"✅ Translation mode set to **{mode}**.")

    @set_mode.error
    async def set_mode_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Manage Server** permission to change this.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
