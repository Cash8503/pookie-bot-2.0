import logging
from urllib.parse import urlparse, parse_qs

import discord
from discord import app_commands
from discord.ext import commands

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

    @commands.hybrid_group(
        name="config",
        invoke_without_command=True,
        case_insensitive=True,
        brief="View and manage server configuration",
        help=(
            "View and manage this server's bot configuration.\n\n"
            "Subcommands:\n"
            "  ranktracker <#channel|off>  — Set/disable rank tracker channel\n"
            "  quotebook <#channel|off>    — Set/disable the quotebook channel\n"
            "  translate mode <live|individual> — Set auto-translate mode\n"
            "  linkclean toggle            — Toggle link cleaner on/off\n"
            "  linkclean ignore            — Mute/unmute link cleaner in current channel\n"
            "  linkclean status            — Show link cleaner state\n"
            "  linkclean test <url>        — Preview URL cleaning\n\n"
            "All subcommands require the Manage Server permission."
        ),
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

    @config.command(
        name="ranktracker",
        brief="Set or disable the rank tracker announcement channel",
        help=(
            "Set which channel rank change announcements post in.\n\n"
            "Examples:\n"
            "  !config ranktracker #rank-updates\n"
            "  !config ranktracker off\n\n"
            "Passing 'off' disables announcements without affecting linked battletags."
        ),
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

    @config.command(
        name="quotebook",
        brief="Set or disable the quotebook channel",
        help=(
            "Set which channel saved quotes are posted in.\n\n"
            "Examples:\n"
            "  !config quotebook #quote-book\n"
            "  !config quotebook off\n\n"
            "Passing 'off' disables quote posting without deleting saved quotes."
        ),
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

    @config.group(
        name="translate",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Manage auto-translate settings",
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

    @config_translate.command(
        name="mode",
        brief="Set auto-translate mode to live or individual",
        help=(
            "Set the server's auto-translate behavior.\n\n"
            "Use `live` for one shared updating embed per channel, or `individual` to reply to each foreign message."
        ),
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

    @config.group(
        name="linkclean",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Manage the link cleaner",
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean(self, ctx: commands.Context):
        await ctx.invoke(self.config_linkclean_status)

    @config_linkclean.command(
        name="toggle",
        brief="Enable or disable the link cleaner",
        help=(
            "Toggles the link cleaner on or off for the entire server.\n\n"
            "When disabled, the bot will not respond to any links in any channel. "
            "Use `!config linkclean ignore` to mute individual channels instead."
        ),
    )
    @commands.has_permissions(manage_guild=True)
    async def config_linkclean_toggle(self, ctx: commands.Context):
        current = self.bot.settings.get(ctx.guild.id, "link_cleaner", "enabled", True)
        new_val = not current
        await self.bot.settings.set(ctx.guild.id, "link_cleaner", "enabled", new_val)
        state = "**enabled** ✅" if new_val else "**disabled** ❌"
        await ctx.send(f"Link cleaner is now {state}.", ephemeral=True)

    @config_linkclean.command(
        name="ignore",
        brief="Mute/unmute the link cleaner in this channel",
        help=(
            "Toggles whether the link cleaner fires in the current channel.\n\n"
            "Useful for deal or promo channels where affiliate links are intentional. "
            "Run again in the same channel to re-enable it."
        ),
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

    @config_linkclean.command(
        name="status",
        brief="Show current link cleaner settings",
        help="Displays the current link cleaner state and ignored channels for this server.",
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

    @config_linkclean.command(
        name="test",
        brief="Preview what a URL looks like after cleaning",
        help=(
            "Pass any URL to see exactly what the cleaner would do — which params get "
            "stripped and which are kept — without posting it publicly.\n\n"
            "Example:\n"
            "  !config linkclean test https://amazon.com/dp/B09XYZ?tag=affiliate-20&ref=sr_1_1"
        ),
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
