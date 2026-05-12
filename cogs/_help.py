from __future__ import annotations

import logging
from dataclasses import dataclass, field

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandHelp:
    brief: str
    description: str
    usage: str | None = None
    examples: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    subcommands: tuple[str, ...] = ()


HELP_CONTENT: dict[str, CommandHelp] = {
    "help": CommandHelp(
        brief="Show bot or command help",
        description="Shows the command overview, or detailed help for one command.",
        usage="{prefix}help [command]",
        examples=("{prefix}help", "{prefix}help ow whois", "{prefix}help typotax repay"),
    ),

    "db": CommandHelp(
        brief="Inspect and edit settings rows",
        description="Owner-only database admin commands for guild and user settings.",
        usage="{prefix}db",
        subcommands=(
            "{prefix}db tables",
            "{prefix}db get <guild|user> <id> [namespace] [key]",
            "{prefix}db find <id>",
            "{prefix}db set <guild|user> <id> <namespace> <key> <value>",
            "{prefix}db del <guild|user> <id> <namespace> <key>",
            "{prefix}db clear <guild|user> <id> [namespace]",
        ),
    ),
    "db tables": CommandHelp(
        brief="Show settings row counts",
        description="Shows row counts for the guild and user settings tables.",
        usage="{prefix}db tables",
    ),
    "db get": CommandHelp(
        brief="Read settings rows",
        description="Reads settings rows for a guild or user, optionally filtered by namespace and key.",
        usage="{prefix}db get <guild|user> <id> [namespace] [key]",
        examples=("{prefix}db get user 123456789", "{prefix}db get guild 987654321 link_cleaner"),
    ),
    "db find": CommandHelp(
        brief="Search settings for an ID",
        description="Searches both guild and user settings tables for rows matching an ID.",
        usage="{prefix}db find <id>",
        examples=("{prefix}db find @someone",),
    ),
    "db set": CommandHelp(
        brief="Set a settings value",
        description="Writes a JSON-parsed or plain-text value into guild or user settings.",
        usage="{prefix}db set <guild|user> <id> <namespace> <key> <value>",
        examples=("{prefix}db set guild 987654321 link_cleaner enabled true",),
    ),
    "db del": CommandHelp(
        brief="Delete one settings row",
        description="Deletes one settings row identified by ID, namespace, and key.",
        usage="{prefix}db del <guild|user> <id> <namespace> <key>",
        examples=("{prefix}db del user 123456789 ow battletag",),
    ),
    "db clear": CommandHelp(
        brief="Clear settings rows",
        description="Deletes all settings for a guild or user, optionally scoped to one namespace.",
        usage="{prefix}db clear <guild|user> <id> [namespace]",
        examples=("{prefix}db clear user 123456789", "{prefix}db clear guild 987654321 link_cleaner"),
    ),

    "template": CommandHelp(
        brief="Template command group",
        description="Example command group for new cog scaffolding.",
        usage="{prefix}template",
        subcommands=("{prefix}template example",),
    ),
    "template example": CommandHelp(
        brief="Template example command",
        description="Example subcommand for new cog scaffolding.",
        usage="{prefix}template example",
    ),

    "activity": CommandHelp(
        brief="Server activity stats and leaderboards",
        description="Track messages, voice time, and emoji usage for this server.",
        usage="{prefix}activity",
        subcommands=(
            "{prefix}activity summary [all]",
            "{prefix}activity leaderboard [all]",
            "{prefix}activity stats [member]",
            "{prefix}activity emojis [all]",
            "{prefix}activity emoji <emoji> [all]",
            "{prefix}activity backfill confirm",
        ),
    ),
    "activity leaderboard": CommandHelp(
        brief="Show the most active members",
        description="Ranks members by activity score for this month or all time.",
        usage="{prefix}activity leaderboard [all]",
        examples=("{prefix}activity leaderboard", "{prefix}activity leaderboard all"),
    ),
    "activity stats": CommandHelp(
        brief="Show activity stats for one member",
        description="Shows message, voice, emoji, and score totals for a member.",
        usage="{prefix}activity stats [member]",
        examples=("{prefix}activity stats", "{prefix}activity stats @Cash"),
    ),
    "activity emojis": CommandHelp(
        brief="Show the most used emojis",
        description="Lists the most used custom and Unicode emojis this month or all time.",
        usage="{prefix}activity emojis [all]",
        examples=("{prefix}activity emojis", "{prefix}activity emojis all"),
    ),
    "activity emoji": CommandHelp(
        brief="Show who uses an emoji most",
        description="Ranks members by usage count for one emoji.",
        usage="{prefix}activity emoji <emoji> [all]",
        examples=("{prefix}activity emoji :pookie:", "{prefix}activity emoji :pookie: all"),
    ),
    "activity summary": CommandHelp(
        brief="Show activity category leaders",
        description="Shows top members for messages, voice time, emoji usage, and most-used emojis.",
        usage="{prefix}activity summary [all]",
        examples=("{prefix}activity summary", "{prefix}activity summary all"),
    ),
    "activity backfill": CommandHelp(
        brief="Rebuild activity stats from history",
        description="Scans channel history and replaces stored activity data. Requires Manage Server.",
        usage="{prefix}activity backfill confirm",
        examples=("{prefix}activity backfill", "{prefix}activity backfill confirm"),
        notes=("This can take a long time on active servers.",),
    ),

    "admin": CommandHelp(
        brief="Bot owner runtime admin commands",
        description="Manage per-server cog availability, runtime loading, reloads, deletion, and restarts.",
        usage="{prefix}admin",
        subcommands=(
            "{prefix}admin enable <cog>",
            "{prefix}admin disable <cog>",
            "{prefix}admin start <cog>",
            "{prefix}admin stop <cog>",
            "{prefix}admin reload <cog>",
            "{prefix}admin reloadall",
            "{prefix}admin globalenable <cog>",
            "{prefix}admin globaldisable <cog>",
            "{prefix}admin list",
            "{prefix}admin nuke <count>",
            "{prefix}admin restart",
        ),
    ),
    "admin enable": CommandHelp(
        brief="Enable a cog in this server",
        description="Removes a cog from this server's disabled list. The extension remains loaded globally.",
        usage="{prefix}admin enable <cog>",
        examples=("{prefix}admin enable ow_picker",),
    ),
    "admin disable": CommandHelp(
        brief="Disable a cog in this server",
        description="Adds a cog to this server's disabled list without affecting other servers.",
        usage="{prefix}admin disable <cog>",
        examples=("{prefix}admin disable ow_picker",),
    ),
    "admin globalenable": CommandHelp(
        brief="Enable a cog on startup globally",
        description="Renames an underscored cog file so it loads on the next bot startup for every server.",
        usage="{prefix}admin globalenable <cog>",
        examples=("{prefix}admin globalenable ow_picker",),
    ),
    "admin globaldisable": CommandHelp(
        brief="Disable a cog on startup globally",
        description="Adds a leading underscore to a cog file so it is skipped on startup for every server.",
        usage="{prefix}admin globaldisable <cog>",
        examples=("{prefix}admin globaldisable ow_picker",),
    ),
    "admin start": CommandHelp(
        brief="Load a cog now",
        description="Loads a cog into the running bot without changing startup behavior.",
        usage="{prefix}admin start <cog>",
        examples=("{prefix}admin start ow_picker",),
    ),
    "admin stop": CommandHelp(
        brief="Unload a cog now",
        description="Unloads a running cog without changing startup behavior.",
        usage="{prefix}admin stop <cog>",
        examples=("{prefix}admin stop ow_picker",),
    ),
    "admin reload": CommandHelp(
        brief="Reload one cog",
        description="Reloads a cog by name, or starts it if it was not loaded.",
        usage="{prefix}admin reload <cog>",
        examples=("{prefix}admin reload typo_tax",),
    ),
    "admin reloadall": CommandHelp(
        brief="Reload every running cog",
        description="Reloads all loaded extensions and reports each result.",
        usage="{prefix}admin reloadall",
    ),
    "admin list": CommandHelp(
        brief="List cog states",
        description="Lists each cog file and whether it is running, enabled on startup, and disabled in this server.",
        usage="{prefix}admin list",
    ),
    "admin nuke": CommandHelp(
        brief="Delete recent channel messages",
        description="Bulk-deletes recent messages in the current channel. Owner only.",
        usage="{prefix}admin nuke <count>",
        examples=("{prefix}admin nuke 10",),
        notes=("Discord skips messages too old for bulk deletion.",),
    ),
    "admin restart": CommandHelp(
        brief="Restart the bot process",
        description="Flushes settings, closes Discord, and restarts the current Python process.",
        usage="{prefix}admin restart",
    ),

    "birthday": CommandHelp(
        brief="Track and announce birthdays",
        description="Store birthdays and post daily announcements to a configured channel.",
        usage="{prefix}birthday",
        subcommands=(
            "{prefix}birthday set <date>",
            "{prefix}birthday remove",
            "{prefix}birthday list",
            "{prefix}birthday setchannel <channel>",
            "{prefix}birthday announce [time|MM-DD]",
        ),
    ),
    "birthday set": CommandHelp(
        brief="Set your birthday",
        description="Stores your month and day. The year is ignored.",
        usage="{prefix}birthday set <date>",
        examples=("{prefix}birthday set 03-25", "{prefix}birthday set March 25"),
    ),
    "birthday remove": CommandHelp(
        brief="Remove your birthday",
        description="Deletes your stored birthday.",
        usage="{prefix}birthday remove",
    ),
    "birthday list": CommandHelp(
        brief="List server birthdays",
        description="Shows members with birthdays sorted by soonest upcoming.",
        usage="{prefix}birthday list",
    ),
    "birthday setchannel": CommandHelp(
        brief="Set birthday announcement channel",
        description="Sets where birthday announcements are posted. Bot owner only.",
        usage="{prefix}birthday setchannel <channel>",
        examples=("{prefix}birthday setchannel #birthdays",),
    ),
    "birthday announce": CommandHelp(
        brief="Trigger birthday announcements",
        description="Runs announcements now, for a date override, or schedules a UTC time.",
        usage="{prefix}birthday announce [time|MM-DD]",
        examples=("{prefix}birthday announce", "{prefix}birthday announce 04-04", "{prefix}birthday announce 9am"),
    ),

    "config": CommandHelp(
        brief="View and manage server configuration",
        description="Manage channels and feature settings for this server. Requires Manage Server.",
        usage="{prefix}config",
        subcommands=(
            "{prefix}config ranktracker <channel|off>",
            "{prefix}config quotebook <channel|off>",
            "{prefix}config translate mode <live|individual>",
            "{prefix}config linkclean toggle",
            "{prefix}config linkclean ignore",
            "{prefix}config linkclean status",
            "{prefix}config linkclean test <url>",
        ),
    ),
    "config ranktracker": CommandHelp(
        brief="Set rank tracker channel",
        description="Sets or disables the rank tracker announcement channel.",
        usage="{prefix}config ranktracker <channel|off>",
        examples=("{prefix}config ranktracker #rank-updates", "{prefix}config ranktracker off"),
    ),
    "config quotebook": CommandHelp(
        brief="Set quotebook channel",
        description="Sets or disables the channel where saved quotes are posted.",
        usage="{prefix}config quotebook <channel|off>",
        examples=("{prefix}config quotebook #quotebook", "{prefix}config quotebook off"),
    ),
    "config translate": CommandHelp(
        brief="Manage auto-translate config",
        description="Show or change server-level auto-translate settings.",
        usage="{prefix}config translate",
        subcommands=("{prefix}config translate mode <live|individual>",),
    ),
    "config translate mode": CommandHelp(
        brief="Set auto-translate mode",
        description="Choose live shared embeds or individual replies for translations.",
        usage="{prefix}config translate mode <live|individual>",
        examples=("{prefix}config translate mode live", "{prefix}config translate mode individual"),
    ),
    "config linkclean": CommandHelp(
        brief="Manage link cleaner config",
        description="Enable, disable, ignore channels, and test URL cleanup.",
        usage="{prefix}config linkclean",
        subcommands=(
            "{prefix}config linkclean toggle",
            "{prefix}config linkclean ignore",
            "{prefix}config linkclean status",
            "{prefix}config linkclean test <url>",
        ),
    ),
    "config linkclean toggle": CommandHelp(
        brief="Toggle link cleaner",
        description="Turns the link cleaner on or off for this server.",
        usage="{prefix}config linkclean toggle",
    ),
    "config linkclean ignore": CommandHelp(
        brief="Ignore or unignore this channel",
        description="Toggles whether link cleaning runs in the current channel.",
        usage="{prefix}config linkclean ignore",
    ),
    "config linkclean status": CommandHelp(
        brief="Show link cleaner status",
        description="Shows whether link cleaning is enabled and which channels are ignored.",
        usage="{prefix}config linkclean status",
    ),
    "config linkclean test": CommandHelp(
        brief="Preview URL cleanup",
        description="Shows which tracking parameters would be stripped from a URL.",
        usage="{prefix}config linkclean test <url>",
        examples=("{prefix}config linkclean test https://example.com/?utm_source=x",),
    ),

    "movie": CommandHelp(
        brief="Look up movies and where to watch",
        description="Searches TMDB and shows ratings, metadata, and US streaming availability.",
        usage="{prefix}movie <title>",
        examples=("{prefix}movie Alien", "{prefix}movie genre horror"),
        subcommands=("{prefix}movie genre <name>",),
    ),
    "movie genre": CommandHelp(
        brief="Browse popular movies by genre",
        description="Shows popular movies in a genre and lets you choose one for details.",
        usage="{prefix}movie genre <name>",
        examples=("{prefix}movie genre action", "{prefix}movie genre sci-fi"),
    ),

    "ow": CommandHelp(
        brief="Overwatch hero picker and profile tools",
        description="Pick heroes for modes, link battletags, and fetch public profile stats.",
        usage="{prefix}ow",
        subcommands=(
            "{prefix}ow qp [count]",
            "{prefix}ow stadium [count]",
            "{prefix}ow link [member] <battletag>",
            "{prefix}ow unlink [member]",
            "{prefix}ow linked",
            "{prefix}ow stats [member]",
            "{prefix}ow whois <query>",
        ),
    ),
    "ow qp": CommandHelp(
        brief="Pick Quickplay heroes",
        description="Randomly assigns Quickplay heroes using role counts or your voice channel.",
        usage="{prefix}ow qp [count|T-D-S|TDS]",
        examples=("{prefix}ow qp 6", "{prefix}ow qp 222", "{prefix}ow qp 2-2-2"),
    ),
    "ow stadium": CommandHelp(
        brief="Pick Stadium heroes",
        description="Runs an interactive role picker from voice chat or uses explicit role counts.",
        usage="{prefix}ow stadium [count|T-D-S|TDS]",
        examples=("{prefix}ow stadium", "{prefix}ow stadium 5", "{prefix}ow stadium 1-2-2"),
    ),
    "ow link": CommandHelp(
        brief="Link an Overwatch battletag",
        description="Links a battletag to you, or to another member if the bot owner runs it.",
        usage="{prefix}ow link [member] <Name#1234>",
        examples=("{prefix}ow link CoolPlayer#1234", "{prefix}ow link @Cash CoolPlayer#1234"),
    ),
    "ow unlink": CommandHelp(
        brief="Unlink an Overwatch battletag",
        description="Removes your linked battletag, or another member's if the bot owner runs it.",
        usage="{prefix}ow unlink [member]",
        examples=("{prefix}ow unlink", "{prefix}ow unlink @Cash"),
    ),
    "ow linked": CommandHelp(
        brief="List linked Overwatch accounts",
        description="Shows every member in this server with a linked battletag.",
        usage="{prefix}ow linked",
    ),
    "ow stats": CommandHelp(
        brief="Show linked Overwatch stats",
        description="Fetches ranks, time played, win rate, and top heroes for a linked profile.",
        usage="{prefix}ow stats [member]",
        examples=("{prefix}ow stats", "{prefix}ow stats @Cash"),
    ),
    "ow whois": CommandHelp(
        brief="Search any Overwatch profile",
        description="Looks up a player by battletag or partial name without requiring a Discord link.",
        usage="{prefix}ow whois <query>",
        examples=("{prefix}ow whois CoolPlayer#1234", "{prefix}ow whois CoolPlayer"),
    ),

    "quote": CommandHelp(
        brief="Save a message to the quotebook",
        description="Reply to a message with this command to save it to the configured quote channel.",
        usage="{prefix}quote [context]",
        examples=("{prefix}quote", "{prefix}quote 4", "{prefix}quote 1-3,5"),
        subcommands=("{prefix}quote random",),
    ),
    "quote random": CommandHelp(
        brief="Show a random saved quote",
        description="Displays a random quote from this server's quotebook.",
        usage="{prefix}quote random",
    ),

    "status": CommandHelp(
        brief="Manage rotating bot statuses",
        description="Owner-only commands for the bot presence rotation.",
        usage="{prefix}status",
        subcommands=("{prefix}status refresh",),
    ),
    "status refresh": CommandHelp(
        brief="Regenerate status messages",
        description="Runs the AI status refresh and reloads the generated status list.",
        usage="{prefix}status refresh",
    ),

    "sticker": CommandHelp(
        brief="Create a server sticker from an image",
        description="Finds an image, crops it for sticker use, and creates a guild sticker.",
        usage="{prefix}sticker [name]",
        examples=("{prefix}sticker", "{prefix}sticker funny name"),
        notes=("Reply to an image or use after a recent image in the channel.",),
    ),

    "translate": CommandHelp(
        brief="Show or change translation settings",
        description="Auto-translates non-English messages using Google Translate or Claude.",
        usage="{prefix}translate",
        subcommands=(
            "{prefix}translate mode <live|individual>",
            "{prefix}translate provider <google|claude>",
            "{prefix}translate lang <code>",
        ),
    ),
    "translate mode": CommandHelp(
        brief="Set translation display mode",
        description="Choose a shared live embed or individual replies per translated message.",
        usage="{prefix}translate mode <live|individual>",
        examples=("{prefix}translate mode live", "{prefix}translate mode individual"),
    ),
    "translate provider": CommandHelp(
        brief="Set translation provider",
        description="Choose Google Translate or Claude for auto-translation.",
        usage="{prefix}translate provider <google|claude>",
        examples=("{prefix}translate provider google", "{prefix}translate provider claude"),
    ),
    "translate lang": CommandHelp(
        brief="Set translation target language",
        description="Sets the target language code for auto-translation.",
        usage="{prefix}translate lang <code>",
        examples=("{prefix}translate lang en", "{prefix}translate lang es"),
    ),

    "trivia": CommandHelp(
        brief="Start a multiple-choice trivia round",
        description="Fetches a random Open Trivia DB question with clickable answer buttons.",
        usage="{prefix}trivia [easy|medium|hard]",
        examples=("{prefix}trivia", "{prefix}trivia hard"),
    ),

    "typotax": CommandHelp(
        brief="View typo debt and notification settings",
        description="Tracks typo debt, optional notifications, and repayment challenges.",
        usage="{prefix}typotax",
        subcommands=(
            "{prefix}typotax optin",
            "{prefix}typotax optout",
            "{prefix}typotax balance [member]",
            "{prefix}typotax repay",
            "{prefix}typotax leaderboard",
            "{prefix}typotax forgive <member> [amount]",
        ),
    ),
    "typotax optin": CommandHelp(
        brief="Enable typo-tax notifications",
        description="Turns on replies when your messages are taxed.",
        usage="{prefix}typotax optin",
    ),
    "typotax optout": CommandHelp(
        brief="Disable typo-tax notifications",
        description="Turns off typo-tax replies while continuing to track your balance.",
        usage="{prefix}typotax optout",
    ),
    "typotax balance": CommandHelp(
        brief="Show typo-tax balance",
        description="Shows your balance or another member's balance.",
        usage="{prefix}typotax balance [member]",
        examples=("{prefix}typotax balance", "{prefix}typotax balance @Cash"),
    ),
    "typotax leaderboard": CommandHelp(
        brief="Show typo-tax leaderboard",
        description="Lists members with the highest current typo-tax balances.",
        usage="{prefix}typotax leaderboard",
    ),
    "typotax repay": CommandHelp(
        brief="Repay one typo-tax point",
        description="Starts a repayment challenge. Correct answers remove one debt point.",
        usage="{prefix}typotax repay",
    ),
    "typotax forgive": CommandHelp(
        brief="Forgive typo-tax debt",
        description="Reduces a member's typo-tax balance. Requires Manage Messages.",
        usage="{prefix}typotax forgive <member> [amount]",
        examples=("{prefix}typotax forgive @Cash", "{prefix}typotax forgive @Cash 3"),
    ),

    "wordle": CommandHelp(
        brief="Play today's Wordle",
        description="Starts or resumes your personal daily Wordle board.",
        usage="{prefix}wordle",
        subcommands=("{prefix}wordle reset", "{prefix}wordle stats"),
    ),
    "wordle reset": CommandHelp(
        brief="Get a fresh Wordle word",
        description="Abandons your current word and starts another for today without affecting stats.",
        usage="{prefix}wordle reset",
    ),
    "wordle stats": CommandHelp(
        brief="Show your Wordle stats",
        description="Shows your lifetime Wordle record and guess distribution.",
        usage="{prefix}wordle stats",
    ),
}


