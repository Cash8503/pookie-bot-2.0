"""
auto_translate.py — Auto-translates messages using a configurable provider.

Settings per guild (all require Manage Server):
  !translate                    — show current settings
  !translate mode live          — one shared embed that edits itself
  !translate mode individual    — reply to each foreign message separately
  !translate provider claude    — use Claude (Anthropic API key required)
  !translate provider google    — use Google Translate (free, no key needed)
  !translate lang <code>        — set target language, e.g. en, es, ja
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import aiohttp
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

try:
    from anthropic import AsyncAnthropic as _AsyncAnthropic
except ImportError:
    _AsyncAnthropic = None

SESSION_TTL   = 300   # seconds before a live embed session expires
MAX_LINES     = 10    # max entries in the live embed before old ones roll off
MIN_LEN       = 3     # skip messages shorter than this after stripping noise
_CLAUDE_MODEL = "claude-haiku-4-5"

# Google only: block obscure detections (abbreviations, slang misidentified as a language)
_ALLOWED_LANGS = {
    "af", "ar", "bg", "bn", "ca", "zh-CN", "zh-TW", "zh", "hr", "cs", "da",
    "nl", "fi", "fr", "de", "el", "gu", "he", "hi", "hu", "id", "it", "ja",
    "ko", "lt", "lv", "ml", "mr", "ms", "no", "pl", "pt", "ro", "ru", "sk",
    "sl", "es", "sv", "sw", "ta", "te", "th", "tl", "tr", "uk", "ur", "vi",
}
MIN_CONF = 0.75  # Google confidence threshold

_STRIP_RE = re.compile(
    r"<[^>]+>"          # Discord mentions / channel refs
    r"|https?://\S+"    # URLs
    r"|```[\s\S]*?```"  # code blocks
    r"|`[^`]+`",        # inline code
    re.MULTILINE,
)

_LANG_NAMES: dict[str, str] = {
    "af": "Afrikaans",  "ar": "Arabic",      "bg": "Bulgarian",  "ca": "Catalan",
    "zh-CN": "Chinese", "zh-TW": "Chinese (Traditional)",        "hr": "Croatian",
    "cs": "Czech",      "da": "Danish",      "nl": "Dutch",      "en": "English",
    "fi": "Finnish",    "fr": "French",      "de": "German",     "el": "Greek",
    "hi": "Hindi",      "hu": "Hungarian",   "id": "Indonesian", "it": "Italian",
    "ja": "Japanese",   "ko": "Korean",      "lv": "Latvian",    "lt": "Lithuanian",
    "ms": "Malay",      "no": "Norwegian",   "pl": "Polish",     "pt": "Portuguese",
    "ro": "Romanian",   "ru": "Russian",     "sk": "Slovak",     "sl": "Slovenian",
    "es": "Spanish",    "sv": "Swedish",     "th": "Thai",       "tr": "Turkish",
    "uk": "Ukrainian",  "vi": "Vietnamese",
}


@dataclass
class _Session:
    """Tracks the live translation embed for one channel."""
    message:      discord.Message | None = None
    entries:      list[str]              = field(default_factory=list)
    last_active:  float                  = field(default_factory=time.monotonic)
    provider_label: str                  = "Google Translate"

    def expired(self) -> bool:
        return time.monotonic() - self.last_active > SESSION_TTL

    def touch(self):
        self.last_active = time.monotonic()

    def add(self, author: str, original: str, translation: str, src_lang: str, provider_label: str):
        lang = _LANG_NAMES.get(src_lang, src_lang.upper())
        self.entries.append(
            f"**{discord.utils.escape_markdown(author)}:** {original}\n"
            f"↳ *{translation}* `[{lang}]`"
        )
        self.provider_label = provider_label
        self.touch()

    def build_embed(self) -> discord.Embed:
        visible = self.entries[-MAX_LINES:]
        embed = discord.Embed(
            title="🌐 Live Translation",
            description="\n\n".join(visible),
            color=0x5865F2,
        )
        embed.set_footer(text=f"{self.provider_label} • Updates as the conversation continues")
        return embed


class AutoTranslateCog(commands.Cog, name="AutoTranslate"):
    """Translates non-English messages automatically."""

    def __init__(self, bot: commands.Bot):
        self.bot      = bot
        self._sessions: dict[int, _Session] = {}
        self._lock    = asyncio.Lock()
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self._claude = _AsyncAnthropic(api_key=api_key) if _AsyncAnthropic and api_key else None

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    def _mode(self, guild_id: int) -> str:
        return self.bot.settings.get(guild_id, "auto_translate", "mode", "live")

    def _provider(self, guild_id: int) -> str:
        return self.bot.settings.get(guild_id, "auto_translate", "provider", "google")

    def _target_lang(self, guild_id: int) -> str:
        return self.bot.settings.get(guild_id, "auto_translate", "target_lang", "en")

    @staticmethod
    def _clean(content: str) -> str:
        return _STRIP_RE.sub("", content).strip()

    # ── Translation backends ───────────────────────────────────────────────────

    async def _translate_google(self, text: str, target_lang: str) -> tuple[str, str] | None:
        cleaned = self._clean(text)
        if not cleaned or len(cleaned) < MIN_LEN:
            return None

        params = {"client": "gtx", "sl": "auto", "tl": target_lang, "dt": "t", "q": cleaned}
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
            # Skip if already the target language
            if src_lang == target_lang or src_lang == target_lang.split("-")[0]:
                return None
            if src_lang not in _ALLOWED_LANGS:
                return None
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
            log.warning("Translation error (google): %s", e)
            return None

    async def _translate_claude(self, text: str, target_lang: str) -> tuple[str, str] | None:
        if self._claude is None:
            log.warning("Claude translation requested but ANTHROPIC_API_KEY is not set")
            return None

        cleaned = self._clean(text)
        if not cleaned or len(cleaned) < MIN_LEN:
            return None

        target_name = _LANG_NAMES.get(target_lang, target_lang.upper())
        prompt = (
            f"Translate the following Discord message to {target_name}.\n"
            f"Respond with exactly SKIP if any of these are true:\n"
            f"- The message is already in {target_name}\n"
            "- It contains no translatable natural language (only emojis, numbers, symbols, gibberish)\n"
            "- It looks like a typo, autocorrect error, or garbled English rather than real foreign text\n"
            "- You are not confident it is intentional writing in another language\n\n"
            "Otherwise respond with this exact format on a single line:\n"
            "[LANG_CODE] translated text\n\n"
            "LANG_CODE is the ISO 639-1 code of the source language (e.g. fr, ja, ru).\n\n"
            f"Message:\n{cleaned}"
        )

        try:
            resp = await asyncio.wait_for(
                self._claude.messages.create(
                    model=_CLAUDE_MODEL,
                    max_tokens=500,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=15,
            )
            raw = resp.content[0].text.strip() if resp.content else ""
            if not raw or raw.upper() == "SKIP":
                return None
            m = re.match(r"\[([a-zA-Z]{2,5}(?:-[a-zA-Z]{2,4})?)\]\s+(.+)", raw, re.DOTALL)
            if not m:
                return None
            src_lang    = m.group(1).lower()
            translation = m.group(2).strip()
            return (translation, src_lang) if translation else None

        except asyncio.TimeoutError:
            log.warning("Claude translation timed out")
            return None
        except Exception as e:
            log.warning("Translation error (claude): %s", e)
            return None

    async def _translate(self, text: str, guild_id: int) -> tuple[str, str, str] | None:
        """Returns (translation, src_lang, provider_label) or None."""
        provider    = self._provider(guild_id)
        target_lang = self._target_lang(guild_id)

        if provider == "claude":
            result = await self._translate_claude(text, target_lang)
            label  = "Claude (Anthropic)"
        else:
            result = await self._translate_google(text, target_lang)
            label  = "Google Translate"

        if result is None:
            return None
        translation, src_lang = result
        return (translation, src_lang, label)

    # ── Message listener ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if len(self._clean(message.content)) < MIN_LEN:
            return

        result = await self._translate(message.content, message.guild.id)
        if result is None:
            return

        translation, src_lang, provider_label = result

        if self._mode(message.guild.id) == "individual":
            lang  = _LANG_NAMES.get(src_lang, src_lang.upper())
            embed = discord.Embed(description=f"↳ *{translation}*", color=0x5865F2)
            embed.set_footer(text=f"🌐 Translated from {lang} via {provider_label}")
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

            session.add(message.author.display_name, message.content, translation, src_lang, provider_label)
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

    # ── Commands ───────────────────────────────────────────────────────────────

    @commands.hybrid_group(
        name="translate",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Show or change translation settings",
    )
    @commands.guild_only()
    async def translate_group(self, ctx: commands.Context):
        assert ctx.guild is not None
        mode      = self._mode(ctx.guild.id)
        provider  = self._provider(ctx.guild.id)
        lang      = self._target_lang(ctx.guild.id)
        lang_name = _LANG_NAMES.get(lang, lang.upper())
        await ctx.send(
            f"🌐 **Translation settings**\n"
            f"Mode: **{mode}** | Provider: **{provider}** | Target: **{lang_name}** (`{lang}`)\n\n"
            "`!translate mode live` — one shared embed that updates in place\n"
            "`!translate mode individual` — reply to each foreign message separately\n"
            "`!translate provider claude` — use Claude (Anthropic API key required)\n"
            "`!translate provider google` — use Google Translate (free, no key needed)\n"
            "`!translate lang <code>` — set target language, e.g. `en`, `es`, `ja`"
        )

    @translate_group.command(name="mode", brief="Set translation mode: 'live' or 'individual'")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_mode(self, ctx: commands.Context, mode: str):
        assert ctx.guild is not None
        mode = mode.lower()
        if mode not in ("live", "individual"):
            await ctx.send("❌ Valid modes: `live`, `individual`")
            return
        await self.bot.settings.set(ctx.guild.id, "auto_translate", "mode", mode)
        if mode == "individual":
            stale = [
                cid for cid in list(self._sessions)
                if getattr(self.bot.get_channel(cid), "guild", None) is ctx.guild
            ]
            for cid in stale:
                self._sessions.pop(cid, None)
        await ctx.send(f"✅ Translation mode set to **{mode}**.")

    @translate_group.command(name="provider", brief="Set provider: 'claude' or 'google'")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_provider(self, ctx: commands.Context, provider: str):
        assert ctx.guild is not None
        provider = provider.lower()
        if provider not in ("claude", "google"):
            await ctx.send("❌ Valid providers: `claude`, `google`")
            return
        if provider == "claude" and self._claude is None:
            await ctx.send("❌ Claude unavailable — set `ANTHROPIC_API_KEY` in `.env` first.")
            return
        await self.bot.settings.set(ctx.guild.id, "auto_translate", "provider", provider)
        await ctx.send(f"✅ Translation provider set to **{provider}**.")

    @translate_group.command(name="lang", brief="Set target language code, e.g. en, es, ja")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_guild=True)
    async def set_lang(self, ctx: commands.Context, code: str):
        assert ctx.guild is not None
        code      = code.lower().strip()
        lang_name = _LANG_NAMES.get(code, code.upper())
        await self.bot.settings.set(ctx.guild.id, "auto_translate", "target_lang", code)
        await ctx.send(f"✅ Target language set to **{lang_name}** (`{code}`).")

    @set_mode.error
    @set_provider.error
    @set_lang.error
    async def _settings_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("❌ You need **Manage Server** permission to change this.")


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
