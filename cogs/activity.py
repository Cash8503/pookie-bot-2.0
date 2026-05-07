"""
activity.py — Activity Tracker cog

Tracks per-guild activity: messages sent, voice time, emoji reactions.
Data is stored under guild settings namespace "activity" with month-keyed keys
so old months naturally expire without any cleanup task.

Key patterns:
  msg:{YYYY-MM}:{user_id}   → int (message count)
  voice:{YYYY-MM}:{user_id} → int (seconds in voice)
  emoji:{YYYY-MM}:{user_id} → dict[str, int] (emoji → count)

Emoji tracking counts both message content and reactions.
Custom emojis are stored as "<:name:id>" so they render in Discord embeds.

NOTE: on_message, on_reaction_add, and on_voice_state_update write directly to
s._cache instead of calling await s.set() to avoid hammering SQLite on every event.
The SettingsManager's periodic flush_all() (every 5 min) handles persistence.
"""

import asyncio
import logging
import re
import time
from collections import Counter
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Score weights
# ------------------------------------------------------------------ #
SCORE_PER_MESSAGE   = 1
SCORE_PER_VOI_MIN   = .25    # per minute in voice
SCORE_PER_EMOJI     = 0.5  # per emoji use

EMOJI_ICON = "<:Happy:1330182022991315024>"

# ------------------------------------------------------------------ #
#  Emoji parsing (message content)
# ------------------------------------------------------------------ #
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")  # full tag, e.g. <:pookie:12345>
_UNICODE_EMOJI_RE = re.compile(
    r"[\U0001F1E0-\U0001F1FF]{2}"           # flag pairs (🇬🇧, 🇺🇸, etc.)
    r"|[\U0001F300-\U0001F5FF"
    r"\U0001F600-\U0001F64F"
    r"\U0001F680-\U0001F6FF"
    r"\U0001F700-\U0001F77F"
    r"\U0001F780-\U0001F7FF"
    r"\U0001F800-\U0001F8FF"
    r"\U0001F900-\U0001F9FF"
    r"\U0001FA00-\U0001FA6F"
    r"\U0001FA70-\U0001FAFF"
    r"\U00002600-\U000026FF"
    r"\U00002700-\U000027BF]",              # single emoji char
    flags=re.UNICODE,
)


def _extract_emojis(text: str) -> list[str]:
    return _CUSTOM_EMOJI_RE.findall(text) + _UNICODE_EMOJI_RE.findall(text)


def _match_emoji_key(combined: Counter, raw: str) -> str | None:
    """Find the canonical stored emoji key that best matches user input.

    Tries exact match first, then name-based match for custom emojis so that
    typing "pookie" or ":pookie:" matches the stored key "<:pookie:123456>".
    """
    if raw in combined:
        return raw
    # Strip surrounding syntax and grab just the name
    name = raw.strip("<> ").strip(":").split(":")[0].lower()
    for key in combined:
        if key.startswith("<") and ":" in key:
            # "<:name:id>" or "<a:name:id>" → name sits after the first ":"
            key_name = key.lstrip("<").lstrip("a").lstrip(":").split(":")[0].lower()
            if key_name == name:
                return key
    return None


def _reaction_emoji_str(emoji) -> str:
    """Return a renderable string for a reaction emoji.

    Custom guild emojis → "<:name:id>" (or "<a:name:id>" for animated).
    Unicode / standard emojis → the raw character(s).
    """
    if isinstance(emoji, str):
        return emoji  # already a unicode char
    # discord.Emoji or discord.PartialEmoji
    if getattr(emoji, "id", None):
        prefix = "a" if getattr(emoji, "animated", False) else ""
        return f"<{prefix}:{emoji.name}:{emoji.id}>"
    # fallback — PartialEmoji with no id (unicode)
    return str(emoji)