def _prefix(ctx: commands.Context | None) -> str:
    return getattr(ctx, "clean_prefix", None) or "!"


def _format(value: str, prefix: str) -> str:
    return value.format(prefix=prefix)


def _fallback_help(command: commands.Command) -> CommandHelp:
    brief = command.brief or command.short_doc or f"Run {command.qualified_name}"
    description = command.help or command.description or brief
    usage = f"{{prefix}}{command.qualified_name} {command.signature}".strip()
    subcommands: tuple[str, ...] = ()
    if isinstance(command, commands.Group):
        subcommands = tuple(f"{{prefix}}{cmd.qualified_name} {cmd.signature}".strip() for cmd in command.commands)
    return CommandHelp(brief=brief, description=description, usage=usage, subcommands=subcommands)


def get_help(command: commands.Command) -> CommandHelp:
    return HELP_CONTENT.get(command.qualified_name, _fallback_help(command))


def _registered_meta(command_key: str) -> CommandHelp:
    try:
        return HELP_CONTENT[command_key]
    except KeyError as exc:
        raise KeyError(f"Missing CommandHelp entry for `{command_key}` in cogs/_help.py") from exc


def _metadata_kwargs(command_key: str, kwargs: dict) -> dict:
    meta = _registered_meta(command_key)
    cleaned = dict(kwargs)
    cleaned["brief"] = meta.brief
    cleaned["help"] = meta.description
    return cleaned


