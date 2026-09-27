import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands, tasks

from cogs._help import documented_hybrid_subcommand, documented_hybrid_subgroup, documented_hybrid_command, documented_hybrid_group, send_command_help
from cogs._guild_cogs import is_cog_disabled

log = logging.getLogger(__name__)

# Formats tried in order when parsing a birthday input
_BIRTHDAY_FORMATS = [
    "%m%d",    # 0325
    "%m-%d",   # 03-25  /  3-25
    "%m/%d",   # 03/25  /  3/25
    "%m.%d",   # 03.25
    "%B %d",   # March 25
    "%b %d",   # Mar 25
    "%d %B",   # 25 March
    "%d %b",   # 25 Mar
]

_ORDINAL_RE = re.compile(r"(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_TIME_RE    = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", re.IGNORECASE)


def _days_until(month: int, day: int, today: datetime) -> int:
    """Return how many days until the next occurrence of this month/day."""
    year = today.year
    try:
        candidate = today.replace(year=year, month=month, day=day)
    except ValueError:
        # Feb 29 on a non-leap year — treat as Mar 1
        candidate = today.replace(year=year, month=3, day=1)

    if candidate.date() < today.date():
        # Already passed this year — move to next year
        try:
            candidate = today.replace(year=year + 1, month=month, day=day)
        except ValueError:
            candidate = today.replace(year=year + 1, month=3, day=1)

    return (candidate.date() - today.date()).days


def _parse_time(text: str) -> tuple[int, int] | None:
    """Parse a time string like '9am', '15:00', '3:30pm' into (hour, minute) UTC."""
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mins, meridiem = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if meridiem == "pm" and h != 12:
        h += 12
    if meridiem == "am" and h == 12:
        h = 0
    if not (0 <= h <= 23 and 0 <= mins <= 59):
        return None
    return h, mins


def _parse_birthday(text: str) -> datetime | None:
    """Try to parse a birthday string into a datetime (year is irrelevant).

    Accepts formats like: 03-25, 3/25, March 25, 25 March, Mar 25th, etc.
    Returns None if nothing matched.
    """
    # Strip ordinal suffixes so "25th" → "25", "1st" → "1"
    cleaned = _ORDINAL_RE.sub(r"\1", text.strip())
    for fmt in _BIRTHDAY_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    return None