def _month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _score(messages: int, voice_seconds: int, emoji_total: int) -> float:
    return (
        messages * SCORE_PER_MESSAGE
        + (voice_seconds // 60) * SCORE_PER_VOI_MIN
        + emoji_total * SCORE_PER_EMOJI
    )


def _fmt_voice(seconds: int) -> str:
    h = seconds // 3600
    m = (seconds % 3600) // 60
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


class _LeaderboardPaginator(discord.ui.View):
    PER_PAGE = 15

    def __init__(self, lines: list[str], title: str, footer: str = "", color: int = 0x5865F2):
        super().__init__(timeout=120)
        self.lines   = lines
        self.title   = title
        self.footer  = footer
        self.color   = color
        self.page    = 0
        self.total   = max(1, (len(lines) + self.PER_PAGE - 1) // self.PER_PAGE)
        self.message: discord.Message | None = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page == 0
        self.next_btn.disabled = self.page >= self.total - 1

    def _build_embed(self) -> discord.Embed:
        start = self.page * self.PER_PAGE
        chunk = self.lines[start : start + self.PER_PAGE]
        embed = discord.Embed(title=self.title, description="\n".join(chunk), color=self.color)
        footer_text = f"Page {self.page + 1}/{self.total}"
        if self.footer:
            footer_text += f" • {self.footer}"
        embed.set_footer(text=footer_text)
        return embed

    async def on_timeout(self):
        if self.message:
            try:
                for item in self.children:
                    item.disabled = True
                await self.message.edit(view=self)
            except Exception:
                pass

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self._build_embed(), view=self)


class ActivityCog(commands.Cog, name="Activity"):
    """Track messages, voice time, and emoji usage per server."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # (guild_id, user_id) → monotonic join time
        self._voice_joins: dict[tuple[int, int], float] = {}

    def cog_load(self):
        # Re-register anyone already sitting in a voice channel so a bot
        # restart/reload doesn't erase their in-progress session.
        now      = time.monotonic()
        wall_now = datetime.now(timezone.utc)
        resumed  = 0
        for guild in self.bot.guilds:
            for channel in guild.voice_channels:
                for member in channel.members:
                    if member.bot:
                        continue
                    joined_at = getattr(member.voice, "joined_at", None)
                    if joined_at:
                        elapsed = (wall_now - joined_at).total_seconds()
                        self._voice_joins[(guild.id, member.id)] = now - elapsed
                    else:
                        self._voice_joins[(guild.id, member.id)] = now
                    resumed += 1
        log.info("Cog Loaded. Resumed %d active voice session(s).", resumed)

    def cog_unload(self):
        # Best-effort: flush any open voice sessions
        if self._voice_joins:
            month = _month_key()
            s = self.bot.settings
            now = time.monotonic()
            for (guild_id, user_id), join_ts in self._voice_joins.items():
                elapsed = int(now - join_ts)
                if elapsed < 1:
                    continue
                ns = s._cache.setdefault(guild_id, {}).setdefault("activity", {})
                key = f"voice:{month}:{user_id}"
                ns[key] = ns.get(key, 0) + elapsed
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Listeners
    # ------------------------------------------------------------------ #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return

        month = _month_key()
        s     = self.bot.settings
        uid   = message.author.id
        gid   = message.guild.id
        ns    = s._cache.setdefault(gid, {}).setdefault("activity", {})

        # Message count
        msg_key     = f"msg:{month}:{uid}"
        ns[msg_key] = ns.get(msg_key, 0) + 1

        # Emoji count (message content)
        emojis = _extract_emojis(message.content or "")
        if emojis:
            e_key = f"emoji:{month}:{uid}"
            if e_key not in ns:
                ns[e_key] = {}
            for e in emojis:
                ns[e_key][e] = ns[e_key].get(e, 0) + 1

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.Member | discord.User):
        if getattr(user, "bot", False):
            return
        guild = getattr(reaction.message, "guild", None)
        if not guild:
            return

        month     = _month_key()
        s         = self.bot.settings
        ns        = s._cache.setdefault(guild.id, {}).setdefault("activity", {})
        emoji_key = f"emoji:{month}:{user.id}"
        emoji_str = _reaction_emoji_str(reaction.emoji)
        emoji_dict = dict(ns.get(emoji_key) or {})
        emoji_dict[emoji_str] = emoji_dict.get(emoji_str, 0) + 1
        ns[emoji_key] = emoji_dict

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        key = (member.guild.id, member.id)

        joined  = before.channel is None and after.channel is not None
        left    = before.channel is not None and after.channel is None
        moved   = before.channel is not None and after.channel is not None and before.channel != after.channel

        if joined:
            self._voice_joins[key] = time.monotonic()

        elif moved:
            # Accumulate time in the old channel, start fresh timer for the new one
            join_ts = self._voice_joins.get(key)
            if join_ts is not None:
                elapsed = int(time.monotonic() - join_ts)
                if elapsed >= 1:
                    month = _month_key()
                    s     = self.bot.settings
                    ns    = s._cache.setdefault(member.guild.id, {}).setdefault("activity", {})
                    v_key = f"voice:{month}:{member.id}"
                    ns[v_key] = ns.get(v_key, 0) + elapsed
            self._voice_joins[key] = time.monotonic()

        elif left:
            join_ts = self._voice_joins.pop(key, None)
            if join_ts is None:
                return  # bot restarted mid-session — skip
            elapsed = int(time.monotonic() - join_ts)
            if elapsed < 1:
                return

            month = _month_key()
            s     = self.bot.settings
            ns    = s._cache.setdefault(member.guild.id, {}).setdefault("activity", {})
            v_key = f"voice:{month}:{member.id}"
            ns[v_key] = ns.get(v_key, 0) + elapsed

    # ------------------------------------------------------------------ #
    #  Commands
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="activity",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Server activity stats and leaderboard",
        help=(
            "Track and display server activity stats.\n\n"
            "Subcommands:\n"
            "  summary [all]        — Top 10 snapshot of all main categories\n"
            "  leaderboard [all]    — Top 10 most active members (this month or all time)\n"
            "  stats [@member]      — Individual stats for a member\n"
            "  emojis [all]         — Most used emojis (this month or all time)\n"
            "  emoji <emoji> [all]  — Who uses a specific emoji the most\n\n"
            "Activity score = messages + (voice minutes × 5) + (emoji uses × 0.5)"
        ),
    )
    async def activity(self, ctx: commands.Context):
        await ctx.send(
            "**Activity Commands**\n"
            "`!activity summary [all]` — Top 10 snapshot of all categories\n"
            "`!activity leaderboard [all]` — Top 10 this month (or all time)\n"
            "`!activity stats [@member]` — Individual stats\n"
            "`!activity emojis [all]` — Most used emojis (or all time)\n"
            "`!activity emoji <emoji> [all]` — Who uses a specific emoji the most\n\n"
            "Run `!help activity <subcommand>` for details."
        )

    @activity.command(
        name="leaderboard",
        aliases=["top"],
        brief="Top 10 most active members",
        help=(
            "Shows the top 10 most active members ranked by activity score.\n\n"
            "Usage:\n"
            "  !activity leaderboard       — this month\n"
            "  !activity leaderboard all   — all time"
        ),
    )
    @app_commands.describe(period="'all' for lifetime stats, leave blank for this month")
    async def leaderboard(self, ctx: commands.Context, period: str = "month"):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        all_time = period.lower() in ("all", "lifetime", "alltime")
        month = _month_key()
        s     = self.bot.settings
        ns    = s._cache.get(ctx.guild.id, {}).get("activity", {})

        # Collect all relevant user IDs
        user_ids: set[int] = set()
        for k in ns:
            parts = k.split(":")
            if len(parts) == 3 and (all_time or parts[1] == month):
                try:
                    user_ids.add(int(parts[2]))
                except ValueError:
                    pass

        period_str = "all time" if all_time else "this month"
        if not user_ids:
            await ctx.send(f"No activity recorded {period_str} yet.")
            return

        rows = []
        for uid in user_ids:
            member = ctx.guild.get_member(uid)
            if member is None:
                continue
            if all_time:
                msgs  = sum(v for k, v in ns.items() if k.startswith("msg:") and k.endswith(f":{uid}"))
                voice = sum(v for k, v in ns.items() if k.startswith("voice:") and k.endswith(f":{uid}"))
                combined: Counter = Counter()
                for k, v in ns.items():
                    if k.startswith("emoji:") and k.endswith(f":{uid}") and isinstance(v, dict):
                        combined.update(v)
                e_total = sum(combined.values())
            else:
                msgs    = ns.get(f"msg:{month}:{uid}", 0)
                voice   = ns.get(f"voice:{month}:{uid}", 0)
                e_total = sum((ns.get(f"emoji:{month}:{uid}") or {}).values())
            sc = _score(msgs, voice, e_total)
            rows.append((member.display_name, sc, msgs, voice, e_total))

        if not rows:
            await ctx.send(f"No active members found {period_str}.")
            return

        rows.sort(key=lambda r: r[1], reverse=True)

        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (name, sc, msgs, voice, emojis) in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i+1}.`"
            lines.append(
                f"{prefix} **{name}** — {sc:.0f} pts "
                f"({msgs} msgs · {_fmt_voice(voice)} VC · {emojis} reactions)"
            )

        title  = f"📊 Activity Leaderboard — {'All Time' if all_time else month}"
        footer = "Score = messages + (VC mins × 5) + (emoji reactions × 0.5)"
        view   = _LeaderboardPaginator(lines, title, footer)
        view.message = await ctx.send(embed=view._build_embed(), view=view if view.total > 1 else None)

    @activity.command(
        name="stats",
        brief="View activity stats for a member",
        help=(
            "Shows this month's activity stats for a member.\n\n"
            "Usage:\n"
            "  !activity stats          — your own stats\n"
            "  !activity stats @someone — another member's stats"
        ),
    )
    @app_commands.describe(member="Member to check (leave blank for yourself)")
    async def stats(self, ctx: commands.Context, member: discord.Member = None):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        target = member or ctx.author
        month  = _month_key()
        s      = self.bot.settings
        ns     = s._cache.get(ctx.guild.id, {}).get("activity", {})

        msgs   = ns.get(f"msg:{month}:{target.id}", 0)
        voice  = ns.get(f"voice:{month}:{target.id}", 0)
        emojis = dict(ns.get(f"emoji:{month}:{target.id}") or {})

        e_total   = sum(emojis.values())
        sc        = _score(msgs, voice, e_total)
        top_emoji = sorted(emojis.items(), key=lambda x: x[1], reverse=True)[:3]

        # All-time totals (sum across all months)
        all_msgs  = sum(v for k, v in ns.items() if k.startswith("msg:") and k.endswith(f":{target.id}"))
        all_voice = sum(v for k, v in ns.items() if k.startswith("voice:") and k.endswith(f":{target.id}"))
        all_emoji: Counter = Counter()
        for k, v in ns.items():
            if k.startswith("emoji:") and k.endswith(f":{target.id}") and isinstance(v, dict):
                all_emoji.update(v)
        all_e_total = sum(all_emoji.values())
        all_sc      = _score(all_msgs, all_voice, all_e_total)

        emoji_str = "  ".join(f"{e} ×{c}" for e, c in top_emoji) or "None"

        embed = discord.Embed(
            title=f"📈 {target.display_name}'s Activity",
            color=0x5865F2,
        )
        embed.add_field(
            name=f"This Month ({month})",
            value=(
                f"Messages: **{msgs}**\n"
                f"Voice time: **{_fmt_voice(voice)}**\n"
                f"Reactions: **{e_total}**\n"
                f"Score: **{sc:.0f} pts**"
            ),
            inline=True,
        )
        embed.add_field(
            name="All Time",
            value=(
                f"Messages: **{all_msgs}**\n"
                f"Voice time: **{_fmt_voice(all_voice)}**\n"
                f"Reactions: **{all_e_total}**\n"
                f"Score: **{all_sc:.0f} pts**"
            ),
            inline=True,
        )
        embed.add_field(name=f"Top Reactions ({month})", value=emoji_str, inline=False)
        embed.set_thumbnail(url=target.display_avatar.url)
        await ctx.send(embed=embed)

    @activity.command(
        name="emojis",
        brief="Most used emoji reactions",
        help=(
            "Shows the top 10 most used emoji reactions server-wide.\n\n"
            "Usage:\n"
            "  !activity emojis       — this month\n"
            "  !activity emojis all   — all time"
        ),
    )
    @app_commands.describe(period="'all' for lifetime stats, leave blank for this month")
    async def emojis(self, ctx: commands.Context, period: str = "month"):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        all_time = period.lower() in ("all", "lifetime", "alltime")
        month  = _month_key()
        s      = self.bot.settings
        ns     = s._cache.get(ctx.guild.id, {}).get("activity", {})

        combined: Counter = Counter()
        for k, v in ns.items():
            if not (k.startswith("emoji:") and isinstance(v, dict)):
                continue
            if all_time or k.startswith(f"emoji:{month}:"):
                combined.update(v)

        period_str = "all time" if all_time else "this month"
        if not combined:
            await ctx.send(f"No emoji reactions recorded {period_str} yet.")
            return

        top   = combined.most_common()
        lines = [f"`{i+1}.` {e} — **{c}** uses" for i, (e, c) in enumerate(top)]

        title  = f"{EMOJI_ICON} Top Reactions — {'All Time' if all_time else month}"
        footer = "Custom emojis from other servers may appear as empty squares or not render at all."
        view   = _LeaderboardPaginator(lines, title, footer)
        view.message = await ctx.send(embed=view._build_embed(), view=view if view.total > 1 else None)


    @activity.command(
        name="emoji",
        brief="Who uses a specific emoji the most",
        help=(
            "Shows the top 10 users of a specific emoji.\n\n"
            "Usage:\n"
            "  !activity emoji 😭           — unicode emoji, this month\n"
            "  !activity emoji :pookie:     — custom emoji by name, this month\n"
            "  !activity emoji :pookie: all — all time"
        ),
    )
    @app_commands.describe(emoji="The emoji to look up", period="'all' for lifetime, blank for this month")
    async def emoji_who(self, ctx: commands.Context, emoji: str, period: str = "month"):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        all_time   = period.lower() in ("all", "lifetime", "alltime")
        month      = _month_key()
        period_str = "All Time" if all_time else month
        s          = self.bot.settings
        ns         = s._cache.get(ctx.guild.id, {}).get("activity", {})

        # Build a combined counter of all emojis across the relevant period
        # so we can fuzzy-match the user's input against known keys
        all_emojis: Counter = Counter()
        for k, v in ns.items():
            if k.startswith("emoji:") and isinstance(v, dict):
                if all_time or k.startswith(f"emoji:{month}:"):
                    all_emojis.update(v)

        matched = _match_emoji_key(all_emojis, emoji)
        if matched is None:
            await ctx.send(
                f"❌ Emoji not found in activity data for {period_str.lower()}.",
                ephemeral=True,
            )
            return

        # Tally per-user counts for the matched emoji
        user_counts: dict[int, int] = {}
        for k, v in ns.items():
            if not (k.startswith("emoji:") and isinstance(v, dict)):
                continue
            if not (all_time or k.startswith(f"emoji:{month}:")):
                continue
            uid   = int(k.split(":")[-1])
            count = v.get(matched, 0)
            if count:
                user_counts[uid] = user_counts.get(uid, 0) + count

        rows = []
        for uid, count in user_counts.items():
            member = ctx.guild.get_member(uid)
            if member:
                rows.append((member.display_name, count))

        if not rows:
            await ctx.send(f"No usage found for that emoji {period_str.lower()}.")
            return

        rows.sort(key=lambda r: r[1], reverse=True)
        medals = ["🥇", "🥈", "🥉"]
        lines  = []
        for i, (name, count) in enumerate(rows):
            prefix = medals[i] if i < 3 else f"`{i+1}.`"
            lines.append(f"{prefix} **{name}** — {count:,} uses")

        title = f"👑 Who uses {matched} the most — {period_str}"
        view  = _LeaderboardPaginator(lines, title)
        view.message = await ctx.send(embed=view._build_embed(), view=view if view.total > 1 else None)

    @activity.command(
        name="summary",
        brief="Top 10 snapshot of all main categories",
        help=(
            "Shows top 10 for messages, voice time, and emoji usage side by side.\n\n"
            "Usage:\n"
            "  !activity summary       — this month\n"
            "  !activity summary all   — all time"
        ),
    )
    @app_commands.describe(period="'all' for lifetime stats, leave blank for this month")
    async def summary(self, ctx: commands.Context, period: str = "month"):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        all_time = period.lower() in ("all", "lifetime", "alltime")
        month    = _month_key()
        s        = self.bot.settings
        ns       = s._cache.get(ctx.guild.id, {}).get("activity", {})
        period_label = "All Time" if all_time else month

        # Collect per-user totals
        user_msgs:  dict[int, int] = {}
        user_voice: dict[int, int] = {}
        user_emoji: dict[int, int] = {}
        emoji_totals: Counter = Counter()

        for k, v in ns.items():
            parts = k.split(":")
            if len(parts) != 3:
                continue
            kind, mon, uid_str = parts
            if not (all_time or mon == month):
                continue
            try:
                uid = int(uid_str)
            except ValueError:
                continue
            if kind == "msg":
                user_msgs[uid]  = user_msgs.get(uid, 0) + v
            elif kind == "voice":
                user_voice[uid] = user_voice.get(uid, 0) + v
            elif kind == "emoji" and isinstance(v, dict):
                user_emoji[uid] = user_emoji.get(uid, 0) + sum(v.values())
                emoji_totals.update(v)

        medals = ["🥇", "🥈", "🥉"]

        def _fmt_rows(uid_score: list[tuple[int, int | float]], fmt) -> str:
            lines = []
            for i, (uid, val) in enumerate(uid_score[:10]):
                member = ctx.guild.get_member(uid)
                name   = member.display_name if member else f"<{uid}>"
                prefix = medals[i] if i < 3 else f"`{i+1}.`"
                lines.append(f"{prefix} **{name}** — {fmt(val)}")
            return "\n".join(lines) or "No data."

        top_msgs  = sorted(user_msgs.items(),  key=lambda x: x[1], reverse=True)
        top_voice = sorted(user_voice.items(), key=lambda x: x[1], reverse=True)
        top_emoji = sorted(user_emoji.items(), key=lambda x: x[1], reverse=True)
        top_react = emoji_totals.most_common(10)

        embed = discord.Embed(title=f"📊 Activity Summary — {period_label}", color=0x5865F2)
        embed.add_field(name="💬 Most Messages", value=_fmt_rows(top_msgs, lambda v: f"{v:,} msgs"), inline=False)
        embed.add_field(name="🎙️ Most Voice Time", value=_fmt_rows(top_voice, _fmt_voice), inline=False)
        embed.add_field(name=f"{EMOJI_ICON} Most Emojis Used", value=_fmt_rows(top_emoji, lambda v: f"{v:,} emojis"), inline=False)

        react_lines = "\n".join(
            f"{medals[i] if i < 3 else f'`{i+1}.`'} {e} — **{c:,}** uses"
            for i, (e, c) in enumerate(top_react)
        ) or "No data."
        embed.add_field(name="🏆 Most Used Emojis", value=react_lines, inline=False)

        await ctx.send(embed=embed)

    @activity.command(
        name="backfill",
        brief="Rebuild activity stats from full channel history",
        help=(
            "Scans every accessible text channel and rebuilds activity data from scratch.\n\n"
            "⚠️ This REPLACES all existing activity data.\n"
            "It will take a long time on active servers — reactions are especially slow\n"
            "because Discord requires a separate API call per reaction type per message.\n\n"
            "Usage:\n"
            "  !activity backfill             — show this warning\n"
            "  !activity backfill confirm     — actually run it\n\n"
            "Requires Manage Server permission."
        ),
    )
    @commands.has_permissions(manage_guild=True)
    async def backfill(self, ctx: commands.Context, confirm: str = ""):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        readable = [
            c for c in ctx.guild.text_channels
            if c.permissions_for(ctx.guild.me).read_message_history
        ]

        if confirm.lower() != "confirm":
            await ctx.send(
                "⚠️ **Activity Backfill**\n"
                "This will scan the full history of every channel and **replace** all existing "
                "activity data with counts from Discord's history.\n\n"
                f"Accessible channels: **{len(readable)}**\n"
                "Voice time cannot be recovered from history and will be reset to 0.\n\n"
                "Run `!activity backfill confirm` to proceed.",
                ephemeral=True,
            )
            return

        status = await ctx.send(
            f"⏳ Starting backfill across **{len(readable)}** channel(s)...\n"
            "I'll update this message with progress every few seconds."
        )

        start_time = time.monotonic()
        s   = self.bot.settings
        gid = ctx.guild.id

        # Wipe existing activity data and start fresh
        s._cache.setdefault(gid, {})["activity"] = {}
        ns = s._cache[gid]["activity"]

        total_msgs      = 0
        total_emojis    = 0
        total_reactions = 0
        channels_done   = 0
        last_edit       = time.monotonic()

        async def _progress():
            nonlocal last_edit
            now = time.monotonic()
            if now - last_edit < 5.0:
                return
            last_edit = now
            try:
                await status.edit(content=(
                    f"⏳ Backfilling... **{channels_done}/{len(readable)}** channels done\n"
                    f"📨 {total_msgs:,} messages · 🔤 {total_emojis:,} msg emojis · {EMOJI_ICON} {total_reactions:,} reactions"
                ))
            except Exception:
                pass

        try:
            for channel in readable:
                try:
                    async for message in channel.history(limit=None, oldest_first=True):
                        if message.author.bot:
                            continue

                        month   = message.created_at.strftime("%Y-%m")
                        uid     = message.author.id
                        msg_key = f"msg:{month}:{uid}"
                        ns[msg_key] = ns.get(msg_key, 0) + 1
                        total_msgs += 1

                        # Message content emojis
                        emojis = _extract_emojis(message.content or "")
                        if emojis:
                            e_key = f"emoji:{month}:{uid}"
                            if e_key not in ns:
                                ns[e_key] = {}
                            for e in emojis:
                                ns[e_key][e] = ns[e_key].get(e, 0) + 1
                            total_emojis += len(emojis)

                        # Reactions — retry on transient 5xx, abort on repeated failure
                        for reaction in message.reactions:
                            emoji_str = _reaction_emoji_str(reaction.emoji)
                            for attempt in range(3):
                                try:
                                    async for user in reaction.users():
                                        if getattr(user, "bot", False):
                                            continue
                                        e_key = f"emoji:{month}:{user.id}"
                                        if e_key not in ns:
                                            ns[e_key] = {}
                                        ns[e_key][emoji_str] = ns[e_key].get(emoji_str, 0) + 1
                                        total_reactions += 1
                                    break  # success
                                except discord.errors.DiscordServerError:
                                    if attempt == 2:
                                        raise  # give up, abort the whole backfill
                                    await asyncio.sleep(2 ** attempt)

                        await _progress()

                except discord.Forbidden:
                    log.warning("Backfill: no access to #%s", channel.name)
                # all other exceptions bubble up to the outer try

                channels_done += 1
                await _progress()

        except Exception as exc:
            elapsed   = time.monotonic() - start_time
            mins, sec = divmod(int(elapsed), 60)
            log.exception("Backfill: aborted after %.1fs in guild=%d", elapsed, gid)
            await status.edit(content=(
                f"❌ **Backfill aborted!**\n"
                f"📨 {total_msgs:,} messages · 🔤 {total_emojis:,} msg emojis · {EMOJI_ICON} {total_reactions:,} reactions counted before failure\n"
                f"Completed **{channels_done}/{len(readable)}** channel(s)\n"
                f"Error: `{exc}`"
            ))
            await ctx.send(
                f"{ctx.author.mention} backfill failed after **{mins}m {sec}s** — see above for details."
            )
            return

        elapsed   = time.monotonic() - start_time
        mins, sec = divmod(int(elapsed), 60)
        mps       = total_msgs / elapsed if elapsed > 0 else 0

        log.info(
            "Backfill complete: guild=%d channels=%d msgs=%d emojis=%d reactions=%d elapsed=%.1fs",
            gid, channels_done, total_msgs, total_emojis, total_reactions, elapsed,
        )
        await status.edit(content=(
            f"✅ **Backfill complete!**\n"
            f"📨 {total_msgs:,} messages · 🔤 {total_emojis:,} msg emojis · {EMOJI_ICON} {total_reactions:,} reactions\n"
            f"Scanned **{channels_done}** channel(s) — data saves automatically within 5 minutes."
        ))
        await ctx.send(
            f"{ctx.author.mention} backfill finished in "
            f"**{mins}m {sec}s** — {mps:.1f} msg/s"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ActivityCog(bot))