def helped_hybrid_command(command_key: str, **kwargs):
    return commands.hybrid_command(**_metadata_kwargs(command_key, kwargs))


def helped_hybrid_group(command_key: str, **kwargs):
    return commands.hybrid_group(**_metadata_kwargs(command_key, kwargs))


def helped_bot_hybrid_command(bot: commands.Bot, command_key: str, **kwargs):
    return bot.hybrid_command(**_metadata_kwargs(command_key, kwargs))


def helped_command(parent: commands.Group, command_key: str, **kwargs):
    return parent.command(**_metadata_kwargs(command_key, kwargs))


def helped_group(parent: commands.Group, command_key: str, **kwargs):
    return parent.group(**_metadata_kwargs(command_key, kwargs))


def apply_help_content(bot: commands.Bot) -> list[str]:
    missing: list[str] = []
    for command in bot.walk_commands():
        meta = HELP_CONTENT.get(command.qualified_name)
        if meta is None:
            missing.append(command.qualified_name)
            meta = _fallback_help(command)

        command.brief = meta.brief
        command.help = meta.description
        command.description = meta.brief
        if meta.usage:
            command.usage = meta.usage.replace("{prefix}", "")

        app_command = getattr(command, "app_command", None)
        if app_command is not None:
            app_command.description = meta.brief[:100]

    if missing:
        log.warning("Missing centralized help entries for: %s", ", ".join(sorted(missing)))
    return missing


