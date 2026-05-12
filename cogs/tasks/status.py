"""
status.py — Cycles through messages in data/statuses.txt as the bot's presence.

File format (one per line):
  type: message     — e.g.  watching: the chaos unfold
  custom: message   — raw text, no prefix prepended, always uses CustomActivity
  message           — bare text defaults to "watching"
  # comment         — ignored

Discord automatically prepends the type label in the status bar, so the message
text must NOT repeat it:
  listening: <text>  → "Listening to <text>"  — do NOT start with "to"
  competing: <text>  → "Competing in <text>"  — do NOT start with "in"
  watching:  <text>  → "Watching <text>"      — do NOT start with "watching"
  playing:   <text>  → "Playing <text>"       — do NOT start with "playing"

LEGACY_MODE:
  False (default) — uses proper Discord ActivityType (watching/playing/listening).
                    Shows rich presence label, but only visible when clicking the profile.
  True            — prepends the type word to the text and sets everything as a plain
                    Game (Playing) activity. The full text e.g. "watching the chaos unfold"
                    shows directly in the status bar without needing a profile click.
"""

import datetime
import logging
import os
import random
from pathlib import Path

import discord
from discord.ext import commands, tasks

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

try:
    from anthropic import AsyncAnthropic as _AsyncAnthropic
except ImportError:
    _AsyncAnthropic = None

log = logging.getLogger(__name__)

# Set to True to show status text directly in the bar (e.g. "Playing watching X")
# instead of using Discord's rich ActivityType labels.
LEGACY_MODE = True

_STATUSES_FILE = Path(__file__).parent.parent.parent / "data" / "statuses.txt"

_ACTIVITY_TYPES = {
    "watching":   discord.ActivityType.watching,
    "playing":    discord.ActivityType.playing,
    "listening":  discord.ActivityType.listening,
    "competing":  discord.ActivityType.competing,
    "custom":     discord.ActivityType.custom,
}


def _lint_statuses(content: str) -> str:
    """Strip redundant connectors the AI might have added (e.g. 'to' in listening, 'in' in competing)."""
    _connectors = {"listening": "to", "competing": "in"}
    cleaned = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and ":" in stripped:
            prefix, _, text = stripped.partition(":")
            prefix = prefix.strip().lower()
            text = text.strip()
            connector = _connectors.get(prefix)
            if connector and text.lower().startswith(connector + " "):
                text = text[len(connector):].strip()
                line = f"{prefix}: {text}"
        cleaned.append(line)
    return "\n".join(cleaned)


def _load_statuses() -> list[tuple[discord.ActivityType, str]]:
    """Parse statuses.txt and return a list of (ActivityType, text) tuples."""
    results = []
    try:
        lines = _STATUSES_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        log.warning("statuses.txt not found at %s", _STATUSES_FILE)
        return [(discord.ActivityType.watching, "IM ALIVE BITCHES 💅")]

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if ":" in line:
            prefix, _, text = line.partition(":")
            prefix = prefix.strip().lower()
            text = text.strip()
            activity_type = _ACTIVITY_TYPES.get(prefix, discord.ActivityType.custom)
        else:
            activity_type = discord.ActivityType.watching
            text = line

        if text:
            results.append((activity_type, text))

    if not results:
        return [(discord.ActivityType.watching, "IM ALIVE BITCHES 💅")]

    return results


