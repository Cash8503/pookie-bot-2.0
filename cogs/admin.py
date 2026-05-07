import os
import sys
import logging
from pathlib import Path

import discord
from discord.ext import commands

log = logging.getLogger(__name__)


def get_cog_files():
    cogs_path = Path(__file__).parent
    return {
        f"cogs.{file.stem}": file
        for file in cogs_path.glob("*.py")
        if not file.name.startswith("_")
    }


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
            "Subcommands:\n"
            "  reload <cog>  — Reload a specific cog by name\n"
            "  reloadall     — Reload every loaded extension\n"
            "  load <cog>    — Load a cog that isn't currently loaded\n"
            "  unload <cog>  — Unload a currently loaded cog\n"
            "  list          — List all cog files and their load status\n"
            "  restart       — Restart the bot process\n\n"
            "Cog names can be given with or without the .py extension.\n"
            "All commands are restricted to the bot owner."
        ),
    )
    async def admin(self, ctx: commands.Context):
        await ctx.send(
            "**Admin Commands** (owner only)\n"
            "`!admin reload <cog>`  — Reload a cog\n"
            "`!admin reloadall`     — Reload all cogs\n"
            "`!admin load <cog>`    — Load a cog\n"
            "`!admin unload <cog>`  — Unload a cog\n"
            "`!admin list`          — List all cogs and status\n"
            "`!admin restart`       — Restart the bot process\n\n"
            "Run `!help admin <subcommand>` for full details."
        )

    # ------------------------------------------------------------------ #
    #  Subcommands
    # ------------------------------------------------------------------ #

    @admin.command(
        name="reload",
        brief="Reload a cog",
        help=(
            "Reloads a cog by name. If the cog isn't currently loaded, "
            "attempts to load it instead.\n\n"
            "The .py extension is optional:\n"
            "  !admin reload link_cleaner\n"
            "  !admin reload link_cleaner.py  (same thing)"
        ),
    )
    async def reload_cog(self, ctx: commands.Context, cog: str):
        ext = f"cogs.{cog.removesuffix('.py')}"
        try:
            await self.bot.reload_extension(ext)
            await ctx.send(f"✅ Reloaded `{ext}`")
            log.info("Reloaded %s (requested by %s)", ext, ctx.author)
        except commands.ExtensionNotLoaded:
            await ctx.send(f"⚠️ `{ext}` wasn't loaded — trying to load it instead...")
            try:
                await self.bot.load_extension(ext)
                await ctx.send(f"✅ Loaded `{ext}`")
                log.info("Loaded %s (requested by %s)", ext, ctx.author)
            except Exception as e:
                await ctx.send(f"❌ Failed to load `{ext}`: `{e}`")
        except Exception as e:
            await ctx.send(f"❌ Failed to reload `{ext}`: `{e}`")

    @admin.command(
        name="reloadall",
        brief="Reload all loaded cogs",
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
        name="load",
        brief="Load a cog",
        help=(
            "Loads a cog that exists on disk but isn't currently loaded.\n\n"
            "The .py extension is optional:\n"
            "  !admin load ow_picker\n"
            "  !admin load ow_picker.py  (same thing)"
        ),
    )
    async def load_cog(self, ctx: commands.Context, cog: str):
        ext = f"cogs.{cog.removesuffix('.py')}"
        try:
            await self.bot.load_extension(ext)
            await ctx.send(f"✅ Loaded `{ext}`")
            log.info("Loaded %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to load `{ext}`: `{e}`")

    @admin.command(
        name="unload",
        brief="Unload a cog",
        help=(
            "Unloads a currently loaded cog without restarting the bot. "
            "The cog's commands and listeners are removed immediately.\n\n"
            "Use `!admin load <cog>` to bring it back without restarting."
        ),
    )
    async def unload_cog(self, ctx: commands.Context, cog: str):
        ext = f"cogs.{cog.removesuffix('.py')}"
        try:
            await self.bot.unload_extension(ext)
            await ctx.send(f"✅ Unloaded `{ext}`")
            log.info("Unloaded %s (requested by %s)", ext, ctx.author)
        except Exception as e:
            await ctx.send(f"❌ Failed to unload `{ext}`: `{e}`")

    @admin.command(
        name="list",
        brief="List all cogs and their load status",
        help=(
            "Lists every .py file in the cogs folder and shows whether "
            "each one is currently loaded (✅) or not (❌).\n\n"
            "Handy for spotting cogs that failed to load on startup."
        ),
    )
    async def list_cogs(self, ctx: commands.Context):
        log.info("Listing cogs (requested by %s)", ctx.author)
        files = get_cog_files()
        results = [
            f"✅ `{ext}`" if ext in self.bot.extensions else f"❌ `{ext}` — not loaded"
            for ext in files.keys()
        ]
        await ctx.send("\n".join(results[:25]) or "No cog files found.")

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