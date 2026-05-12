"""
quotes.py — Quote Board cog

Reply to any message with !quote to save it to the configured #quotebook channel.
Optionally include context messages after the anchor:

  !quote          → just the replied-to message
  !quote 4        → anchor + next 4 messages (offsets 0-4)
  !quote 1,4,5    → anchor + messages at offsets 1, 4, 5
  !quote 1-3,5    → anchor + messages at offsets 1,2,3,5

Guild settings namespace "quotes":
  "channel"  → int channel_id (set via !config quotebook #channel)
  "list"     → list[dict]  all saved quotes
  "next_id"  → int         auto-increment counter

Quote dict shape:
  id, messages (list), channel_id, channel_name,
  timestamp (anchor), saved_by_id, saved_by_name, anchor_message_id
"""

import asyncio
import logging
import random
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

log = logging.getLogger(__name__)

MAX_QUOTE_LEN = 4000
MAX_CONTEXT   = 20   # max offset allowed

_OFFSET_SPEC_RE = re.compile(r"^[\d,\-\s]+$")


def _parse_offsets(spec: str) -> set[int]:
    """Parse '4', '1,4,5', '1-3,5' into a set of integer offsets. Always includes 0."""
    offsets = {0}
    for part in spec.replace(" ", "").split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            offsets.update(range(int(a), int(b) + 1))
        else:
            offsets.add(int(part))
    return {o for o in offsets if 0 <= o <= MAX_CONTEXT}


def _build_embed(entry: dict, *, guild: discord.Guild = None) -> discord.Embed:
    msgs = entry["messages"]

    if len(msgs) == 1:
        # Single-message: clean author + big description format
        m          = msgs[0]
        text       = m["content"]
        if len(text) > MAX_QUOTE_LEN:
            text = text[:MAX_QUOTE_LEN - 3] + "..."

        embed = discord.Embed(description=f"\u201c{text}\u201d", color=0x5865F2)

        avatar_url = m.get("author_avatar")
        if guild:
            member = guild.get_member(m["author_id"])
            if member:
                avatar_url = member.display_avatar.url

        embed.set_author(name=m["author_name"], icon_url=avatar_url)
        embed.set_footer(
            text=f"Saved by {entry['saved_by_name']} | #{entry['channel_name']} | Quote #{entry['id']}"
        )
    else:
        # Multi-message: transcript format
        lines = []
        for m in msgs:
            name    = m["author_name"]
            content = m["content"] or ""
            lines.append(f"**{name}**: {content}")

        description = "\n".join(lines)
        if len(description) > MAX_QUOTE_LEN:
            description = description[:MAX_QUOTE_LEN - 3] + "..."

        embed = discord.Embed(description=description, color=0x5865F2)
        embed.set_footer(
            text=(
                f"Saved by {entry['saved_by_name']} | #{entry['channel_name']} "
                f"| Quote #{entry['id']} ({len(msgs)} messages)"
            )
        )

    try:
        embed.timestamp = datetime.fromisoformat(entry["timestamp"])
    except Exception:
        pass

    return embed


def _msg_to_dict(msg: discord.Message) -> dict:
    return {
        "content":      msg.content or "",
        "author_id":    msg.author.id,
        "author_name":  getattr(msg.author, "display_name", msg.author.name),
        "author_avatar": str(msg.author.display_avatar.url) if msg.author.display_avatar else None,
        "timestamp":    msg.created_at.astimezone(timezone.utc).isoformat(),
    }