class BirthdayCog(commands.Cog, name="Birthdays"):
    """Birthday tracking and daily announcements."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._pending_announce: asyncio.Task | None = None

    def cog_load(self):
        self._birthday_check.start()
        log.info("Cog Loaded.")

    def cog_unload(self):
        self._birthday_check.cancel()
        if self._pending_announce and not self._pending_announce.done():
            self._pending_announce.cancel()
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Background task — runs hourly, fires announcements at 9 AM UTC
    # ------------------------------------------------------------------ #

    async def _run_announcements(self, force: bool = False, date_str: str | None = None) -> int:
        """Post birthday announcements to every configured guild. Returns number of messages sent.

        If force=True, skips the last_announced guard so already-run guilds are retried.
        If date_str is provided (MM-DD), announces for that date instead of today UTC.
        """
        now       = datetime.now(timezone.utc)
        today_str = date_str or f"{now.month:02d}-{now.day:02d}"
        announced = 0
        for guild in self.bot.guilds:
            if is_cog_disabled(self.bot.settings, guild.id, "birthdays"):
                continue
            channel_id = self.bot.settings.get(guild.id, "birthdays", "channel")
            if not channel_id:
                continue
            channel = guild.get_channel(channel_id)
            if not channel:
                continue
            if not force and self.bot.settings.get(guild.id, "birthdays", "last_announced") == today_str:
                continue
            sent = 0
            for uid, namespaces in self.bot.settings._user_cache.items():
                bday = namespaces.get("birthdays", {}).get("date")
                if bday != today_str:
                    continue
                member = guild.get_member(uid)
                if not member or member.bot:
                    continue
                try:
                    await channel.send(
                        f"🎂 Happy Birthday, {member.mention}! Hope you have an amazing day! 🎉"
                    )
                    sent += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.warning("Birthday announce failed in guild %d: %s", guild.id, e)
            # Only mark as announced if at least one message went out
            if sent:
                await self.bot.settings.set(guild.id, "birthdays", "last_announced", today_str)
                announced += sent
        return announced

    async def _scheduled_announce(self, delay: float, reply_channel_id: int):
        await asyncio.sleep(delay)
        count   = await self._run_announcements(force=True)
        channel = self.bot.get_channel(reply_channel_id)
        if channel:
            try:
                await channel.send(f"🎂 Birthday announcements sent to **{count}** server(s).")
            except Exception:
                pass
        self._pending_announce = None

    @tasks.loop(hours=1)
    async def _birthday_check(self):
        if datetime.now(timezone.utc).hour != 9:
            return
        await self._run_announcements()

    @_birthday_check.before_loop
    async def _before_check(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------------ #
    #  Commands
    # ------------------------------------------------------------------ #

    @documented_hybrid_group(
        name="birthday",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def birthday(self, ctx: commands.Context):
        """Track and announce birthdays

        Store birthdays and post daily announcements to a configured channel.

        Usage:
            {prefix}birthday"""
        await send_command_help(ctx)

    @documented_hybrid_subcommand(birthday,
        name="set",
    )
    async def birthday_set(self, ctx: commands.Context, *, date: str):
        """Set your birthday

        Stores your month and day. The year is ignored.

        Usage:
            {prefix}birthday set <date>

        Examples:
            {prefix}birthday set 03-25
            {prefix}birthday set March 25"""
        dt = _parse_birthday(date)
        if dt is None:
            await ctx.send(
                "❌ Couldn't parse that date. Try something like `March 25`, `3/25`, or `03-25`.",
                ephemeral=True,
            )
            return

        stored = f"{dt.month:02d}-{dt.day:02d}"
        await self.bot.settings.set_user(ctx.author.id, "birthdays", "date", stored)
        pretty = dt.strftime("%B %d")
        await ctx.send(f"🎂 Birthday set to **{pretty}**!", ephemeral=True)

    @documented_hybrid_subcommand(birthday,
        name="remove",
    )
    async def birthday_remove(self, ctx: commands.Context):
        """Remove your birthday

        Deletes your stored birthday.

        Usage:
            {prefix}birthday remove"""
        if not self.bot.settings.get_user(ctx.author.id, "birthdays", "date"):
            await ctx.send("❌ You don't have a birthday set.", ephemeral=True)
            return
        await self.bot.settings.delete_user(ctx.author.id, "birthdays", "date")
        await ctx.send("✅ Your birthday has been removed.", ephemeral=True)

    @documented_hybrid_subcommand(birthday,
        name="list",
    )
    async def birthday_list(self, ctx: commands.Context):
        """List server birthdays

        Shows members with birthdays sorted by soonest upcoming.

        Usage:
            {prefix}birthday list"""
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        now = datetime.now(timezone.utc)
        entries = []
        for uid, namespaces in self.bot.settings._user_cache.items():
            bday = namespaces.get("birthdays", {}).get("date")
            if not bday:
                continue
            member = ctx.guild.get_member(uid)
            if not member or member.bot:
                continue
            try:
                dt = datetime.strptime(bday, "%m-%d")
            except ValueError:
                continue
            days = _days_until(dt.month, dt.day, now)
            entries.append((days, dt.month, dt.day, member.display_name))

        if not entries:
            await ctx.send("No birthdays set yet! Use `!birthday set March 25` (or any date format) to add yours.")
            return

        entries.sort()

        lines = []
        for days, month, day, name in entries:
            pretty = datetime.strptime(f"{month:02d}-{day:02d}", "%m-%d").strftime("%B %d")
            if days == 0:
                lines.append(f"🎂 **{name}** — {pretty}  🎉 **Today!**")
            elif days < 31:
                day_word = "day" if days == 1 else "days"
                lines.append(f"**{name}** — {pretty} *(in {days} {day_word})*")
            else:
                lines.append(f"**{name}** — {pretty}")

        embed = discord.Embed(
            title="🎂 Server Birthdays",
            description="\n".join(lines),
            color=discord.Color.from_rgb(255, 105, 180),
        )
        await ctx.send(embed=embed)

    @documented_hybrid_subcommand(birthday,
        name="setchannel",
    )
    async def birthday_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set birthday announcement channel

        Sets where birthday announcements are posted. Bot owner only.

        Usage:
            {prefix}birthday setchannel <channel>

        Examples:
            {prefix}birthday setchannel #birthdays"""
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ Only the bot owner can set the birthday channel.", ephemeral=True)
            return
        await self.bot.settings.set(ctx.guild.id, "birthdays", "channel", channel.id)
        await ctx.send(f"✅ Birthday announcements will post in {channel.mention}.", ephemeral=True)

    @documented_hybrid_subcommand(birthday,
        name="announce",
    )
    async def birthday_announce(self, ctx: commands.Context, *, time: str = ""):
        """Trigger birthday announcements

        Runs announcements now, for a date override, or schedules a UTC time.

        Usage:
            {prefix}birthday announce [time|MM-DD]

        Examples:
            {prefix}birthday announce
            {prefix}birthday announce 04-04
            {prefix}birthday announce 9am"""
        if not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ Only the bot owner can use this command.", ephemeral=True)
            return

        if self._pending_announce and not self._pending_announce.done():
            await ctx.send("⏳ An announcement is already scheduled. Wait for it to fire first.", ephemeral=True)
            return

        if not time:
            count = await self._run_announcements(force=True)
            await ctx.send(f"🎂 Birthday announcements sent to **{count}** member(s).")
            return

        # Check if it's a date override (MM-DD format)
        try:
            dt = datetime.strptime(time.strip(), "%m-%d")
            date_str = f"{dt.month:02d}-{dt.day:02d}"
            count = await self._run_announcements(force=True, date_str=date_str)
            await ctx.send(f"🎂 Birthday announcements for **{dt.strftime('%B %d')}** sent to **{count}** member(s).")
            return
        except ValueError:
            pass

        # Otherwise treat as a time to schedule
        parsed = _parse_time(time)
        if parsed is None:
            await ctx.send(
                "❌ Couldn't parse that. Use a date like `04-04`, a time like `9am` or `15:30` (UTC).",
                ephemeral=True,
            )
            return

        h, mins  = parsed
        now      = datetime.now(timezone.utc)
        target   = now.replace(hour=h, minute=mins, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)

        delay = (target - now).total_seconds()
        loop  = asyncio.get_event_loop()
        self._pending_announce = loop.create_task(
            self._scheduled_announce(delay, ctx.channel.id)
        )
        t_str = target.strftime("%H:%M UTC")
        await ctx.send(f"⏰ Birthday announcements scheduled for **{t_str}** (in {int(delay // 60)} min).")

    # Keep the historic plural command as a complete hybrid command tree.
    # Discord application commands do not support prefix-style aliases, so
    # explicit forwarding subcommands are required for /birthdays parity.

    @documented_hybrid_group(
        name="birthdays",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def birthdays(self, ctx: commands.Context):
        """Track and announce birthdays

        Plural command path for the birthday tools.

        Usage:
            {prefix}birthdays"""
        await send_command_help(ctx)

    @documented_hybrid_subcommand(birthdays, name="set")
    async def birthdays_set(self, ctx: commands.Context, *, date: str):
        """Set your birthday

        Stores your month and day through the plural birthday command path.

        Usage:
            {prefix}birthdays set <date>

        Arguments:
            date: Birthday date such as March 25 or 03-25

        Examples:
            {prefix}birthdays set March 25"""
        await ctx.invoke(self.birthday_set, date=date)

    @documented_hybrid_subcommand(birthdays, name="remove")
    async def birthdays_remove(self, ctx: commands.Context):
        """Remove your birthday

        Deletes your stored birthday through the plural command path.

        Usage:
            {prefix}birthdays remove"""
        await ctx.invoke(self.birthday_remove)

    @documented_hybrid_subcommand(birthdays, name="list")
    async def birthdays_list(self, ctx: commands.Context):
        """List server birthdays

        Shows upcoming birthdays through the plural command path.

        Usage:
            {prefix}birthdays list"""
        await ctx.invoke(self.birthday_list)

    @documented_hybrid_subcommand(birthdays, name="setchannel")
    async def birthdays_setchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set birthday announcement channel

        Sets the announcement channel through the plural command path.

        Usage:
            {prefix}birthdays setchannel <channel>

        Arguments:
            channel: Text channel for birthday announcements

        Examples:
            {prefix}birthdays setchannel #birthdays"""
        await ctx.invoke(self.birthday_setchannel, channel=channel)

    @documented_hybrid_subcommand(birthdays, name="announce")
    async def birthdays_announce(self, ctx: commands.Context, *, time: str = ""):
        """Trigger birthday announcements

        Runs or schedules announcements through the plural command path.

        Usage:
            {prefix}birthdays announce [time|MM-DD]

        Arguments:
            time: Optional UTC time or MM-DD date override

        Examples:
            {prefix}birthdays announce
            {prefix}birthdays announce 9am"""
        await ctx.invoke(self.birthday_announce, time=time)


async def setup(bot: commands.Bot):
    await bot.add_cog(BirthdayCog(bot))
