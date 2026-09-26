import os
import sys
import logging
from pathlib import Path

from discord.ext import commands

from cogs._help import documented_command, documented_hybrid_group, send_command_help
from cogs._guild_cogs import (
    PROTECTED_COGS,
    get_disabled_cogs,
    is_cog_disabled,
    normalize_cog_key,
    set_cog_disabled,
)

log = logging.getLogger(__name__)


def _cogs_path() -> Path:
    return Path(__file__).parent


def _resolve_cog_name(cog: str) -> str:
    """Strip .py extension and leading _ so callers can pass any variant."""
    return cog.removesuffix(".py").lstrip("_")


def get_all_cog_files() -> dict[str, tuple[Path, bool]]:
    """Return {logical_name: (path, enabled)} for every cog file.

    enabled=True  → file has no leading _ (loads on startup)
    enabled=False → file has a leading _ (skipped on startup)
    """
    result: dict[str, tuple[Path, bool]] = {}
    cogs_path = _cogs_path()
    for file in cogs_path.rglob("*.py"):
        if "__pycache__" in file.parts or file.name == "__init__.py":
            continue
        if "async def setup(" not in file.read_text(encoding="utf-8", errors="ignore"):
            continue
        relative = file.relative_to(cogs_path).with_suffix("")
        parts = list(relative.parts)
        enabled = not parts[-1].startswith("_")
        parts[-1] = parts[-1].lstrip("_")
        result[".".join(parts)] = (file, enabled)
    return result


def _format_disabled(disabled: list[str]) -> str:
    return ", ".join(f"`{name}`" for name in disabled) or "None"


def _match_cog_key(raw: str, all_files: dict[str, tuple[Path, bool]]) -> str:
    name = normalize_cog_key(_resolve_cog_name(raw))
    if name in all_files:
        return name

    matches = [key for key in all_files if key.endswith(f".{name}") or key.rsplit(".", 1)[-1] == name]
    if len(matches) == 1:
        return matches[0]
    return name


