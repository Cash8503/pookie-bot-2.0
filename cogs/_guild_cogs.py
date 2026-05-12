from __future__ import annotations

from discord.ext import commands

NAMESPACE = "guild_cogs"
DISABLED_KEY = "disabled"
PROTECTED_COGS = {"admin"}


class GuildCogDisabled(commands.CheckFailure):
    def __init__(self, cog_key: str):
        self.cog_key = cog_key
        super().__init__(f"`{cog_key}` is disabled in this server.")


def normalize_cog_key(raw: str) -> str:
    cleaned = raw.strip().removesuffix(".py").replace("\\", ".").replace("/", ".")
    if cleaned.startswith("cogs."):
        cleaned = cleaned[len("cogs.") :]
    parts = [part.lstrip("_").lower() for part in cleaned.split(".") if part]
    return ".".join(parts)


def cog_key_from_module(module_name: str | None) -> str | None:
    if not module_name or not module_name.startswith("cogs."):
        return None
    return normalize_cog_key(module_name)


def cog_key_from_command(command: commands.Command | None) -> str | None:
    if command is None:
        return None
    return cog_key_from_module(getattr(command.callback, "__module__", None))


def cog_key_from_cog(cog: commands.Cog) -> str | None:
    return cog_key_from_module(cog.__class__.__module__)


def get_disabled_cogs(settings, guild_id: int) -> list[str]:
    raw = settings.get(guild_id, NAMESPACE, DISABLED_KEY, []) or []
    return sorted({normalize_cog_key(str(item)) for item in raw if str(item).strip()})


def is_cog_disabled(settings, guild_id: int, cog_key: str | None) -> bool:
    if not cog_key:
        return False
    normalized = normalize_cog_key(cog_key)
    if normalized in PROTECTED_COGS:
        return False
    return normalized in get_disabled_cogs(settings, guild_id)


async def set_cog_disabled(settings, guild_id: int, cog_key: str, disabled: bool) -> list[str]:
    normalized = normalize_cog_key(cog_key)
    current = set(get_disabled_cogs(settings, guild_id))
    if disabled:
        current.add(normalized)
    else:
        current.discard(normalized)
    disabled_cogs = sorted(current)
    await settings.set(guild_id, NAMESPACE, DISABLED_KEY, disabled_cogs)
    return disabled_cogs
