"""
rank_tracker.py — Background rank change detection and announcements.

Polls all linked players every 15 minutes and posts an embed to the
configured channel whenever a rank changes in any role.

Configure the announcement channel with:
  !config ranktracker #channel
"""

import asyncio
import logging
import time

import aiohttp
import discord
from discord.ext import commands, tasks

from cogs.ow_picker import fetch_player, _battletag_to_player_id, _fmt_rank

log = logging.getLogger(__name__)

ROLES = ("tank", "damage", "support", "open")

ROLE_LABEL = {
    "tank":    "Tank",
    "damage":  "Damage",
    "support": "Support",
    "open":    "Open Queue",
}

RANK_ORDER = {
    "bronze": 0, "silver": 1, "gold": 2, "platinum": 3,
    "diamond": 4, "master": 5, "grandmaster": 6, "champion": 7,
}

# Seconds to wait between player fetches — keeps OverFast API happy
FETCH_DELAY = 2.0


def _plain_rank(rank: dict | None) -> str:
    """Plain-text rank string for logging (no Discord emoji)."""
    if not rank:
        return "Unranked"
    div  = rank.get("division", "?").capitalize()
    tier = rank.get("tier")
    if tier and rank.get("division", "").lower() not in ("grandmaster", "champion"):
        return f"{div} {tier}"
    return div


def _rank_score(rank: dict | None) -> int | None:
    """Numeric rank score for comparison. Higher = better. None = unranked."""
    if rank is None:
        return None
    base = RANK_ORDER.get(rank.get("division", "").lower(), -1) * 10
    tier = rank.get("tier") or 0
    return base + (3 - tier)   # tier 1 → +2, tier 2 → +1, tier 3 → 0


def _extract_snapshot(data: dict) -> dict:
    """Pull current ranks from fetch_player() response into snapshot dict."""
    comp_pc = (
        data.get("summary", {})
            .get("competitive", {})
            .get("pc", {})
    ) or {}
    result = {}
    for role in ROLES:
        rank = comp_pc.get(role)
        if rank:
            result[role] = {"division": rank.get("division"), "tier": rank.get("tier")}
        else:
            result[role] = None
    return result


def _compare_snapshots(
    old: dict | None,
    new: dict,
) -> list[tuple[str, str, dict | None, dict | None]]:
    """
    Returns (role, change_type, old_rank, new_rank) for each changed role.
    change_type is "up", "down", or "placed" (was unranked, now ranked).
    Becoming unranked is silently ignored (season resets / privacy flaps).
    """
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
        new_str = _fmt_rank(new_rank)
        if change_type == "placed":
            lines.append(f"🏅 **{display_name}** placed in **{label}**! {new_str}")
        elif change_type == "up":
            lines.append(f"🎉 **{display_name}** ranked up in **{label}**! {_fmt_rank(old_rank)} → {new_str}")
        else:
            lines.append(f"📉 **{display_name}** dropped in **{label}**. {_fmt_rank(old_rank)} → {new_str}")
    return discord.Embed(description="\n".join(lines), color=0xF99E1A)


class RankTracker(commands.Cog, name="Rank Tracker"):
    """Background rank change detection and announcements."""

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

    async def _run_check(self):
        s = self.bot.settings
        check_start = time.monotonic()

        # Find guilds that have a rank tracker channel set
        tracked_guilds: dict[int, int] = {}
        for guild_id, namespaces in s._cache.items():
            channel_id = namespaces.get("rank_tracker", {}).get("channel")
            if channel_id:
                tracked_guilds[guild_id] = channel_id

        if not tracked_guilds:
            return

        # Find all users with a linked battletag
        linked_users: list[tuple[int, str]] = []
        for user_id, namespaces in s._user_cache.items():
            battletag = namespaces.get("ow", {}).get("battletag")
            if battletag:
                linked_users.append((user_id, battletag))

        if not linked_users:
            return

        log.info("Rank check started: %d user(s)", len(linked_users))

        for user_id, battletag in linked_users:
            player_id = _battletag_to_player_id(battletag)
            player_start = time.monotonic()
            try:
                data = await fetch_player(self.session, player_id)
            except ValueError:
                log.info("  %-20s  private/not found  (%.2fs)", battletag, time.monotonic() - player_start)
                await asyncio.sleep(FETCH_DELAY)
                continue
            except Exception as e:
                log.warning("  %-20s  fetch error: %s  (%.2fs)", battletag, e, time.monotonic() - player_start)
                await asyncio.sleep(FETCH_DELAY)
                continue

            new_snapshot = _extract_snapshot(data)
            old_snapshot = s.get_user(user_id, "ow", "rank_snapshot")

            # First run — save baseline silently, no announcement
            if old_snapshot is None:
                log.info("  %-20s  baseline saved  (%.2fs)", battletag, time.monotonic() - player_start)
                await s.set_user(user_id, "ow", "rank_snapshot", new_snapshot)
                await asyncio.sleep(FETCH_DELAY)
                continue

            changes = _compare_snapshots(old_snapshot, new_snapshot)

            ranks_str = "  ".join(
                f"{r}={_plain_rank(new_snapshot.get(r))}" for r in ROLES if new_snapshot.get(r)
            ) or "unranked"

            if not changes:
                log.info("  %-20s  no change  [%s]  (%.2fs)", battletag, ranks_str, time.monotonic() - player_start)
                await asyncio.sleep(FETCH_DELAY)
                continue

            change_str = ", ".join(
                f"{r} {arrow}  {_plain_rank(old)} → {_plain_rank(new)}"
                for r, ct, old, new in changes
                for arrow in (("↑" if ct == "up" else ("↓" if ct == "down" else "★")),)
            )
            log.info("  %-20s  CHANGED: %s  (%.2fs)", battletag, change_str, time.monotonic() - player_start)

            # Save before posting so a crash mid-announce doesn't re-fire
            await s.set_user(user_id, "ow", "rank_snapshot", new_snapshot)

            # Announce in every tracked guild the user is a member of
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
                    log.warning("Rank check: missing send permission in channel %d (guild %d)", channel_id, guild_id)
                except discord.HTTPException as e:
                    log.error("Rank check: failed to send announcement: %s", e)

            await asyncio.sleep(FETCH_DELAY)

        log.info("Rank check done: %d user(s) in %.1fs", len(linked_users), time.monotonic() - check_start)


async def setup(bot: commands.Bot):
    await bot.add_cog(RankTracker(bot))