class QuotesCog(commands.Cog, name="Quotes"):
    """Save and recall memorable server quotes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._lock = asyncio.Lock()

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Root command — !quote [context_spec]
    # ------------------------------------------------------------------ #

    @helped_hybrid_group("quote",
        name="quote",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def quote(self, ctx: commands.Context, *, args: str = None):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        # Validate args if provided
        offsets: set[int] | None = None
        if args is not None:
            if not _OFFSET_SPEC_RE.match(args):
                await ctx.send(
                    "❌ Invalid context spec.\n"
                    "Examples: `!quote 4`  |  `!quote 1,3,5`  |  `!quote 1-4,6`",
                    ephemeral=True,
                )
                return
            try:
                offsets = _parse_offsets(args)
            except ValueError:
                await ctx.send(
                    "❌ Invalid context spec.\n"
                    "Examples: `!quote 4`  |  `!quote 1,3,5`  |  `!quote 1-4,6`",
                    ephemeral=True,
                )
                return

        # Resolve anchor message from reply
        ref = getattr(ctx.message, "reference", None)
        if not ref or not ref.message_id:
            await ctx.send("❌ Reply to a message to quote it.", ephemeral=True)
            return

        try:
            anchor = ref.resolved or await ctx.channel.fetch_message(ref.message_id)
        except discord.NotFound:
            await ctx.send("❌ Couldn't find the message you replied to.", ephemeral=True)
            return

        if not anchor.content and not offsets:
            await ctx.send("❌ That message has no text content to quote.", ephemeral=True)
            return

        # Check quotebook channel
        channel_id = self.bot.settings.get(ctx.guild.id, "quotes", "channel")
        if not channel_id:
            await ctx.send(
                "❌ No quotebook channel set. Ask an admin to run `!config quotebook #channel`.",
                ephemeral=True,
            )
            return

        qb_channel = ctx.guild.get_channel(channel_id)
        if qb_channel is None:
            await ctx.send(
                "❌ The configured quotebook channel no longer exists. Ask an admin to reconfigure it.",
                ephemeral=True,
            )
            return

        # Build messages list
        if offsets is None:
            # Plain !quote — just the anchor
            message_dicts = [_msg_to_dict(anchor)]
        else:
            max_offset = max(offsets)
            # Fetch up to max_offset messages after the anchor, oldest first
            after_msgs: list[discord.Message] = []
            async for m in ctx.channel.history(
                limit=max_offset, after=anchor, oldest_first=True
            ):
                after_msgs.append(m)

            # Build ordered list: index 0 = anchor, index N = Nth message after
            pool = [anchor] + after_msgs
            message_dicts = [_msg_to_dict(pool[i]) for i in sorted(offsets) if i < len(pool)]

            if not message_dicts:
                await ctx.send("❌ No messages found at those offsets.", ephemeral=True)
                return

        async with self._lock:
            quotes  = list(self.bot.settings.get(ctx.guild.id, "quotes", "list") or [])
            next_id = self.bot.settings.get(ctx.guild.id, "quotes", "next_id") or 1

            # Deduplication on anchor message
            existing = next((q for q in quotes if q.get("anchor_message_id") == anchor.id), None)
            if existing:
                await ctx.send(
                    f"⚠️ That message is already in the quotebook (Quote #{existing['id']}).",
                    ephemeral=True,
                )
                return

            entry = {
                "id":               next_id,
                "messages":         message_dicts,
                "channel_id":       anchor.channel.id,
                "channel_name":     anchor.channel.name,
                "timestamp":        anchor.created_at.astimezone(timezone.utc).isoformat(),
                "saved_by_id":      ctx.author.id,
                "saved_by_name":    ctx.author.display_name,
                "anchor_message_id": anchor.id,
            }

            quotes.append(entry)
            await self.bot.settings.set(ctx.guild.id, "quotes", "list", quotes)
            await self.bot.settings.set(ctx.guild.id, "quotes", "next_id", next_id + 1)

        embed = _build_embed(entry, guild=ctx.guild)
        try:
            await qb_channel.send(embed=embed)
        except discord.Forbidden:
            await ctx.send(
                f"❌ I don't have permission to send messages in {qb_channel.mention}.",
                ephemeral=True,
            )
            return

        await ctx.send(f"✅ Quote #{next_id} saved to {qb_channel.mention}!", ephemeral=True)
        log.info("Quote #%d saved in guild %d by %s (%d message(s))", next_id, ctx.guild.id, ctx.author, len(message_dicts))

    # ------------------------------------------------------------------ #
    #  !quote random
    # ------------------------------------------------------------------ #

    @helped_command(quote, "quote random",
        name="random",
    )
    async def quote_random(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        quotes = self.bot.settings.get(ctx.guild.id, "quotes", "list") or []
        if not quotes:
            await ctx.send("❌ No quotes saved yet. Reply to a message with `!quote` to add one.")
            return

        entry = random.choice(quotes)
        embed = _build_embed(entry, guild=ctx.guild)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(QuotesCog(bot))
