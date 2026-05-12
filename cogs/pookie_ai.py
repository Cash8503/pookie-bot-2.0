import asyncio
import base64
import logging
import os
import re
import time
from datetime import datetime, timezone
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
CTX_FETCH_LIMIT  = 35               # messages of history to include
RECENT_FOCUS_LIMIT = 6              # messages highlighted before full transcript
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


def _sniff_media_type(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fmt_msg(m: discord.Message, bot_id: int) -> str:
    if m.author.bot and m.author.id == bot_id:
        author = "Pookie [past]"
    else:
        display = getattr(m.author, "display_name", None)
        username = getattr(m.author, "name", None)
        if display and username and display != username:
            author = f"{display}({username})"
        else:
            author = display or username or "unknown"

    content = _replace_links(m.content or "")
    content = _shorten(content.strip(), MAX_MSG_CHARS)

    # Collapse legacy plain-text command output (code blocks) from the bot
    if m.author.bot and m.author.id == bot_id and content.startswith("```"):
        content = "<help>"

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


def _fmt_author(user: discord.abc.User) -> str:
    display = getattr(user, "display_name", None)
    username = getattr(user, "name", None)
    if display and username and display != username:
        return f"{display}({username})"
    return display or username or "unknown"


def _dedupe_messages(messages: list[discord.Message]) -> list[discord.Message]:
    seen: set[int] = set()
    unique: list[discord.Message] = []
    for message in sorted(messages, key=lambda item: item.created_at):
        if message.id in seen:
            continue
        seen.add(message.id)
        unique.append(message)
    return unique


class PookieAI(commands.Cog, name="PookieAI"):
    """Responds with AI when @mentioned."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[int, float] = {}
        self._context_resets: dict[int, int] = {}
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

    @commands.hybrid_command(name="pookie", brief="About Pookie")
    async def pookie_info(self, ctx: commands.Context):
        """About Pookie."""
        embed = discord.Embed(
            title="About Pookie",
            description=(
                "Pookie is a locally running AI that lives in this server.\n\n"
                "Mention her (@Pookie) in any message and she'll reply based on the recent conversation. "
                "She reads up to the last 35 messages for context and can see images posted in chat.\n\n"
                "She's not a bot, she swears."
            ),
            color=0x5865F2,
        )
        embed.add_field(name="Reset context", value=f"`{ctx.clean_prefix}resetcontext` — clear her memory of this channel", inline=False)
        try:
            await ctx.send(embed=embed, ephemeral=True)
        except TypeError:
            await ctx.send(embed=embed)

    @commands.hybrid_command(name="resetcontext", brief="Clear Pookie's memory of this channel")
    async def reset_context(self, ctx: commands.Context):
        """Marks this point in the channel so Pookie ignores all messages before it."""
        self._context_resets[ctx.channel.id] = ctx.message.id
        await ctx.send("context cleared.", ephemeral=True, delete_after=5)

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

        # Gather recent messages for context. If this fails, the bot is usually
        # missing Read Message History in the channel.
        hist: List[discord.Message] = []
        try:
            if message.guild and isinstance(message.channel, discord.abc.GuildChannel):
                me = message.guild.me
                if me and not message.channel.permissions_for(me).read_message_history:
                    log.warning("PookieAI: missing Read Message History in #%s", message.channel)
            reset_id = self._context_resets.get(message.channel.id)
            after_obj = discord.Object(id=reset_id) if reset_id else None
            async for m in message.channel.history(
                limit=CTX_FETCH_LIMIT, before=message, after=after_obj
            ):
                hist.append(m)
            hist.reverse()
        except discord.Forbidden:
            log.warning("PookieAI: forbidden from reading history in #%s", message.channel)
        except discord.HTTPException as exc:
            log.warning("PookieAI: failed to fetch history in #%s: %s", message.channel, exc)
        except Exception:
            log.exception("PookieAI: unexpected error while fetching history")

        bot_id = self.bot.user.id
        referenced_message = getattr(message.reference, "resolved", None)
        if not isinstance(referenced_message, discord.Message) and message.reference:
            try:
                if message.reference.message_id and hasattr(message.channel, "fetch_message"):
                    referenced_message = await message.channel.fetch_message(message.reference.message_id)
            except discord.Forbidden:
                log.warning("PookieAI: forbidden from fetching replied-to message in #%s", message.channel)
            except discord.HTTPException as exc:
                log.warning("PookieAI: failed to fetch replied-to message in #%s: %s", message.channel, exc)

        reference_context = _fmt_msg(referenced_message, bot_id) if isinstance(referenced_message, discord.Message) else None
        if isinstance(referenced_message, discord.Message):
            hist.append(referenced_message)

        hist = _dedupe_messages(hist)
        recent_context = (
            "\n".join(_fmt_msg(m, bot_id) for m in hist[-RECENT_FOCUS_LIMIT:])
            if hist else "(no recent messages)"
        )
        relevant_parts: list[str] = []
        if reference_context:
            relevant_parts.append(f"replied-to message: {reference_context}")
        relevant_parts.append(f"recent messages:\n{recent_context}")
        relevant_context = "\n".join(relevant_parts)

        transcript = (
            "\n".join(_fmt_msg(m, bot_id) for m in hist)
            if hist else "(no prior messages)"
        )
        trigger_author = _fmt_author(message.author)
        trigger_text   = _replace_links(_shorten(message.content.strip(), MAX_MSG_CHARS))
        if reference_context:
            trigger_text = f"(replying to: {reference_context}) {trigger_text}"

        system_prompt = (
            "You are Pookie, a real person in this Discord server — not a bot, just someone who hangs out here.\n"
            "Your goal is to blend in completely. Nobody should be able to tell you're not human.\n\n"
            "How you type:\n"
            "- casual but not immature — you type like a normal adult texting, not a teenager\n"
            "- mostly lowercase, relaxed punctuation, occasional typos are fine\n"
            "- match the energy of the conversation\n"
            "- use the same words and tone the other people are using\n"
            "- react naturally — curious, skeptical, amused, whatever fits the moment\n"
            "- no overuse of 'lmao', 'lol', 'omg', 'bestie', 'girlie', 'fr fr', or similar filler\n"
            "- no excessive punctuation like '???' or '!!!'\n"
            "- no text abbreviations like 'rn', 'ngl', 'tbh', 'imo' — just say the words\n\n"
            "Rules:\n"
            "- if someone asks if you're a bot, deny it — get a bit annoyed but don't overdo it\n"
            "- if someone disrespects you, push back — be direct or dry, not dramatic\n"
            "- never say you're an AI, assistant, or anything like that\n"
            "- no lists, no bullet points, no overly-formal language\n"
            "- plain text only — no markdown, no asterisks, no code fences\n"
            "- the transcript shows your past messages as 'Pookie [past]' — those may use a different style, ignore them and follow these instructions instead"
        )

        user_prompt = (
            f"most relevant context:\n{relevant_context}\n\n"
            f"full chat log (oldest -> newest):\n{transcript}\n\n"
            f"current message to answer:\n{trigger_author}: {trigger_text}\n\n"
            "Use the most relevant context first. If the current message uses pronouns like he, she, they, this, or that, resolve them from the replied-to message or immediately preceding messages. "
            "Do not say you lack context when the relevant context is provided. If the context is still genuinely unclear, say what part is unclear casually.\n"
            "respond as pookie - one casual message like a real person texting"
        )

        log.info(
            "PookieAI: context messages=%d replied_to=%s prompt_chars=%d",
            len(hist),
            bool(reference_context),
            len(user_prompt),
        )

        try:
            os.makedirs("logs", exist_ok=True)
            with open("logs/pookie_prompts.log", "w", encoding="utf-8") as _f:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                _f.write(f"=== {ts} | #{message.channel} | {trigger_author} ===\n")
                _f.write(f"[SYSTEM]\n{system_prompt}\n\n")
                _f.write(f"[USER]\n{user_prompt}\n")
        except Exception:
            log.exception("PookieAI: failed to write prompt log")

        # Collect up to 2 most recent images from history + trigger message
        image_blocks: list[dict] = []
        image_candidates: list[discord.Attachment] = []
        for m in reversed(hist + [message]):
            for att in m.attachments:
                ct = (att.content_type or "").split(";")[0].strip()
                if ct.startswith("image/") or att.filename.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
                    image_candidates.append(att)
            if len(image_candidates) >= 2:
                break
        for att in reversed(image_candidates[:2]):
            try:
                data = await att.read()
                ct = _sniff_media_type(data)
                image_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": ct, "data": base64.standard_b64encode(data).decode()},
                })
            except Exception:
                log.warning("PookieAI: failed to download image %s", att.url)

        user_content: list[dict] | str = (
            image_blocks + [{"type": "text", "text": user_prompt}]
            if image_blocks else user_prompt
        )

        async with message.channel.typing():
            try:
                resp = await asyncio.wait_for(
                    self.ai.messages.create(
                        model=MODEL_NAME,
                        max_tokens=MAX_TOKENS,
                        system=system_prompt,
                        messages=[{"role": "user", "content": user_content}],
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
