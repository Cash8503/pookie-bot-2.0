import importlib
import inspect
from pathlib import Path

from discord.ext import commands


def _cog_command_objects():
    root = Path(__file__).parents[1]
    seen = set()
    for path in sorted((root / "cogs").rglob("*.py")):
        if path.name == "__init__.py":
            continue
        relative = path.relative_to(root).with_suffix("")
        module_name = ".".join(relative.parts)
        module = importlib.import_module(module_name)
        for candidate in vars(module).values():
            if not inspect.isclass(candidate) or candidate.__module__ != module_name:
                continue
            if not issubclass(candidate, commands.Cog):
                continue
            for command in getattr(candidate, "__cog_commands__", ()):
                identity = id(command)
                if identity not in seen:
                    seen.add(identity)
                    yield f"{module_name}:{command.qualified_name}", command


def test_every_command_has_prefix_and_slash_registration():
    import bot as bot_module

    command_objects = list(_cog_command_objects())
    command_objects.extend(
        (f"bot:{command.qualified_name}", command)
        for command in bot_module.bot.walk_commands()
    )

    assert len(command_objects) >= 95
    invalid = []
    for label, command in command_objects:
        if not isinstance(command, (commands.HybridCommand, commands.HybridGroup)):
            invalid.append(f"{label} is {type(command).__name__}")
            continue
        if command.app_command is None:
            invalid.append(f"{label} has no slash command")
        elif command.app_command.name != command.name:
            invalid.append(
                f"{label} prefix name {command.name!r} != slash name {command.app_command.name!r}"
            )
        if command.aliases:
            invalid.append(f"{label} has prefix-only aliases: {command.aliases!r}")

    assert invalid == []
