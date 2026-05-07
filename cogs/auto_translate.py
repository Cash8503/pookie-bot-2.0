"""
auto_translate.py — Auto-translates non-English messages into a single live embed.

When someone speaks in a foreign language, the bot sends one embed that edits
itself to show a running transcript of translations. No spam — just one embed
per conversation that updates in-place.

Uses Claude Haiku for detection + translation in a single call.
"""

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import discord
from discord.ext import commands

try:
    from anthropic import AsyncAnthropic as _Anthropic
except ImportError:
    _Anthropic = None

log = logging.getLogger(__name__)

MODEL          = "claude-haiku-4-5-20251001"
SESSION_TTL    = 300     # seconds before an idle embed is considered stale
MAX_LINES      = 10      # max entries shown in the embed before old ones roll off
MIN_LEN        = 6       # skip messages shorter than this
TRANSLATE_COST = 0       # placeholder for future cost tracking

# Regex to strip content that isn't real language
_STRIP_RE = re.compile(
    r"<[^>]+>"          # Discord mentions / channel refs
    r"|https?://\S+"    # URLs
    r"|```[\s\S]*?```"  # code blocks
    r"|`[^`]+`",        # inline code
    re.MULTILINE,
)


@dataclass
class _Session:
    """Tracks the live translation embed for one channel."""
    message: discord.Message | None = None
    entries: list[str]              = field(default_factory=list)
    last_active: float              = field(default_factory=time.monotonic)

    def expired(self) -> bool:
        return time.monotonic() - self.last_active > SESSION_TTL

    def touch(self):
        self.last_active = time.monotonic()

    def add(self, author: str, original: str, translation: str):
        self.entries.append(
            f"**{discord.utils.escape_markdown(author)}:** {original}\n↳ *{translation}*"
        )
        self.touch()

    def build_embed(self) -> discord.Embed:
        visible = self.entries[-MAX_LINES:]
        embed = discord.Embed(
            title="🌐 Live Translation",
            description="\n\n".join(visible),
            color=0x5865F2,
        )
        embed.set_footer(text="Translating via Claude • Edits as the conversation continues")
        return embed


class AutoTranslateCog(commands.Cog, name="AutoTranslate"):
    """Sends a single updating embed for non-English conversations."""

    def __init__(self, bot: commands.Bot):
        self.bot  = bot
        api_key   = os.getenv("ANTHROPIC_API_KEY")
        self.ai   = _Anthropic(api_key=api_key) if _Anthropic and api_key else None
        self._sessions: dict[int, _Session] = {}   # channel_id → session
        self._lock = asyncio.Lock()
        if not self.ai:
            log.warning("AutoTranslateCog: Anthropic unavailable — cog is inactive.")

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    @staticmethod
    def _has_real_text(content: str) -> bool:
        """Return True if the message has enough actual text worth translating."""
        cleaned = _STRIP_RE.sub("", content).strip()
        # Remove emoji and punctuation to check for actual words
        word_chars = re.sub(r"[^\w\s]", "", cleaned, flags=re.UNICODE).strip()
        return len(word_chars) >= MIN_LEN

    async def _detect_and_translate(self, text: str) -> str | None:
        """
        Ask Claude to detect language and translate if non-English.
        Returns the English translation, or None if already English / error.
        """
        cleaned = _STRIP_RE.sub("", text).strip()
        if not cleaned:
            return None

        prompt = (
            "You are a language detector and translator.\n"
            "If the message below is already in English, reply with exactly: ENGLISH\n"
            "If it is in any other language, reply with ONLY the English translation — "
            "no explanation, no prefix, just the translated text.\n\n"
            "franch is not a typo, its a nickname"
            f"Message: {cleaned}"
        )
        try:
            resp = await asyncio.wait_for(
                self.ai.messages.create(
                    model=MODEL,
                    max_tokens=300,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=15,
            )
            result = resp.content[0].text.strip()
            return None if result.upper() == "ENGLISH" else result
        except asyncio.TimeoutError:
            log.warning("Translation timed out")
            return None
        except Exception as e:
            log.warning("Translation error: %s", e)
            return None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not self.ai:
            return
        if message.author.bot or message.guild is None:
            return
        if not self._has_real_text(message.content):
            return

        # Run translation (outside the lock — this is the slow part)
        translation = await self._detect_and_translate(message.content)
        if translation is None:
            return   # English or error

        async with self._lock:
            session = self._sessions.get(message.channel.id)
            if session is None or session.expired():
                session = _Session()
                self._sessions[message.channel.id] = session

            session.add(message.author.display_name, message.content, translation)
            embed = session.build_embed()

            try:
                if session.message is None:
                    session.message = await message.channel.send(embed=embed)
                else:
                    await session.message.edit(embed=embed)
            except discord.NotFound:
                # Embed was deleted — create a fresh one
                session.message = await message.channel.send(embed=embed)
            except discord.HTTPException as e:
                log.warning("Translate embed update failed: %s", e)
                session.message = None


async def setup(bot: commands.Bot):
    await bot.add_cog(AutoTranslateCog(bot))
