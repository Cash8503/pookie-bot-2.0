# cogs/general.py
import time
from discord import app_commands
from typing import Optional
import discord
from discord.ext import commands
from utils.be_smart import smart_send, smart_defer

class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(name="ping", description="Check bot latency.")
    @app_commands.describe(ephemeral="If true, slash reply is ephemeral")
    async def ping(self, ctx: commands.Context, ephemeral: bool = False):
        """Works as both /ping and !ping."""
        latency_ms = round(self.bot.latency * 1000)
        await smart_send(ctx, f"Pong! `{latency_ms} ms`", ephemeral=ephemeral)
        

    @commands.hybrid_command(name="purge", description="Delete recent messages in this channel.")
    @app_commands.describe(
        limit="How many recent messages to scan/delete (1-100).",
        user="Only delete messages from this user.",
        contains="Only delete messages containing this text (case-insensitive).",
        ephemeral="If true, slash reply is ephemeral",
    )
    @commands.has_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(
        self,
        ctx: commands.Context,
        limit: int,
        user: Optional[discord.Member] = None,
        contains: Optional[str] = None,
        ephemeral: bool = False,
    ):
        await smart_defer(ctx, ephemeral=True)
        """
        Basic purge:
          - Deletes up to `limit` messages that match optional `user` and/or `contains`.
          - Skips messages older than 14 days (Discord API limit).
        """
        if not ctx.guild or not isinstance(ctx.channel, (discord.TextChannel, discord.Thread)):
            return await smart_send(ctx, "This command can only be used in a server text channel.", ephemeral=True)

        # Clamp the scan size
        limit = max(1, min(100, limit))
        needle = (contains or "").lower()

        def check(m: discord.Message) -> bool:
            if user and m.author.id != user.id:
                return False
            if needle and needle not in (m.content or "").lower():
                return False
            return True

        # If invoked via prefix, try to remove the invocation message itself first so it won't be counted.
        if not getattr(ctx, "interaction", None):
            try:
                await ctx.message.delete()
            except Exception:
                pass

        try:
            deleted = await ctx.channel.purge(
                limit=limit,
                check=check,
                reason=f"Purge by {ctx.author} ({ctx.author.id})",
                bulk=True,
            )
        except discord.Forbidden:
            return await smart_send(ctx, "I’m missing permissions: **Manage Messages** (and possibly **Read Message History**).", ephemeral=True)
        except discord.HTTPException as e:
            return await smart_send(ctx, f"Purge failed: `{e}`", ephemeral=True)

        # Acknowledge
        msg = f"Deleted `{len(deleted)}` message(s)."
        # For prefix use, you might want the confirmation to vanish after a few seconds.
        kwargs = {"ephemeral": ephemeral}
        if not ephemeral and getattr(ctx, "interaction", None) is None:
            kwargs["delete_after"] = 5
        await smart_send(ctx, msg, **kwargs)


async def setup(bot: commands.Bot):
    print("Loading General")
    await bot.add_cog(General(bot))