def validate_hybrid_commands(bot: commands.Bot) -> list[str]:
    hybrid_types = tuple(
        cls for cls in (
            getattr(commands, "HybridCommand", None),
            getattr(commands, "HybridGroup", None),
        )
        if cls is not None
    )
    if not hybrid_types:
        return []

    non_hybrid = [
        command.qualified_name
        for command in bot.walk_commands()
        if not isinstance(command, hybrid_types)
    ]
    if non_hybrid:
        log.warning("Non-hybrid commands detected: %s", ", ".join(sorted(non_hybrid)))
    return non_hybrid


def build_help_embed(
    command: commands.Command,
    *,
    ctx: commands.Context | None = None,
    error: commands.CommandError | None = None,
) -> discord.Embed:
    prefix = _prefix(ctx)
    meta = get_help(command)
    title = f"Help: {prefix}{command.qualified_name}"
    if error:
        title = f"Missing Argument: {prefix}{command.qualified_name}"

    embed = discord.Embed(title=title, description=meta.description, color=0x5865F2)
    if error:
        embed.add_field(name="What happened", value=str(error), inline=False)
    if meta.usage:
        embed.add_field(name="Usage", value=f"`{_format(meta.usage, prefix)}`", inline=False)
    if meta.examples:
        embed.add_field(
            name="Examples",
            value="\n".join(f"`{_format(example, prefix)}`" for example in meta.examples),
            inline=False,
        )
    if meta.subcommands:
        embed.add_field(
            name="Subcommands",
            value="\n".join(f"`{_format(subcommand, prefix)}`" for subcommand in meta.subcommands),
            inline=False,
        )
    if meta.notes:
        embed.add_field(name="Notes", value="\n".join(meta.notes), inline=False)
    embed.set_footer(text=meta.brief)
    return embed