class Admin(commands.Cog, name="Admin"):
    """Bot owner-only administration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    async def _refresh_runtime_commands(self) -> None:
        refresh = getattr(self.bot, "refresh_command_metadata", None)
        if callable(refresh):
            refresh()
        await self.bot.tree.sync()

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    # ------------------------------------------------------------------ #
    #  Group root
    # ------------------------------------------------------------------ #

    @documented_hybrid_group(
        name="admin",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def admin(self, ctx: commands.Context):
        """Bot owner runtime admin commands

        Manage per-server cog availability, runtime loading, reloads, deletion, and restarts.

        Usage:
            {prefix}admin"""
        await send_command_help(ctx)

    # ------------------------------------------------------------------ #
    #  Per-guild availability
    # ------------------------------------------------------------------ #

    @documented_command(admin,
        name="enable",
    )
    async def enable_cog(self, ctx: commands.Context, cog: str):
        """Enable a cog in this server

        Removes a cog from this server's disabled list. The extension remains loaded globally.

        Usage:
            {prefix}admin enable <cog>

        Examples:
            {prefix}admin enable ow_picker"""
        if ctx.guild is None:
            await ctx.send("Use this in a server, or use `!admin globalenable <cog>` for startup behavior.")
            return

        all_files = get_all_cog_files()
        name = _match_cog_key(cog, all_files)

        if name not in all_files:
            await ctx.send(f"No cog named `{name}` found.")
            return

        if not is_cog_disabled(self.bot.settings, ctx.guild.id, name):
            await ctx.send(f"`{name}` is already enabled in **{ctx.guild.name}**.")
            return

        disabled = await set_cog_disabled(self.bot.settings, ctx.guild.id, name, False)
        await ctx.send(
            f"Enabled `{name}` in **{ctx.guild.name}**.\n"
            f"Disabled here: {_format_disabled(disabled)}"
        )
        log.info("Enabled cog %s in guild %s (requested by %s)", name, ctx.guild.id, ctx.author)

    @documented_command(admin,
        name="disable",
    )
    async def disable_cog(self, ctx: commands.Context, cog: str):
        """Disable a cog in this server

        Adds a cog to this server's disabled list without affecting other servers.

        Usage:
            {prefix}admin disable <cog>

        Examples:
            {prefix}admin disable ow_picker"""
        if ctx.guild is None:
            await ctx.send("Use this in a server, or use `!admin globaldisable <cog>` for startup behavior.")
            return

        all_files = get_all_cog_files()
        name = _match_cog_key(cog, all_files)

        if name not in all_files:
            await ctx.send(f"No cog named `{name}` found.")
            return
        if name in PROTECTED_COGS:
            await ctx.send(f"`{name}` cannot be disabled per server.")
            return

        if is_cog_disabled(self.bot.settings, ctx.guild.id, name):
            await ctx.send(f"`{name}` is already disabled in **{ctx.guild.name}**.")
            return

        disabled = await set_cog_disabled(self.bot.settings, ctx.guild.id, name, True)
        await ctx.send(
            f"Disabled `{name}` in **{ctx.guild.name}**.\n"
            f"Disabled here: {_format_disabled(disabled)}"
        )
        log.info("Disabled cog %s in guild %s (requested by %s)", name, ctx.guild.id, ctx.author)

    @documented_command(admin, name="globalenable")
    async def global_enable_cog(self, ctx: commands.Context, cog: str):
        """Enable a cog on startup globally

        Renames an underscored cog file so it loads on the next bot startup for every server.

        Usage:
            {prefix}admin globalenable <cog>

        Examples:
            {prefix}admin globalenable ow_picker"""
        all_files = get_all_cog_files()
        name = _match_cog_key(cog, all_files)

        if name not in all_files:
            await ctx.send(f"No cog named `{name}` found.")
            return

        path, enabled = all_files[name]
        if enabled:
            await ctx.send(f"`{name}` is already globally enabled.")
            return

        new_path = path.parent / f"{path.stem.lstrip('_')}.py"
        path.rename(new_path)
        await ctx.send(f"Globally enabled `{name}` - it will load on next startup.")
        log.info("Globally enabled cog %s (requested by %s)", name, ctx.author)

    @documented_command(admin, name="globaldisable")
    async def global_disable_cog(self, ctx: commands.Context, cog: str):
        """Disable a cog on startup globally

        Adds a leading underscore to a cog file so it is skipped on startup for every server.

        Usage:
            {prefix}admin globaldisable <cog>

        Examples:
            {prefix}admin globaldisable ow_picker"""
        all_files = get_all_cog_files()
        name = _match_cog_key(cog, all_files)

        if name not in all_files:
            await ctx.send(f"No cog named `{name}` found.")
            return
        if name in PROTECTED_COGS:
            await ctx.send(f"`{name}` cannot be globally disabled from Discord.")
            return

        path, enabled = all_files[name]
        if not enabled:
            await ctx.send(f"`{name}` is already globally disabled.")
            return

        new_path = path.parent / f"_{path.name}"
        path.rename(new_path)
        await ctx.send(f"Globally disabled `{name}` - it will be skipped on next startup.")
        log.info("Globally disabled cog %s (requested by %s)", name, ctx.author)

    # ------------------------------------------------------------------ #
    #  Runtime control — start / stop / reload
    # ------------------------------------------------------------------ #

    @documented_command(admin,
        name="start",
    )
    async def start_cog(self, ctx: commands.Context, cog: str):
        """Load a cog now

        Loads a cog into the running bot without changing startup behavior.

        Usage:
            {prefix}admin start <cog>

        Examples:
            {prefix}admin start ow_picker"""
        name = _match_cog_key(cog, get_all_cog_files())
        ext = f"cogs.{name}"
        try:
            await self.bot.load_extension(ext)
            await self._refresh_runtime_commands()
            await ctx.send(f"✅ Started `{name}`.")
            log.info("Started %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to start `{name}`: `{e}`")

    @documented_command(admin,
        name="stop",
    )
    async def stop_cog(self, ctx: commands.Context, cog: str):
        """Unload a cog now

        Unloads a running cog without changing startup behavior.

        Usage:
            {prefix}admin stop <cog>

        Examples:
            {prefix}admin stop ow_picker"""
        name = _match_cog_key(cog, get_all_cog_files())
        ext = f"cogs.{name}"
        try:
            await self.bot.unload_extension(ext)
            await self._refresh_runtime_commands()
            await ctx.send(f"✅ Stopped `{name}`.")
            log.info("Stopped %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to stop `{name}`: `{e}`")

    @documented_command(admin,
        name="reload",
    )
    async def reload_cog(self, ctx: commands.Context, cog: str):
        """Reload one cog

        Reloads a cog by name, or starts it if it was not loaded.

        Usage:
            {prefix}admin reload <cog>

        Examples:
            {prefix}admin reload typo_tax"""
        name = _match_cog_key(cog, get_all_cog_files())
        ext = f"cogs.{name}"
        try:
            await self.bot.reload_extension(ext)
            await self._refresh_runtime_commands()
            await ctx.send(f"✅ Reloaded `{name}`.")
            log.info("Reloaded %s (requested by %s)", ext, ctx.author)
        except commands.ExtensionNotLoaded:
            await ctx.send(f"⚠️ `{name}` wasn't running — starting it instead...")
            try:
                await self.bot.load_extension(ext)
                await self._refresh_runtime_commands()
                await ctx.send(f"✅ Started `{name}`.")
                log.info("Started %s (requested by %s)", ext, ctx.author)
            except Exception as e:
                await ctx.send(f"❌ Failed to start `{name}`: `{e}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{name}`: `{e}`")

    @documented_command(admin,
        name="reloadall",
    )
    async def reload_all(self, ctx: commands.Context):
        """Reload every running cog

        Reloads all loaded extensions and reports each result.

        Usage:
            {prefix}admin reloadall"""
        log.info("Reloading all cogs (requested by %s)", ctx.author)
        results = []
        for ext in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ `{ext}`")
            except Exception as e:
                results.append(f"❌ `{ext}` — {e}")
        await self._refresh_runtime_commands()
        await ctx.send("\n".join(results[:25]))

    @documented_command(admin,
        name="list",
    )
    async def list_cogs(self, ctx: commands.Context):
        """List cog states

        Lists each cog file and whether it is running, enabled on startup, and disabled in this server.

        Usage:
            {prefix}admin list"""
        log.info("Listing cogs (requested by %s)", ctx.author)
        all_files = get_all_cog_files()
        if not all_files:
            await ctx.send("No cog files found.")
            return

        guild_disabled = get_disabled_cogs(self.bot.settings, ctx.guild.id) if ctx.guild else []
        results = []
        for name, (_, enabled) in sorted(all_files.items()):
            running = f"cogs.{name}" in self.bot.extensions
            run_icon = "▶️" if running else "⏹"
            ena_icon = "✅" if enabled else "🚫"
            guild_icon = "🔕" if name in guild_disabled else "🔔"
            results.append(f"{run_icon} {ena_icon} {guild_icon} `{name}`")

        if ctx.guild:
            results.insert(0, f"Disabled in **{ctx.guild.name}**: {_format_disabled(guild_disabled)}")

        await ctx.send("\n".join(results[:25]))

    @documented_command(admin,
        name="nuke",
    )
    async def nuke(self, ctx: commands.Context, count: int):
        """Delete recent channel messages

        Bulk-deletes recent messages in the current channel. Owner only.

        Usage:
            {prefix}admin nuke <count>

        Examples:
            {prefix}admin nuke 10

        Notes:
            Discord skips messages too old for bulk deletion."""
        if count < 1 or count > 100:
            await ctx.send("❌ Count must be between 1 and 100.")
            return

        # +1 to include the command message itself, then purge removes it too
        deleted = await ctx.channel.purge(limit=count + 1)
        await ctx.send(f"🗑️ Deleted **{len(deleted)}** message(s).")
        log.info("Nuke: deleted %d message(s) in #%s (requested by %s)", len(deleted), ctx.channel, ctx.author)

    @documented_command(admin,
        name="restart",
    )
    async def restart_bot(self, ctx: commands.Context):
        """Restart the bot process

        Flushes settings, closes Discord, and restarts the current Python process.

        Usage:
            {prefix}admin restart"""
        await ctx.send("♻️ Restarting...")
        log.warning("Restart requested by %s", ctx.author)
        await self.bot.settings.flush_all()   # checkpoint WAL before exec replaces process
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))
