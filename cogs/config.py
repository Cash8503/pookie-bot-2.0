import logging
from urllib.parse import urlparse, parse_qs

import discord
from discord.ext import commands

from cogs._help import documented_command, documented_group, documented_hybrid_command, documented_hybrid_group

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

    @documented_hybrid_group(
        name="config",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config(self, ctx: commands.Context):
        """View and manage server configuration

        Manage channels and feature settings for this server. Requires Manage Server.

        Usage:
            {prefix}config"""
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

    @documented_command(config,
        name="ranktracker",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_ranktracker(self, ctx: commands.Context, *, channel_or_off: str):
        """Set rank tracker channel

        Sets or disables the rank tracker announcement channel.

        Usage:
            {prefix}config ranktracker <channel|off>

        Arguments:
            channel_or_off: A #channel mention or ID, or 'off' to disable

        Examples:
            {prefix}config ranktracker #rank-updates
            {prefix}config ranktracker off"""
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

    @documented_command(config,
        name="quotebook",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_quotebook(self, ctx: commands.Context, *, channel_or_off: str):
        """Set quotebook channel

        Sets or disables the channel where saved quotes are posted.

        Usage:
            {prefix}config quotebook <channel|off>

        Arguments:
            channel_or_off: A #channel mention or ID, or 'off' to disable

        Examples:
            {prefix}config quotebook #quotebook
            {prefix}config quotebook off"""
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

    @documented_group(config,
        name="translate",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config_translate(self, ctx: commands.Context):
        """Manage auto-translate config

        Show or change server-level auto-translate settings.

        Usage:
            {prefix}config translate"""
        mode = self.bot.settings.get(ctx.guild.id, "auto_translate", "mode", "live")
        await ctx.send(
            f"🌐 Auto-translate mode: **{mode}**\n"
            "`!config translate mode live` — one shared embed that updates in place\n"
            "`!config translate mode individual` — reply to each foreign message separately",
            ephemeral=True,
        )

    @documented_command(config_translate,
        name="mode",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_translate_mode(self, ctx: commands.Context, mode: str):
        """Set auto-translate mode

        Choose live shared embeds or individual replies for translations.

        Usage:
            {prefix}config translate mode <live|individual>

        Arguments:
            mode: live or individual

        Examples:
            {prefix}config translate mode live
            {prefix}config translate mode individual"""
        mode = mode.lower().strip()
        if mode not in ("live", "individual"):
            await ctx.send("❌ Valid modes: `live`, `individual`", ephemeral=True)
            return
        await self.bot.settings.set(ctx.guild.id, "auto_translate", "mode", mode)
        await ctx.send(f"✅ Auto-translate mode set to **{mode}**.", ephemeral=True)

    # ------------------------------------------------------------------ #
    #  Link cleaner sub-group
    # ------------------------------------------------------------------ #

    @documented_group(config,
        name="linkclean",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean(self, ctx: commands.Context):
        """Manage link cleaner config

        Enable, disable, ignore channels, and test URL cleanup.

        Usage:
            {prefix}config linkclean"""
        await ctx.invoke(self.config_linkclean_status)

    @documented_command(config_linkclean,
        name="toggle",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_toggle(self, ctx: commands.Context):
        """Toggle link cleaner

        Turns the link cleaner on or off for this server.

        Usage:
            {prefix}config linkclean toggle"""
        current = self.bot.settings.get(ctx.guild.id, "link_cleaner", "enabled", True)
        new_val = not current
        await self.bot.settings.set(ctx.guild.id, "link_cleaner", "enabled", new_val)
        state = "**enabled** ✅" if new_val else "**disabled** ❌"
        await ctx.send(f"Link cleaner is now {state}.", ephemeral=True)

    @documented_command(config_linkclean,
        name="ignore",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_ignore(self, ctx: commands.Context):
        """Ignore or unignore this channel

        Toggles whether link cleaning runs in the current channel.

        Usage:
            {prefix}config linkclean ignore"""
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

    @documented_command(config_linkclean,
        name="status",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_status(self, ctx: commands.Context):
        """Show link cleaner status

        Shows whether link cleaning is enabled and which channels are ignored.

        Usage:
            {prefix}config linkclean status"""
        s = self.bot.settings
        state = "Enabled ✅" if s.get(ctx.guild.id, "link_cleaner", "enabled", True) else "Disabled ❌"
        ignored = ", ".join(
            f"<#{c}>" for c in s.get(ctx.guild.id, "link_cleaner", "ignored_channels", [])
        ) or "None"
        embed = discord.Embed(title="🧹 Link Cleaner Status", color=0x5865F2)
        embed.add_field(name="Status", value=state, inline=True)
        embed.add_field(name="Ignored Channels", value=ignored, inline=False)
        await ctx.send(embed=embed, ephemeral=True)

    @documented_command(config_linkclean,
        name="test",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_test(self, ctx: commands.Context, *, url: str):
        """Preview URL cleanup

        Shows which tracking parameters would be stripped from a URL.

        Usage:
            {prefix}config linkclean test <url>

        Arguments:
            url: The URL to preview

        Examples:
            {prefix}config linkclean test https://example.com/?utm_source=x"""
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