async def send_command_help(
    ctx: commands.Context,
    command: commands.Command | None = None,
    error: commands.CommandError | None = None,
) -> None:
    command = command or ctx.command
    if command is None:
        return

    embed = build_help_embed(command, ctx=ctx, error=error)
    try:
        await ctx.send(embed=embed, ephemeral=True)
    except TypeError:
        await ctx.send(embed=embed)


def build_bot_help_embed(bot: commands.Bot, ctx: commands.Context | None = None) -> discord.Embed:
    prefix = _prefix(ctx)
    visible = []
    for command in bot.commands:
        if command.hidden:
            continue
        visible.append(command)
    visible.sort(key=lambda item: item.name)

    lines = []
    for command in visible:
        meta = get_help(command)
        lines.append(f"`{prefix}{command.name}` - {meta.brief}")

    embed = discord.Embed(
        title="Pookie Bot Help",
        description="\n".join(lines) or "No commands are loaded.",
        color=0x5865F2,
    )
    embed.add_field(
        name="Command Details",
        value=f"Use `{prefix}help <command>` for full usage and examples.",
        inline=False,
    )
    return embed


async def send_bot_help(ctx: commands.Context, bot: commands.Bot) -> None:
    embed = build_bot_help_embed(bot, ctx)
    try:
        await ctx.send(embed=embed, ephemeral=True)
    except TypeError:
        await ctx.send(embed=embed)