class StatusCog(commands.Cog, name="Status"):
    """Rotates the bot's presence through messages in data/statuses.txt."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._statuses: list[tuple[discord.ActivityType, str]] = []
        self._index = 0
        api_key = os.getenv("ANTHROPIC_API_KEY")
        self._ai = _AsyncAnthropic(api_key=api_key) if _AsyncAnthropic and api_key else None

    def cog_load(self):
        self._statuses = _load_statuses()
        random.shuffle(self._statuses)
        self._rotate_status.start()
        if self._ai:
            self._weekly_refresh.start()
        else:
            log.warning("StatusCog: Anthropic unavailable — weekly refresh disabled.")
        log.info("Cog Loaded. %d status(es) loaded.", len(self._statuses))

    def cog_unload(self):
        self._rotate_status.cancel()
        self._weekly_refresh.cancel()
        log.info("Cog Unloaded.")

    async def _set_next(self):
        if not self._statuses:
            return
        activity_type, text = self._statuses[self._index % len(self._statuses)]
        self._index += 1

        if activity_type == discord.ActivityType.custom:
            # custom: lines always use CustomActivity with raw text, no prefix
            activity = discord.CustomActivity(name=text)
        elif LEGACY_MODE:
            prefix = activity_type.name.lower()
            connector = {"listening": "to", "competing": "in"}.get(prefix, "")
            label = f"{prefix} {connector} {text}".replace("  ", " ") if connector else f"{prefix} {text}"
            activity = discord.CustomActivity(name=label)
        else:
            activity = discord.Activity(type=activity_type, name=text)

        await self.bot.change_presence(activity=activity)
        log.debug("Status updated (%s): %s", "legacy" if LEGACY_MODE else activity_type.name, text)

    @tasks.loop(minutes=30)
    async def _rotate_status(self):
        await self._set_next()

    @_rotate_status.before_loop
    async def _before_rotate(self):
        await self.bot.wait_until_ready()
        await self._set_next()  # fire immediately instead of waiting 30 min

    # ------------------------------------------------------------------ #
    #  Weekly AI refresh — rewrites statuses.txt with fresh messages
    # ------------------------------------------------------------------ #

    @tasks.loop(time=datetime.time(hour=23, minute=59, tzinfo=datetime.timezone.utc))
    async def _weekly_refresh(self):
        if datetime.datetime.now(datetime.timezone.utc).weekday() != 6:  # 0=Mon … 6=Sun
            return
        try:
            current = _STATUSES_FILE.read_text(encoding="utf-8")
        except Exception:
            current = ""

        prompt = (
            "You are rewriting the status messages file for a Discord bot named Pookie.\n"
            "Keep the exact same file format. The bot's personality is darkly humorous, "
            "self-aware, chronically online, and sardonic — like a witness to internet chaos "
            "who has fully accepted their fate.\n\n"
            f"Current file for reference (match this vibe and format exactly):\n{current}\n\n"
            "Generate a completely fresh set of status messages. Same categories "
            "(custom, watching, playing, listening, competing), similar count per category "
            "(~20 each), same format. Make them fresh, funny, and fit the existing humor "
            "style. Reply with ONLY the file contents — no explanation, no markdown fences.\n\n"
            "IMPORTANT — The bot runs in legacy mode, which means the code automatically builds "
            "the full display string from the type and text. Do NOT include the type word or its "
            "connector in the text — the code adds them for you:\n"
            "  listening: <text>  → displays as 'listening to <text>'  — do NOT start text with 'to'\n"
            "  competing: <text>  → displays as 'competing in <text>'  — do NOT start text with 'in'\n"
            "  watching:  <text>  → displays as 'watching <text>'      — do NOT start text with 'watching'\n"
            "  playing:   <text>  → displays as 'playing <text>'       — do NOT start text with 'playing'\n"
            "  custom:    <text>  → shown exactly as written, no prefix added — use for anything that doesn't fit the above.\n\n"
            "Examples of CORRECT entries:\n"
            "  listening: your justifications reach new heights\n"
            "  competing: in the self-inflicted wound trials\n\n"
            "Examples of WRONG entries (do NOT do this):\n"
            "  listening: to your justifications reach new heights   ← 'to' is added automatically, this creates 'listening to to ...'\n"
            "  competing: in the self-inflicted wound trials         ← 'in' is added automatically, this creates 'competing in in ...'"
        )

        try:
            resp = await self._ai.messages.create(
                model="claude-haiku-4-5",
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            new_content = resp.content[0].text.strip() if resp.content else ""
        except Exception:
            log.exception("StatusCog: weekly refresh API call failed")
            return

        if not new_content:
            log.warning("StatusCog: weekly refresh got empty response — skipping")
            return

        new_content = _lint_statuses(new_content)

        # Sanity-check: must have at least 20 non-comment lines
        valid_lines = [l for l in new_content.splitlines() if l.strip() and not l.startswith("#")]
        if len(valid_lines) < 20:
            log.warning("StatusCog: weekly refresh response too short (%d lines) — skipping", len(valid_lines))
            return

        _STATUSES_FILE.write_text(new_content, encoding="utf-8")
        new_statuses = _load_statuses()
        random.shuffle(new_statuses)
        self._statuses = new_statuses
        self._index = 0
        log.info("StatusCog: weekly refresh complete — %d new statuses loaded", len(new_statuses))

    @_weekly_refresh.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()
        # No immediate fire — let it run after the first full week

    # ------------------------------------------------------------------ #
    #  Owner commands
    # ------------------------------------------------------------------ #

    @helped_hybrid_group("status",
        name="status",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def status_group(self, ctx: commands.Context):
        await ctx.send(
            "**Status Commands** (owner only)\n"
            "`!status refresh` — Manually regenerate statuses.txt via AI"
        )

    @helped_command(status_group, "status refresh",
        name="refresh",
    )
    @commands.is_owner()
    async def refresh(self, ctx: commands.Context):
        if not self._ai:
            await ctx.send("❌ Anthropic client is unavailable — check `ANTHROPIC_API_KEY`.")
            return

        msg = await ctx.send("⏳ Regenerating statuses via AI…")
        try:
            await self._weekly_refresh()  # call the task body directly
        except Exception as e:
            log.exception("StatusCog: manual refresh failed")
            await msg.edit(content=f"❌ Refresh failed: `{e}`")
            return

        await msg.edit(
            content=f"✅ Refresh complete — **{len(self._statuses)}** status(es) loaded."
        )
        log.info("Manual status refresh triggered by %s", ctx.author)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusCog(bot))
