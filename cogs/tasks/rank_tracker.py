"""Background Overwatch rank checks with persistent account lifecycle state.

Public profiles that have succeeded before stay on the normal 15-minute poll.
Private or missing profiles are retried once per day. Linked users who no longer
share any guild with Pookie are archived without deleting their Battletag or
rank snapshot, then automatically restored if they join a shared guild again.
"""

from __future__ import annotations

import asyncio
import logging
import time

import aiohttp
import discord
from discord.ext import commands, tasks

from cogs._guild_cogs import is_cog_disabled
from cogs._rank_tracking import (
    STATUS_ARCHIVED,
    STATUS_UNAVAILABLE,
    archive_account,
    check_is_due,
    get_tracker_state,
    mark_attempt,
    mark_success,
    mark_unavailable,
    reactivate_account,
)
from cogs.ow_picker import _battletag_to_player_id, _fmt_rank, fetch_player


log = logging.getLogger(__name__)

ROLES = ("tank", "damage", "support", "open")

ROLE_LABEL = {
    "tank": "Tank",
    "damage": "Damage",
    "support": "Support",
    "open": "Open Queue",
}

RANK_ORDER = {
    "bronze": 0,
    "silver": 1,
    "gold": 2,
    "platinum": 3,
    "diamond": 4,
    "master": 5,
    "grandmaster": 6,
    "champion": 7,
}

# Small spacing between real API calls keeps the OverFast service comfortable.
FETCH_DELAY = 2.0


def _plain_rank(rank: dict | None) -> str:
    """Plain-text rank string for logging without Discord emoji."""

    if not rank:
        return "Unranked"
    division = rank.get("division", "?").capitalize()
    tier = rank.get("tier")
    if tier and rank.get("division", "").lower() not in ("grandmaster", "champion"):
        return f"{division} {tier}"
    return division


def _rank_score(rank: dict | None) -> int | None:
    """Numeric rank score for comparisons; higher is better."""

    if rank is None:
        return None
    base = RANK_ORDER.get(rank.get("division", "").lower(), -1) * 10
    tier = rank.get("tier") or 0
    return base + (3 - tier)


def _extract_snapshot(data: dict) -> dict:
    """Pull the current PC ranks from a player API response."""

    competitive = (
        data.get("summary", {}).get("competitive", {}).get("pc", {})
    ) or {}
    result = {}
    for role in ROLES:
        rank = competitive.get(role)
        result[role] = (
            {"division": rank.get("division"), "tier": rank.get("tier")}
            if rank
            else None
        )
    return result


def _compare_snapshots(
    old: dict | None,
    new: dict,
) -> list[tuple[str, str, dict | None, dict | None]]:
    """Return placed, up, and down changes while ignoring unranked transitions."""

    changes = []
    old = old or {}
    for role in ROLES:
        old_rank = old.get(role)
        new_rank = new.get(role)
        if old_rank == new_rank:
            continue
        old_score = _rank_score(old_rank)
        new_score = _rank_score(new_rank)
        if old_score is None and new_score is not None:
            changes.append((role, "placed", old_rank, new_rank))
        elif old_score is not None and new_score is not None:
            if new_score > old_score:
                changes.append((role, "up", old_rank, new_rank))
            elif new_score < old_score:
                changes.append((role, "down", old_rank, new_rank))
    return changes


def _build_embed(display_name: str, changes: list) -> discord.Embed:
    lines = []
    for role, change_type, old_rank, new_rank in changes:
        label = ROLE_LABEL[role]
        new_text = _fmt_rank(new_rank)
        if change_type == "placed":
            lines.append(f"🏅 **{display_name}** placed in **{label}**! {new_text}")
        elif change_type == "up":
            lines.append(
                f"🎉 **{display_name}** ranked up in **{label}**! "
                f"{_fmt_rank(old_rank)} → {new_text}"
            )
        else:
            lines.append(
                f"📉 **{display_name}** dropped in **{label}**. "
                f"{_fmt_rank(old_rank)} → {new_text}"
            )
    return discord.Embed(description="\n".join(lines), color=0xF99E1A)


