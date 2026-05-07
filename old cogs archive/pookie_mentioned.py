# cogs/pookie_ai.py
import asyncio
import os
import time
from typing import List, Optional

import discord
from discord.ext import commands

try:
    from openai import AsyncOpenAI as _RuntimeAsyncOpenAI
except Exception:
    _RuntimeAsyncOpenAI = None  # not installed / unavailable


# ===================== CONFIG =====================
TRIGGER_TOKEN = "pookie"       # case-insensitive substring trigger
CTX_FETCH_LIMIT = 10           # how many prior messages to include as context
MAX_MSG_CHARS = 600            # clamp each message content to this many chars

MODEL_NAME = "gpt-5-nano"      # fast + cheap is perfect for quips
MAX_OUTPUT_TOKENS = 10000         # small to avoid truncation
REQUEST_TIMEOUT = 25           # seconds
CHANNEL_COOLDOWN = 4.0         # seconds, basic anti-spam
# ==================================================


def _shorten(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _format_context(rows: List[discord.Message]) -> str:
    """Oldest -> newest compact transcript."""
    lines = []
    for m in rows:
        author = "You: (Pookie Bot)" if m.author.bot else getattr(m.author, "display_name", m.author.name)
        content = m.content or ""
        content = _shorten(content.strip(), MAX_MSG_CHARS)
        if not content and m.attachments:
            content = f"[{len(m.attachments)} attachment(s)]"
        prefix = ""
        try:
            ref = m.reference.resolved if m.reference else None
            if isinstance(ref, discord.Message):
                replied_to = getattr(ref.author, "display_name", getattr(ref.author, "name", "someone"))
                prefix = f"(reply to {replied_to}) "
        except Exception:
            pass
        lines.append(f"{author}: {prefix}{content}")
    return "\n".join(lines)


def _is_directed_at_bot(bot: commands.Bot, message: discord.Message) -> bool:
    """True if mentions bot, replies to bot, or contains TRIGGER_TOKEN."""
    content = (message.content or "").lower()
    if TRIGGER_TOKEN in content:
        return True
    if bot.user and bot.user.mentioned_in(message):
        return True
    try:
        if message.reference and isinstance(message.reference.resolved, discord.Message):
            target = message.reference.resolved
            if target.author and bot.user and target.author.id == bot.user.id:
                return True
    except Exception:
        pass
    print(f"[PookieAI] Not directed at bot: '{message.content}'")
    return False


class PookieAI(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldowns: dict[int, float] = {}  # channel_id -> last_ts
        api_key = os.getenv("OPENAI_API_KEY")
        if _RuntimeAsyncOpenAI and api_key:
            self.ai: Optional[object] = _RuntimeAsyncOpenAI(api_key=api_key)
        else:
            self.ai = None

    def _cooldown_ok(self, channel_id: int) -> bool:
        now = time.time()
        last = self._cooldowns.get(channel_id, 0.0)
        if now - last >= CHANNEL_COOLDOWN:
            self._cooldowns[channel_id] = now
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots
        if message.author.bot:
            return

        # Respect SeriousMode if present
        sm = self.bot.get_cog("SeriousMode")
        if sm and message.guild and getattr(sm, "is_active", None) and sm.is_active(message.guild):
            return

        # Only proceed if clearly directed at the bot
        if not _is_directed_at_bot(self.bot, message):
            return

        # Rate-limit per channel
        if not self._cooldown_ok(message.channel.id):
            return

        if not self.ai:
            print("[PookieAI] OpenAI not configured; set OPENAI_API_KEY.")
            return

        # Gather context (newest -> oldest, then reverse)
        hist: List[discord.Message] = []
        try:
            msgs: List[discord.Message] = []
            async for m in message.channel.history(
                limit=CTX_FETCH_LIMIT,
                before=message,
                oldest_first=False
            ):
                msgs.append(m)
            msgs.reverse()
            hist = msgs
        except Exception:
            pass
        

        transcript = _format_context(hist)
        print(f"[PookieAI] Context:\n{transcript}")
        trigger_author = getattr(message.author, "display_name", message.author.name)
        trigger_text = _shorten(message.content.strip(), MAX_MSG_CHARS)

        # --- edgy/annoyed, a lil' mean but funny (non-toxic) ---
        system_prompt = (
            "You are Pookie, a playful Discord bot with an eye-roll vibe.\n"
            "You will not have many tokens, do not waste them on reasoning.\n"
            "Tone: witty, mildly annoyed, a little mean in a FUNNY way—never hateful.\n"
            "Rules:\n"
            "• ONE short quip, <=140 chars.\n"
            "• No slurs, harassment, NSFW, or @everyone/@here.\n"
            "• Sound like chat, not a lecture.\n"
            "• Output plain text only. No prefaces, no code fences."
            
        )

        user_prompt = (
            f"Recent channel context (oldest → newest):\n{transcript}\n\n"
            f"User message from {trigger_author}:\n{trigger_text}\n\n"
            f"Write ONE edgy/funny line as a direct reply."
        )

        # Show typing indicator while we think
        async with message.channel.typing():
            try:
                # Minimal args (this model doesn't support temperature/reasoning)
                kwargs = dict(
                    model=MODEL_NAME,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",  "content": user_prompt},
                    ],
                    max_completion_tokens=MAX_OUTPUT_TOKENS,
                    response_format={"type": "text"},
                )

                try:
                    resp = await asyncio.wait_for(
                        self.ai.chat.completions.create(**kwargs),
                        timeout=REQUEST_TIMEOUT
                    )
                    print(f"[PookieAI] OpenAI response received.")
                    print(resp)
                except Exception as e:
                    # Some models want max_tokens instead
                    if "Unsupported parameter" in str(e) and "max_completion_tokens" in str(e):
                        kwargs.pop("max_completion_tokens", None)
                        kwargs["max_tokens"] = MAX_OUTPUT_TOKENS
                        resp = await asyncio.wait_for(
                            self.ai.chat.completions.create(**kwargs),
                            timeout=REQUEST_TIMEOUT
                        )
                    else:
                        raise

                text = (resp.choices[0].message.content or "").strip()
            except asyncio.TimeoutError:
                return
            except Exception as e:
                print(f"[PookieAI] OpenAI error: {e}")
                return

        print(f"[PookieAI] finish_reason={resp.choices[0].finish_reason}, usage={getattr(resp, 'usage', None)}")

        # If model gave nothing (or got truncated), use a safe quip
        if not text:
            text = "Use your inside math voice—it's 4. 🙄"

        try:
            await message.reply(text, mention_author=False)
        except Exception:
            try:
                await message.channel.send(text)
            except Exception:
                pass


async def setup(bot: commands.Bot):
    print("Loading PookieAI")
    await bot.add_cog(PookieAI(bot))
