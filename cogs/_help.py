from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands

from cogs._guild_cogs import cog_key_from_cog, is_cog_disabled

log = logging.getLogger(__name__)

_META_KEY = "pookie_help"
_SECTION_RE = re.compile(r"^(usage|arguments?|examples?|notes?):\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class CommandHelp:
    brief: str
    description: str
    usage: str | None = None
    examples: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()
    arguments: dict[str, str] = field(default_factory=dict)


def _clean_section_lines(lines: list[str]) -> tuple[str, ...]:
    cleaned: list[str] = []
    for line in lines:
        value = line.strip()
        if not value:
            continue
        cleaned.append(re.sub(r"^[-*]\s+", "", value))
    return tuple(cleaned)


def parse_help_doc(doc: str | None) -> CommandHelp | None:
    """Parse a command docstring into help metadata."""
    if not doc or not doc.strip():
        return None

    lines = inspect.cleandoc(doc).splitlines()
    while lines and not lines[0].strip():
        lines.pop(0)
    if not lines:
        return None

    brief = lines[0].strip()
    body: list[str] = []
    sections: dict[str, list[str]] = {
        "usage": [],
        "arguments": [],
        "examples": [],
        "notes": [],
    }
    current: str | None = None

    for raw in lines[1:]:
        match = _SECTION_RE.match(raw.strip())
        if match:
            name = match.group(1).lower()
            if name.startswith("argument"):
                current = "arguments"
            elif name.startswith("example"):
                current = "examples"
            elif name.startswith("note"):
                current = "notes"
            else:
                current = "usage"
            continue
        if current is None:
            body.append(raw.rstrip())
        else:
            sections[current].append(raw.rstrip())

    description = "\n".join(body).strip() or brief
    usage_lines = _clean_section_lines(sections["usage"])
    examples = _clean_section_lines(sections["examples"])
    notes = _clean_section_lines(sections["notes"])

    arguments: dict[str, str] = {}
    last_name: str | None = None
    for raw in sections["arguments"]:
        value = raw.strip()
        if not value:
            continue
        if ":" in value:
            name, description_text = value.split(":", 1)
            name = name.strip().strip("`")
            if name:
                arguments[name] = description_text.strip()
                last_name = name
        elif last_name:
            arguments[last_name] = f"{arguments[last_name]} {value}".strip()

    return CommandHelp(
        brief=brief,
        description=description,
        usage=usage_lines[0] if usage_lines else None,
        examples=examples,
        notes=notes,
        arguments=arguments,
    )


def _fallback_help(command: commands.Command) -> CommandHelp:
    brief = command.brief or command.short_doc or f"Run {command.qualified_name}"
    description = command.help or command.description or brief
    usage = f"{{prefix}}{command.qualified_name} {command.signature}".strip()
    return CommandHelp(brief=brief, description=description, usage=usage)


def get_help(command: commands.Command) -> CommandHelp:
    stored = command.extras.get(_META_KEY)
    if isinstance(stored, CommandHelp):
        return stored
    parsed = parse_help_doc(getattr(command.callback, "__doc__", None))
    return parsed or _fallback_help(command)


def _documented_kwargs(callback, kwargs: dict) -> tuple[object, dict]:
    meta = parse_help_doc(getattr(callback, "__doc__", None))
    if meta is None:
        raise ValueError(
            f"Command callback {callback.__module__}.{callback.__qualname__} "
            "must have a documented help docstring."
        )

    cleaned = dict(kwargs)
    extras = dict(cleaned.pop("extras", {}) or {})
    extras[_META_KEY] = meta
    cleaned["extras"] = extras
    cleaned.setdefault("brief", meta.brief)
    cleaned.setdefault("help", meta.description)
    cleaned.setdefault("description", meta.brief[:100])
    if meta.usage:
        cleaned.setdefault("usage", meta.usage.replace("{prefix}", ""))

    if meta.arguments:
        callback = app_commands.describe(**meta.arguments)(callback)
    return callback, cleaned


def documented_hybrid_command(**kwargs):
    def decorator(callback):
        documented, cleaned = _documented_kwargs(callback, kwargs)
        return commands.hybrid_command(**cleaned)(documented)

    return decorator


def documented_hybrid_group(**kwargs):
    def decorator(callback):
        documented, cleaned = _documented_kwargs(callback, kwargs)
        return commands.hybrid_group(**cleaned)(documented)

    return decorator


def documented_bot_hybrid_command(bot: commands.Bot, **kwargs):
    def decorator(callback):
        documented, cleaned = _documented_kwargs(callback, kwargs)
        return bot.hybrid_command(**cleaned)(documented)

    return decorator


def documented_command(parent: commands.Group, **kwargs):
    def decorator(callback):
        documented, cleaned = _documented_kwargs(callback, kwargs)
        return parent.command(**cleaned)(documented)

    return decorator


def documented_group(parent: commands.Group, **kwargs):
    def decorator(callback):
        documented, cleaned = _documented_kwargs(callback, kwargs)
        return parent.group(**cleaned)(documented)

    return decorator


def apply_documented_help(bot: commands.Bot) -> list[str]:
    """Reapply local command docs and report callbacks without documentation."""
    missing: list[str] = []
    for command in bot.walk_commands():
        meta = parse_help_doc(getattr(command.callback, "__doc__", None))
        if meta is None:
            missing.append(command.qualified_name)
            meta = _fallback_help(command)
        else:
            command.extras[_META_KEY] = meta

        command.brief = meta.brief
        command.help = meta.description
        command.description = meta.brief
        if meta.usage:
            command.usage = meta.usage.replace("{prefix}", "")

        app_command = getattr(command, "app_command", None)
        if app_command is not None:
            app_command.description = meta.brief[:100]

    if missing:
        log.warning("Commands without local help docs: %s", ", ".join(sorted(missing)))
    return missing


def validate_hybrid_commands(bot: commands.Bot) -> list[str]:
    hybrid_types = tuple(
        cls
        for cls in (
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


def _prefix(ctx: commands.Context | None) -> str:
    return getattr(ctx, "clean_prefix", None) or "!"


def _format(value: str, prefix: str) -> str:
    try:
        return value.format(prefix=prefix)
    except (KeyError, ValueError):
        return value


def _command_usage(command: commands.Command, meta: CommandHelp, prefix: str) -> str:
    if meta.usage:
        return _format(meta.usage, prefix)
    signature = f" {command.signature}" if command.signature else ""
    return f"{prefix}{command.qualified_name}{signature}"


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
        title = f"Command Help: {prefix}{command.qualified_name}"

    embed = discord.Embed(title=title[:256], description=meta.description[:4096], color=0x5865F2)
    if error:
        embed.add_field(name="What happened", value=str(error)[:1024], inline=False)
    embed.add_field(
        name="Usage",
        value=f"`{_command_usage(command, meta, prefix)}`"[:1024],
        inline=False,
    )
    if meta.arguments:
        argument_text = "\n".join(f"`{name}` — {value}" for name, value in meta.arguments.items())
        embed.add_field(name="Arguments", value=argument_text[:1024], inline=False)
    if meta.examples:
        examples = "\n".join(f"`{_format(example, prefix)}`" for example in meta.examples)
        embed.add_field(name="Examples", value=examples[:1024], inline=False)
    if isinstance(command, commands.Group) and command.commands:
        lines = []
        for subcommand in sorted(command.commands, key=lambda item: item.name):
            submeta = get_help(subcommand)
            lines.append(f"`{prefix}{subcommand.qualified_name}` — {submeta.brief}")
        embed.add_field(name="Subcommands", value="\n".join(lines)[:1024], inline=False)
    if meta.notes:
        embed.add_field(name="Notes", value="\n".join(meta.notes)[:1024], inline=False)
    embed.set_footer(text=meta.brief[:2048])
    return embed


async def send_command_help(
    ctx: commands.Context,
    command: commands.Command | None = None,
    error: commands.CommandError | None = None,
) -> None:
    command = command or ctx.command
    if command is None:
        return
    await ctx.send(embed=build_help_embed(command, ctx=ctx, error=error), ephemeral=True)


def _normalise_topic(value: str) -> str:
    return re.sub(r"[\s_.-]+", "", value).lower()


def find_cog(bot: commands.Bot, topic: str) -> commands.Cog | None:
    wanted = _normalise_topic(topic)
    for cog in bot.cogs.values():
        module_name = cog.__class__.__module__.removeprefix("cogs.")
        candidates = {
            _normalise_topic(cog.qualified_name),
            _normalise_topic(cog.__class__.__name__.removesuffix("Cog")),
            _normalise_topic(module_name),
            _normalise_topic(module_name.rsplit(".", 1)[-1]),
        }
        if wanted in candidates:
            return cog
    return None


async def command_is_visible(command: commands.Command, ctx: commands.Context) -> bool:
    if command.hidden:
        return False
    old_command = ctx.command
    try:
        ctx.command = command
        return await command.can_run(ctx)
    except commands.CommandError:
        return False
    except Exception:
        log.debug("Help visibility check failed for %s", command.qualified_name, exc_info=True)
        return False
    finally:
        ctx.command = old_command


async def cog_is_visible(cog: commands.Cog, ctx: commands.Context) -> bool:
    if getattr(cog, "help_hidden", False) and not await ctx.bot.is_owner(ctx.author):
        return False
    if ctx.guild is not None and hasattr(ctx.bot, "settings"):
        cog_key = cog_key_from_cog(cog)
        if is_cog_disabled(ctx.bot.settings, ctx.guild.id, cog_key):
            return False
    commands_for_cog = cog.get_commands()
    if commands_for_cog:
        return any([await command_is_visible(command, ctx) for command in commands_for_cog])
    return True


async def build_plugin_help_embed(cog: commands.Cog, ctx: commands.Context) -> discord.Embed:
    description = inspect.getdoc(cog.__class__) or "No plugin description provided."
    embed = discord.Embed(
        title=f"{cog.qualified_name} Plugin",
        description=description[:4096],
        color=0x5865F2,
    )
    commands_for_cog: list[commands.Command] = []
    for command in cog.get_commands():
        if await command_is_visible(command, ctx):
            commands_for_cog.append(command)
    if commands_for_cog:
        prefix = _prefix(ctx)
        lines = [
            f"`{prefix}{command.qualified_name}` — {get_help(command).brief}"
            for command in sorted(commands_for_cog, key=lambda item: item.qualified_name)
        ]
        embed.add_field(name="Commands", value="\n".join(lines)[:1024], inline=False)
        embed.set_footer(text=f"Use {prefix}help <command> for detailed usage.")
    else:
        embed.add_field(
            name="Commands",
            value="This plugin runs automatically or is configured through another plugin.",
            inline=False,
        )
    return embed


async def build_bot_help_embed(bot: commands.Bot, ctx: commands.Context) -> discord.Embed:
    prefix = _prefix(ctx)
    uncategorised: list[commands.Command] = []
    fields: list[tuple[str, str]] = []

    for cog in sorted(bot.cogs.values(), key=lambda item: item.qualified_name.lower()):
        if not await cog_is_visible(cog, ctx):
            continue
        all_commands = cog.get_commands()
        visible = [command for command in all_commands if await command_is_visible(command, ctx)]
        if all_commands and not visible:
            continue
        if visible:
            text = "\n".join(
                f"`{prefix}{command.name}` — {get_help(command).brief}"
                for command in sorted(visible, key=lambda item: item.name)
            )
        else:
            topic = cog.__class__.__module__.removeprefix("cogs.")
            text = f"Automatic plugin — `{prefix}help {topic}`"
        fields.append((cog.qualified_name, text))

    for command in bot.commands:
        if command.cog is None and await command_is_visible(command, ctx):
            uncategorised.append(command)
    if uncategorised:
        text = "\n".join(
            f"`{prefix}{command.name}` — {get_help(command).brief}"
            for command in sorted(uncategorised, key=lambda item: item.name)
        )
        fields.insert(0, ("General", text))

    embed = discord.Embed(
        title="Pookie Bot Help",
        description=(
            f"Use `{prefix}help <plugin>` for a plugin overview or "
            f"`{prefix}help <command>` for detailed usage."
        ),
        color=0x5865F2,
    )
    for name, value in fields[:25]:
        embed.add_field(name=name[:256], value=value[:1024], inline=False)
    return embed


async def send_bot_help(ctx: commands.Context, bot: commands.Bot) -> None:
    await ctx.send(embed=await build_bot_help_embed(bot, ctx), ephemeral=True)
