import asyncio
import logging
import os
import re
import time
from typing import List

import discord
from discord.ext import commands

from cogs._guild_cogs import is_cog_disabled

log = logging.getLogger(__name__)

try:
    from anthropic import AsyncAnthropic as _AsyncAnthropic
except ImportError:
    _AsyncAnthropic = None

# ===================== CONFIG =====================
CTX_FETCH_LIMIT  = 20               # messages of history to include
MAX_MSG_CHARS    = 600              # clamp each message in transcript
MODEL_NAME       = "claude-haiku-4-5"  # fast + cheap for Discord quips
MAX_TOKENS       = 300
REQUEST_TIMEOUT  = 25               # seconds
CHANNEL_COOLDOWN = 3.0              # seconds per channel
# ==================================================

_URL_RE       = re.compile(r"https?://\S+", re.IGNORECASE)
_TENOR_RE     = re.compile(r"tenor\.com/view/(.+?)(?:\?|$)", re.I)
_TENOR_TRAIL  = re.compile(r"-\d+$")   # strip trailing numeric ID from slug

_LINK_RULES = [
    (re.compile(r"\.(png|jpe?g|webp|bmp|tiff?)(\?|$)",  re.I), "<image>"),
    (re.compile(r"\.(mp4|webm|mov|avi)(\?|$)",           re.I), "<video>"),
    (re.compile(r"(youtu\.be|youtube\.com/watch)",       re.I), "<youtube>"),
    (re.compile(r"(discord\.gg|discord\.com/invite)",    re.I), "<invite>"),
    (re.compile(r"twitch\.tv",                           re.I), "<stream>"),
    (re.compile(r"spotify\.com",                         re.I), "<spotify>"),
    (re.compile(r"giphy\.com|\.gif(\?|$)",               re.I), "<gif>"),
]


def _tenor_label(url: str) -> str | None:
    m = _TENOR_RE.search(url)
    if not m:
        return None
    slug = _TENOR_TRAIL.sub("", m.group(1))          # drop trailing ID
    words = [w for w in slug.split("-") if w != "gif"]
    return f"<gif: {' '.join(words)}>" if words else "<gif>"


def _replace_links(text: str) -> str:
    def _sub(m):
        url = m.group(0)
        if "tenor.com" in url.lower():
            return _tenor_label(url) or "<gif>"
        for pattern, label in _LINK_RULES:
            if pattern.search(url):
                return label
        return "<link>"
    return _URL_RE.sub(_sub, text)


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fmt_msg(m: discord.Message, bot_id: int) -> str:
    if m.author.bot and m.author.id == bot_id:
        author = "You (Pookie)"
    else:
        display = getattr(m.author, "display_name", None)
        username = getattr(m.author, "name", None)
        if display and username and display != username:
            author = f"{display}({username})"
        else:
            author = display or username or "unknown"

    content = _replace_links(m.content or "")
    content = _shorten(content.strip(), MAX_MSG_CHARS)

    if not content:
        if m.attachments:
            content = f"<{len(m.attachments)} attachment(s)>"
        elif m.embeds:
            content = "<embed>"
        else:
            content = "<empty>"

    try:
        ref = m.reference.resolved if m.reference else None
        if isinstance(ref, discord.Message):
            replied = getattr(ref.author, "display_name", getattr(ref.author, "name", "someone"))
            content = f"(↩ {replied}) {content}"
    except Exception:
        pass

    return f"{author}: {content}"


class PookieAI(commands.Cog, name="PookieAI"):
    """Responds with AI when @mentioned."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[int, float] = {}
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self.ai = _AsyncAnthropic(api_key=api_key) if _AsyncAnthropic and api_key else None
        if not self.ai:
            log.warning("PookieAI: Anthropic unavailable — install anthropic and set ANTHROPIC_API_KEY.")

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    def _on_cooldown(self, channel_id: int) -> bool:
        now = time.monotonic()
        if now - self._cooldowns.get(channel_id, 0.0) < CHANNEL_COOLDOWN:
            return True
        self._cooldowns[channel_id] = now
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return
        if message.guild and is_cog_disabled(self.bot.settings, message.guild.id, "pookie_ai"):
            return
        if not self.bot.user or not self.bot.user.mentioned_in(message):
            return
        if self._on_cooldown(message.channel.id):
            return
        if not self.ai:
            return

        # Gather last 20 messages for context
        hist: List[discord.Message] = []
        try:
            async for m in message.channel.history(
                limit=CTX_FETCH_LIMIT, before=message, oldest_first=True
            ):
                hist.append(m)
        except Exception:
            pass

        bot_id = self.bot.user.id
        transcript = (
            "\n".join(_fmt_msg(m, bot_id) for m in hist)
            if hist else "(no prior messages)"
        )
        trigger_author = getattr(message.author, "display_name", message.author.name)
        trigger_text   = _replace_links(_shorten(message.content.strip(), MAX_MSG_CHARS))

        system_prompt = (
            "You are Pookie, a real person in this Discord server. not a bot — a real girl who hangs out here.\n"
            "Your job is to blend in completely. nobody should be able to tell you're not human.\n\n"
            "How you type:\n"
            "- mostly lowercase, lazy punctuation, typos are fine\n"
            "- short replies. 1-2 sentences max, like a real chat message\n"
            "- match the energy of the conversation — if it's chaotic, be chaotic. if it's chill, be chill\n"
            "- use the same slang/vibe the other people in chat are using\n"
            "- don't use perfect grammar or full sentences\n"
            "- react like a person would — surprised, annoyed, amused, whatever fits\n"
            "- reference what people said earlier in chat naturally\n"
            "- avoid text abbreviations like 'rn', 'ngl', 'tbh', 'imo', 'irl' — just say the words normally\n\n"
            "Rules:\n"
            "- if someone asks if you're a bot, deny it. get a little offended even\n"
            "- if someone insults or disrespects you, clap back. don't just take it — be witty, sarcastic, or blunt depending on the situation\n"
            "- never say you're an AI, assistant, or anything like that\n"
            "- no lists, no bullet points, no formal language\n"
            "- no slurs, no harassment\n"
            "- plain text only — no code fences, no asterisks for emphasis"
        )

        user_prompt = (
            f"chat log (oldest → newest):\n{transcript}\n\n"
            f"{trigger_author}: {trigger_text}\n\n"
            "respond as pookie — one casual message like a real person texting"
        )

        async with message.channel.typing():
            try:
                resp = await asyncio.wait_for(
                    self.ai.messages.create(
                        model=MODEL_NAME,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_prompt}],
                    ),
                    timeout=REQUEST_TIMEOUT,
                )
                text = resp.content[0].text.strip() if resp.content else ""
                usage = getattr(resp, "usage", None)
                log.info(
                    "PookieAI: stop=%s in=%s out=%s",
                    resp.stop_reason,
                    getattr(usage, "input_tokens", "?"),
                    getattr(usage, "output_tokens", "?"),
                )
            except asyncio.TimeoutError:
                log.warning("PookieAI: request timed out")
                return
            except Exception:
                log.exception("PookieAI: unexpected error")
                return

        if not text:
            text = "I literally cannot even right now. 💅"

        try:
            await message.reply(text, mention_author=False)
        except Exception:
            try:
                await message.channel.send(text)
            except Exception:
                log.exception("PookieAI: failed to send response")


async def setup(bot: commands.Bot):
    await bot.add_cog(PookieAI(bot))