class RankTracker(commands.Cog, name="Rank Tracker"):
    """Tracks known public profiles and pauses unavailable or departed accounts."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    def cog_load(self):
        self.session = aiohttp.ClientSession()
        self._rank_check.start()
        log.info("Cog Loaded.")

    async def cog_unload(self):
        self._rank_check.cancel()
        if self.session:
            await self.session.close()
        log.info("Cog Unloaded.")

    @tasks.loop(minutes=15)
    async def _rank_check(self):
        await self._run_check()

    @_rank_check.before_loop
    async def _before_rank_check(self):
        await self.bot.wait_until_ready()

    def _linked_users(self) -> list[tuple[int, str]]:
        linked = []
        for user_id, namespaces in self.bot.settings._user_cache.items():
            battletag = namespaces.get("ow", {}).get("battletag")
            if battletag:
                linked.append((user_id, battletag))
        return linked

    def _tracked_guilds(self) -> dict[int, int]:
        tracked = {}
        settings = self.bot.settings
        for guild_id, namespaces in settings._cache.items():
            if is_cog_disabled(settings, guild_id, "tasks.rank_tracker"):
                continue
            channel_id = namespaces.get("rank_tracker", {}).get("channel")
            if channel_id:
                tracked[guild_id] = channel_id
        return tracked

    def _shared_guild_ids(self, user_id: int, *, excluding: int | None = None) -> set[int]:
        shared = set()
        for guild in self.bot.guilds:
            if excluding is not None and guild.id == excluding:
                continue
            member = guild.get_member(user_id)
            if member is not None and not member.bot:
                shared.add(guild.id)
        return shared

    async def _reconcile_membership(self, user_id: int) -> set[int]:
        shared_guilds = self._shared_guild_ids(user_id)
        if shared_guilds:
            if await reactivate_account(self.bot.settings, user_id):
                log.info("Rank tracker: restored user %d after joining a shared guild", user_id)
        elif await archive_account(self.bot.settings, user_id):
            log.info("Rank tracker: archived user %d with no shared guilds", user_id)
        return shared_guilds

    async def _run_check(self):
        settings = self.bot.settings
        check_start = time.monotonic()
        now = time.time()
        linked_users = self._linked_users()
        if not linked_users:
            return

        tracked_guilds = self._tracked_guilds()
        candidates: list[tuple[int, str]] = []
        deferred = 0
        archived = 0

        # Reconcile every connection even when no guild has announcements enabled.
        for user_id, battletag in linked_users:
            shared_guilds = await self._reconcile_membership(user_id)
            state = get_tracker_state(settings, user_id)
            if not shared_guilds or state["status"] == STATUS_ARCHIVED:
                archived += 1
                continue

            # A shared server without a configured tracker keeps the connection
            # active but does not generate pointless API traffic.
            if not shared_guilds.intersection(tracked_guilds):
                continue

            if check_is_due(state, now=now):
                candidates.append((user_id, battletag))
            elif state["status"] == STATUS_UNAVAILABLE:
                deferred += 1

        if not candidates:
            log.debug(
                "Rank check: no accounts due (%d daily-deferred, %d archived)",
                deferred,
                archived,
            )
            return

        log.info(
            "Rank check started: %d due of %d linked (%d daily-deferred, %d archived)",
            len(candidates),
            len(linked_users),
            deferred,
            archived,
        )

        for index, (user_id, battletag) in enumerate(candidates):
            player_id = _battletag_to_player_id(battletag)
            player_start = time.monotonic()
            try:
                assert self.session is not None
                data = await fetch_player(self.session, player_id)
            except ValueError:
                await mark_unavailable(settings, user_id)
                log.info(
                    "  %-20s  private/not found; daily retry enabled  (%.2fs)",
                    battletag,
                    time.monotonic() - player_start,
                )
                if index < len(candidates) - 1:
                    await asyncio.sleep(FETCH_DELAY)
                continue
            except Exception as exc:
                await mark_attempt(settings, user_id)
                log.warning(
                    "  %-20s  fetch error: %s  (%.2fs)",
                    battletag,
                    exc,
                    time.monotonic() - player_start,
                )
                if index < len(candidates) - 1:
                    await asyncio.sleep(FETCH_DELAY)
                continue

            # A member can leave while their request is in flight. Never let a
            # late success silently reactivate an account that should be archived.
            if not self._shared_guild_ids(user_id):
                await archive_account(settings, user_id)
                if index < len(candidates) - 1:
                    await asyncio.sleep(FETCH_DELAY)
                continue

            await mark_success(settings, user_id)
            new_snapshot = _extract_snapshot(data)
            old_snapshot = settings.get_user(user_id, "ow", "rank_snapshot")

            if old_snapshot is None:
                log.info(
                    "  %-20s  qualified and baseline saved  (%.2fs)",
                    battletag,
                    time.monotonic() - player_start,
                )
                await settings.set_user(user_id, "ow", "rank_snapshot", new_snapshot)
                if index < len(candidates) - 1:
                    await asyncio.sleep(FETCH_DELAY)
                continue

            changes = _compare_snapshots(old_snapshot, new_snapshot)
            ranks_text = "  ".join(
                f"{role}={_plain_rank(new_snapshot.get(role))}"
                for role in ROLES
                if new_snapshot.get(role)
            ) or "unranked"

            if not changes:
                log.info(
                    "  %-20s  no change  [%s]  (%.2fs)",
                    battletag,
                    ranks_text,
                    time.monotonic() - player_start,
                )
                if index < len(candidates) - 1:
                    await asyncio.sleep(FETCH_DELAY)
                continue

            change_text = ", ".join(
                f"{role} {arrow}  {_plain_rank(old)} → {_plain_rank(new)}"
                for role, change_type, old, new in changes
                for arrow in (
                    "↑" if change_type == "up" else ("↓" if change_type == "down" else "★"),
                )
            )
            log.info(
                "  %-20s  CHANGED: %s  (%.2fs)",
                battletag,
                change_text,
                time.monotonic() - player_start,
            )

            # Persist before announcing so a restart cannot repeat the alert.
            await settings.set_user(user_id, "ow", "rank_snapshot", new_snapshot)

            for guild_id, channel_id in tracked_guilds.items():
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    continue
                member = guild.get_member(user_id)
                if member is None or member.bot:
                    continue
                channel = guild.get_channel(channel_id)
                if channel is None:
                    continue
                try:
                    await channel.send(embed=_build_embed(member.display_name, changes))
                except discord.Forbidden:
                    log.warning(
                        "Rank check: missing send permission in channel %d (guild %d)",
                        channel_id,
                        guild_id,
                    )
                except discord.HTTPException as exc:
                    log.error("Rank check: failed to send announcement: %s", exc)

            if index < len(candidates) - 1:
                await asyncio.sleep(FETCH_DELAY)

        log.info(
            "Rank check done: %d API call(s) in %.1fs",
            len(candidates),
            time.monotonic() - check_start,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Archive a linked account after its last shared guild is left."""

        if member.bot:
            return
        battletag = self.bot.settings.get_user(member.id, "ow", "battletag")
        if not battletag or self._shared_guild_ids(member.id, excluding=member.guild.id):
            return
        if await archive_account(self.bot.settings, member.id):
            log.info("Rank tracker: archived %s after leaving the last shared guild", battletag)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Restore a preserved linked account when its owner rejoins."""

        if member.bot:
            return
        battletag = self.bot.settings.get_user(member.id, "ow", "battletag")
        if battletag and await reactivate_account(self.bot.settings, member.id):
            log.info("Rank tracker: restored %s after a member join", battletag)

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        """Reconcile linked members when Pookie itself leaves a guild."""

        for member in guild.members:
            if member.bot:
                continue
            battletag = self.bot.settings.get_user(member.id, "ow", "battletag")
            if not battletag or self._shared_guild_ids(member.id, excluding=guild.id):
                continue
            await archive_account(self.bot.settings, member.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        """Restore archived connections already present in a newly shared guild."""

        for member in guild.members:
            if member.bot:
                continue
            battletag = self.bot.settings.get_user(member.id, "ow", "battletag")
            if battletag:
                await reactivate_account(self.bot.settings, member.id)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankTracker(bot))
