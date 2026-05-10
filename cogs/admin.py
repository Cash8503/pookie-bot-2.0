import os
import sys
import logging
from pathlib import Path

import discord
from discord.ext import commands

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
    for file in _cogs_path().glob("*.py"):
        if file.stem.startswith("_"):
            result[file.stem[1:]] = (file, False)
        else:
            result[file.stem] = (file, True)
    return result


class Admin(commands.Cog, name="Admin"):
    """Bot owner-only administration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    # ------------------------------------------------------------------ #
    #  Group root
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="admin",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Bot owner admin commands",
        help=(
            "Owner-only commands for managing the bot at runtime.\n\n"
            "Startup behavior (persists across restarts):\n"
            "  enable <cog>  — Mark cog to load on startup (removes _ prefix)\n"
            "  disable <cog> — Mark cog to skip on startup (adds _ prefix)\n\n"
            "Runtime control (temporary, no file changes):\n"
            "  start <cog>   — Load a cog right now\n"
            "  stop <cog>    — Unload a cog right now\n"
            "  reload <cog>  — Reload a specific cog\n"
            "  reloadall     — Reload every loaded extension\n\n"
            "Other:\n"
            "  list          — List all cogs, enabled/disabled, running/stopped\n"
            "  restart       — Restart the bot process\n\n"
            "All commands are restricted to the bot owner."
        ),
    )
    async def admin(self, ctx: commands.Context):
        await ctx.send(
            "**Admin Commands** (owner only)\n"
            "**Startup behavior**\n"
            "`!admin enable <cog>`  — Mark cog to load on startup\n"
            "`!admin disable <cog>` — Mark cog to skip on startup\n"
            "**Runtime control**\n"
            "`!admin start <cog>`   — Load a cog now\n"
            "`!admin stop <cog>`    — Unload a cog now\n"
            "`!admin reload <cog>`  — Reload a cog\n"
            "`!admin reloadall`     — Reload all cogs\n"
            "**Other**\n"
            "`!admin list`          — List all cogs and status\n"
            "`!admin restart`       — Restart the bot process\n\n"
            "Run `!help admin <subcommand>` for full details."
        )

    # ------------------------------------------------------------------ #
    #  Startup behavior — enable / disable
    # ------------------------------------------------------------------ #

    @admin.command(
        name="enable",
        brief="Mark a cog to load on startup",
        help=(
            "Removes the leading _ from the cog's filename so it will be loaded "
            "automatically on the next bot startup.\n\n"
            "Does not start the cog immediately — use `!admin start <cog>` for that.\n\n"
            "Example:\n"
            "  !admin enable ow_picker"
        ),
    )
    async def enable_cog(self, ctx: commands.Context, cog: str):
        name = _resolve_cog_name(cog)
        all_files = get_all_cog_files()

        if name not in all_files:
            await ctx.send(f"❌ No cog named `{name}` found.")
            return

        path, enabled = all_files[name]
        if enabled:
            await ctx.send(f"ℹ️ `{name}` is already enabled.")
            return

        new_path = path.parent / f"{name}.py"
        path.rename(new_path)
        await ctx.send(f"✅ Enabled `{name}` — it will load on next startup.")
        log.info("Enabled cog %s (requested by %s)", name, ctx.author)

    @admin.command(
        name="disable",
        brief="Mark a cog to skip on startup",
        help=(
            "Adds a leading _ to the cog's filename so it will be skipped "
            "automatically on the next bot startup.\n\n"
            "Does not stop the cog immediately — use `!admin stop <cog>` for that.\n\n"
            "Example:\n"
            "  !admin disable ow_picker"
        ),
    )
    async def disable_cog(self, ctx: commands.Context, cog: str):
        name = _resolve_cog_name(cog)
        all_files = get_all_cog_files()

        if name not in all_files:
            await ctx.send(f"❌ No cog named `{name}` found.")
            return

        path, enabled = all_files[name]
        if not enabled:
            await ctx.send(f"ℹ️ `{name}` is already disabled.")
            return

        new_path = path.parent / f"_{name}.py"
        path.rename(new_path)
        await ctx.send(f"✅ Disabled `{name}` — it will be skipped on next startup.")
        log.info("Disabled cog %s (requested by %s)", name, ctx.author)

    # ------------------------------------------------------------------ #
    #  Runtime control — start / stop / reload
    # ------------------------------------------------------------------ #

    @admin.command(
        name="start",
        brief="Load a cog now (temporary)",
        help=(
            "Loads a cog into the running bot without changing its startup behavior.\n\n"
            "To also make it load on restart, use `!admin enable <cog>` first.\n\n"
            "Example:\n"
            "  !admin start ow_picker"
        ),
    )
    async def start_cog(self, ctx: commands.Context, cog: str):
        name = _resolve_cog_name(cog)
        ext = f"cogs.{name}"
        try:
            await self.bot.load_extension(ext)
            await ctx.send(f"✅ Started `{name}`.")
            log.info("Started %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to start `{name}`: `{e}`")

    @admin.command(
        name="stop",
        brief="Unload a cog now (temporary)",
        help=(
            "Unloads a cog from the running bot without changing its startup behavior.\n\n"
            "To also prevent it from loading on restart, use `!admin disable <cog>`.\n\n"
            "Example:\n"
            "  !admin stop ow_picker"
        ),
    )
    async def stop_cog(self, ctx: commands.Context, cog: str):
        name = _resolve_cog_name(cog)
        ext = f"cogs.{name}"
        try:
            await self.bot.unload_extension(ext)
            await ctx.send(f"✅ Stopped `{name}`.")
            log.info("Stopped %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to stop `{name}`: `{e}`")

    @admin.command(
        name="reload",
        brief="Reload a cog",
        help=(
            "Reloads a cog by name. If the cog isn't currently loaded, "
            "attempts to load it instead.\n\n"
            "Example:\n"
            "  !admin reload link_cleaner"
        ),
    )
    async def reload_cog(self, ctx: commands.Context, cog: str):
        name = _resolve_cog_name(cog)
        ext = f"cogs.{name}"
        try:
            await self.bot.reload_extension(ext)
            await ctx.send(f"✅ Reloaded `{name}`.")
            log.info("Reloaded %s (requested by %s)", ext, ctx.author)
        except commands.ExtensionNotLoaded:
            await ctx.send(f"⚠️ `{name}` wasn't running — starting it instead...")
            try:
                await self.bot.load_extension(ext)
                await ctx.send(f"✅ Started `{name}`.")
                log.info("Started %s (requested by %s)", ext, ctx.author)
            except Exception as e:
                await ctx.send(f"❌ Failed to start `{name}`: `{e}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{name}`: `{e}`")

    @admin.command(
        name="reloadall",
        brief="Reload all running cogs",
        help=(
            "Reloads every currently loaded extension in one go.\n\n"
            "Reports the result for each cog — ✅ for success, ❌ for failure "
            "with the error message. Useful after pulling code changes."
        ),
    )
    async def reload_all(self, ctx: commands.Context):
        log.info("Reloading all cogs (requested by %s)", ctx.author)
        results = []
        for ext in list(self.bot.extensions.keys()):
            try:
                await self.bot.reload_extension(ext)
                results.append(f"✅ `{ext}`")
            except Exception as e:
                results.append(f"❌ `{ext}` — {e}")
        await ctx.send("\n".join(results[:25]))

    @admin.command(
        name="list",
        brief="List all cogs, their enabled state, and running state",
        help=(
            "Lists every cog file and shows two status flags:\n"
            "  ▶️  running now   |  ⏹  stopped\n"
            "  ✅  enabled (loads on startup)  |  🚫  disabled\n\n"
            "Example output:\n"
            "  ▶️ ✅ link_cleaner\n"
            "  ⏹ 🚫 ow_picker"
        ),
    )
    async def list_cogs(self, ctx: commands.Context):
        log.info("Listing cogs (requested by %s)", ctx.author)
        all_files = get_all_cog_files()
        if not all_files:
            await ctx.send("No cog files found.")
            return

        results = []
        for name, (_, enabled) in sorted(all_files.items()):
            running = f"cogs.{name}" in self.bot.extensions
            run_icon = "▶️" if running else "⏹"
            ena_icon = "✅" if enabled else "🚫"
            results.append(f"{run_icon} {ena_icon} `{name}`")

        await ctx.send("\n".join(results[:25]))

    @admin.command(
        name="nuke",
        brief="Delete the last N messages in this channel",
        help=(
            "Bulk-deletes the last N messages in the current channel (max 100).\n\n"
            "Discord only allows bulk-deleting messages under 14 days old. "
            "Messages older than that are skipped.\n\n"
            "Examples:\n"
            "  !admin nuke 10   — delete the last 10 messages\n"
            "  !admin nuke 50   — delete the last 50 messages"
        ),
    )
    async def nuke(self, ctx: commands.Context, count: int):
        if count < 1 or count > 100:
            await ctx.send("❌ Count must be between 1 and 100.")
            return

        # +1 to include the command message itself, then purge removes it too
        deleted = await ctx.channel.purge(limit=count + 1)
        await ctx.send(f"🗑️ Deleted **{len(deleted)}** message(s).")
        log.info("Nuke: deleted %d message(s) in #%s (requested by %s)", len(deleted), ctx.channel, ctx.author)

    @admin.command(
        name="restart",
        brief="Restart the bot process",
        help=(
            "Gracefully closes the bot and restarts the process using os.execv.\n\n"
            "This is a hard restart — the same as killing and relaunching the script. "
            "All in-memory state (guild settings, hero cache, etc.) will be reset."
        ),
    )
    async def restart_bot(self, ctx: commands.Context):
        await ctx.send("♻️ Restarting...")
        log.warning("Restart requested by %s", ctx.author)
        await self.bot.settings.flush_all()   # checkpoint WAL before exec replaces process
        await self.bot.close()
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def setup(bot: commands.Bot):
    await bot.add_cog(Admin(bot))