import logging
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

from cogs.link_cleaner import clean_url

log = logging.getLogger(__name__)


class ConfigCog(commands.Cog, name="Config"):
    """Server configuration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Root — show full server config
    # ------------------------------------------------------------------ #

    @helped_hybrid_group("config",
        name="config",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config(self, ctx: commands.Context):
        s = self.bot.settings
        channel_id = s.get(ctx.guild.id, "rank_tracker", "channel")
        channel_str = f"<#{channel_id}>" if channel_id else "Not set"
        qb_id = s.get(ctx.guild.id, "quotes", "channel")
        qb_str = f"<#{qb_id}>" if qb_id else "Not set"
        lc_enabled = s.get(ctx.guild.id, "link_cleaner", "enabled", True)
        lc_state = "Enabled ✅" if lc_enabled else "Disabled ❌"
        ignored = s.get(ctx.guild.id, "link_cleaner", "ignored_channels", [])
        ignored_str = ", ".join(f"<#{c}>" for c in ignored) or "None"

        embed = discord.Embed(title="⚙️ Server Configuration", color=0x5865F2)
        embed.add_field(name="Rank Tracker Channel", value=channel_str, inline=False)
        embed.add_field(name="Quotebook Channel", value=qb_str, inline=False)
        embed.add_field(name="Link Cleaner", value=lc_state, inline=True)
        embed.add_field(name="Ignored Channels", value=ignored_str, inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ #
    #  Rank tracker channel
    # ------------------------------------------------------------------ #

    @helped_command(config, "config ranktracker",
        name="ranktracker",
    )
    @app_commands.describe(channel_or_off="A #channel mention or ID, or 'off' to disable")
    @commands.has_permissions(manage_guild=True)
    async def config_ranktracker(self, ctx: commands.Context, *, channel_or_off: str):
        if channel_or_off.strip().lower() == "off":
            await self.bot.settings.delete(ctx.guild.id, "rank_tracker", "channel")
            await ctx.send("📴 Rank tracker disabled.", ephemeral=True)
            return
        try:
            channel = await commands.TextChannelConverter().convert(ctx, channel_or_off.strip())
        except commands.BadArgument:
            await ctx.send(
                "❌ Couldn't find that channel. Use a #mention, channel ID, or `off`.",
                ephemeral=True,
            )
            return
        await self.bot.settings.set(ctx.guild.id, "rank_tracker", "channel", channel.id)
        await ctx.send(
            f"✅ Rank tracker announcements will post in {channel.mention}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    #  Quotebook channel
    # ------------------------------------------------------------------ #

    @helped_command(config, "config quotebook",
        name="quotebook",
    )
    @app_commands.describe(channel_or_off="A #channel mention or ID, or 'off' to disable")
    @commands.has_permissions(manage_guild=True)
    async def config_quotebook(self, ctx: commands.Context, *, channel_or_off: str):
        if channel_or_off.strip().lower() == "off":
            await self.bot.settings.delete(ctx.guild.id, "quotes", "channel")
            await ctx.send("📴 Quotebook channel disabled.", ephemeral=True)
            return
        try:
            channel = await commands.TextChannelConverter().convert(ctx, channel_or_off.strip())
        except commands.BadArgument:
            await ctx.send(
                "❌ Couldn't find that channel. Use a #mention, channel ID, or `off`.",
                ephemeral=True,
            )
            return
        await self.bot.settings.set(ctx.guild.id, "quotes", "channel", channel.id)
        await ctx.send(
            f"✅ Quotes will be posted in {channel.mention}.",
            ephemeral=True,
        )

    # ------------------------------------------------------------------ #
    #  Auto-translate sub-group
    # ------------------------------------------------------------------ #

    @helped_group(config, "config translate",
        name="translate",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config_translate(self, ctx: commands.Context):
        mode = self.bot.settings.get(ctx.guild.id, "auto_translate", "mode", "live")
        await ctx.send(
            f"🌐 Auto-translate mode: **{mode}**\n"
            "`!config translate mode live` — one shared embed that updates in place\n"
            "`!config translate mode individual` — reply to each foreign message separately",
            ephemeral=True,
        )

    @helped_command(config_translate, "config translate mode",
        name="mode",
    )
    @commands.has_permissions(manage_guild=True)
    @app_commands.describe(mode="live or individual")
    async def config_translate_mode(self, ctx: commands.Context, mode: str):
        mode = mode.lower().strip()
        if mode not in ("live", "individual"):
            await ctx.send("❌ Valid modes: `live`, `individual`", ephemeral=True)
            return
        await self.bot.settings.set(ctx.guild.id, "auto_translate", "mode", mode)
        await ctx.send(f"✅ Auto-translate mode set to **{mode}**.", ephemeral=True)

    # ------------------------------------------------------------------ #
    #  Link cleaner sub-group
    # ------------------------------------------------------------------ #

    @helped_group(config, "config linkclean",
        name="linkclean",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean(self, ctx: commands.Context):
        await ctx.invoke(self.config_linkclean_status)

    @helped_command(config_linkclean, "config linkclean toggle",
        name="toggle",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_toggle(self, ctx: commands.Context):
        current = self.bot.settings.get(ctx.guild.id, "link_cleaner", "enabled", True)
        new_val = not current
        await self.bot.settings.set(ctx.guild.id, "link_cleaner", "enabled", new_val)
        state = "**enabled** ✅" if new_val else "**disabled** ❌"
        await ctx.send(f"Link cleaner is now {state}.", ephemeral=True)

    @helped_command(config_linkclean, "config linkclean ignore",
        name="ignore",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_ignore(self, ctx: commands.Context):
        cid = ctx.channel.id
        ignored = list(self.bot.settings.get(ctx.guild.id, "link_cleaner", "ignored_channels", []))
        if cid in ignored:
            ignored.remove(cid)
            await self.bot.settings.set(ctx.guild.id, "link_cleaner", "ignored_channels", ignored)
            await ctx.send(f"{ctx.channel.mention} is no longer ignored. ✅", ephemeral=True)
        else:
            ignored.append(cid)
            await self.bot.settings.set(ctx.guild.id, "link_cleaner", "ignored_channels", ignored)
            await ctx.send(f"{ctx.channel.mention} is now ignored. ❌", ephemeral=True)

    @helped_command(config_linkclean, "config linkclean status",
        name="status",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_status(self, ctx: commands.Context):
        s = self.bot.settings
        state = "Enabled ✅" if s.get(ctx.guild.id, "link_cleaner", "enabled", True) else "Disabled ❌"
        ignored = ", ".join(
            f"<#{c}>" for c in s.get(ctx.guild.id, "link_cleaner", "ignored_channels", [])
        ) or "None"
        embed = discord.Embed(title="🧹 Link Cleaner Status", color=0x5865F2)
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(name="Ignored Channels", value=ignored, inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @helped_command(config_linkclean, "config linkclean test",
        name="test",
    )
    @app_commands.describe(url="The URL to preview")
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_test(self, ctx: commands.Context, *, url: str):
        cleaned = clean_url(url)
        if cleaned == url:
            await ctx.send("✅ That URL is already clean — no tracking params found.", ephemeral=True)
            return
        original_params = set(parse_qs(urlparse(url).query).keys())
        clean_params    = set(parse_qs(urlparse(cleaned).query).keys())
        stripped = original_params - clean_params
        kept     = original_params - stripped
        embed = discord.Embed(title="🔍 URL Test Result", color=0x57F287)
        embed.add_field(name="Original", value=f"`{url}`", inline=False)
        embed.add_field(name="Cleaned",  value=f"`{cleaned}`", inline=False)
        embed.add_field(
            name="🗑️ Stripped",
            value=", ".join(f"`{p}`" for p in sorted(stripped)) or "None",
            inline=True,
        )
        embed.add_field(
            name="✅ Kept",
            value=", ".join(f"`{p}`" for p in sorted(kept)) or "None",
            inline=True,
        )
        await ctx.send(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ConfigCog(bot))
