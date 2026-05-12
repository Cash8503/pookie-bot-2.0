import sys
import os
import json
from pathlib import Path
import asyncio
import logging

import aiosqlite
import discord
from discord.ext import commands
from dotenv import load_dotenv

from cogs._help import (
    apply_help_content,
    helped_bot_hybrid_command,
    send_bot_help,
    send_command_help,
    validate_hybrid_commands,
)
from cogs._guild_cogs import GuildCogDisabled, cog_key_from_command, cog_key_from_module, is_cog_disabled

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s | %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


# ---------------------------------------------------------------------------
# Settings manager — in-memory cache backed by SQLite
# ---------------------------------------------------------------------------

class SettingsManager:
    """Per-guild settings store. Reads from memory; writes through to SQLite."""

    DB_PATH = Path(__file__).parent / "data" / "data.db"

    def __init__(self):
        self._cache: dict[int, dict[str, dict[str, object]]] = {}
        self._user_cache: dict[int, dict[str, dict[str, object]]] = {}
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.DB_PATH)
        # WAL mode survives hard crashes better than the default journal mode
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.executescript("""
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id  INTEGER NOT NULL,
                namespace TEXT    NOT NULL,
                key       TEXT    NOT NULL,
                value     TEXT    NOT NULL,
                PRIMARY KEY (guild_id, namespace, key)
            );
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id   INTEGER NOT NULL,
                namespace TEXT    NOT NULL,
                key       TEXT    NOT NULL,
                value     TEXT    NOT NULL,
                PRIMARY KEY (user_id, namespace, key)
            );
        """)
        await self._db.commit()

        async with self._db.execute(
            "SELECT guild_id, namespace, key, value FROM guild_settings"
        ) as cursor:
            async for guild_id, namespace, key, value in cursor:
                self._cache.setdefault(guild_id, {}).setdefault(namespace, {})[key] = json.loads(value)

        async with self._db.execute(
            "SELECT user_id, namespace, key, value FROM user_settings"
        ) as cursor:
            async for user_id, namespace, key, value in cursor:
                self._user_cache.setdefault(user_id, {}).setdefault(namespace, {})[key] = json.loads(value)

        log.info(
            "SettingsManager: loaded %d guild(s) and %d user(s) from DB.",
            len(self._cache), len(self._user_cache),
        )

    async def flush_all(self) -> None:
        """Write every cached entry to the DB. Called periodically and on shutdown."""
        if not self._db:
            return
        try:
            guild_rows = [
                (gid, ns, k, json.dumps(v))
                for gid, namespaces in self._cache.items()
                for ns, keys in namespaces.items()
                for k, v in keys.items()
            ]
            user_rows = [
                (uid, ns, k, json.dumps(v))
                for uid, namespaces in self._user_cache.items()
                for ns, keys in namespaces.items()
                for k, v in keys.items()
            ]
            if guild_rows:
                await self._db.executemany(
                    """INSERT INTO guild_settings (guild_id, namespace, key, value)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(guild_id, namespace, key) DO UPDATE SET value = excluded.value""",
                    guild_rows,
                )
            if user_rows:
                await self._db.executemany(
                    """INSERT INTO user_settings (user_id, namespace, key, value)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(user_id, namespace, key) DO UPDATE SET value = excluded.value""",
                    user_rows,
                )
            await self._db.commit()
            log.debug("SettingsManager: flush_all wrote %d guild row(s) and %d user row(s).", len(guild_rows), len(user_rows))
        except Exception:
            log.exception("SettingsManager: flush_all failed")

    async def close(self):
        await self.flush_all()
        if self._db:
            await self._db.close()
            self._db = None

    # -- Guild settings ------------------------------------------------------

    def get(self, guild_id: int, namespace: str, key: str, default=None):
        return self._cache.get(guild_id, {}).get(namespace, {}).get(key, default)

    async def set(self, guild_id: int, namespace: str, key: str, value) -> None:
        self._cache.setdefault(guild_id, {}).setdefault(namespace, {})[key] = value
        try:
            await self._db.execute(
                """
                INSERT INTO guild_settings (guild_id, namespace, key, value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, namespace, key) DO UPDATE SET value = excluded.value
                """,
                (guild_id, namespace, key, json.dumps(value)),
            )
            await self._db.commit()
        except Exception:
            log.exception("SettingsManager: failed to persist guild setting (%s, %s, %s)", guild_id, namespace, key)

    # -- User settings -------------------------------------------------------

    def get_user(self, user_id: int, namespace: str, key: str, default=None):
        return self._user_cache.get(user_id, {}).get(namespace, {}).get(key, default)

    async def set_user(self, user_id: int, namespace: str, key: str, value) -> None:
        self._user_cache.setdefault(user_id, {}).setdefault(namespace, {})[key] = value
        try:
            await self._db.execute(
                """
                INSERT INTO user_settings (user_id, namespace, key, value)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, namespace, key) DO UPDATE SET value = excluded.value
                """,
                (user_id, namespace, key, json.dumps(value)),
            )
            await self._db.commit()
        except Exception:
            log.exception("SettingsManager: failed to persist user setting (%s, %s, %s)", user_id, namespace, key)

    async def delete(self, guild_id: int, namespace: str, key: str) -> None:
        self._cache.get(guild_id, {}).get(namespace, {}).pop(key, None)
        await self._db.execute(
            "DELETE FROM guild_settings WHERE guild_id=? AND namespace=? AND key=?",
            (guild_id, namespace, key),
        )
        await self._db.commit()

    async def delete_user(self, user_id: int, namespace: str, key: str) -> None:
        self._user_cache.get(user_id, {}).get(namespace, {}).pop(key, None)
        await self._db.execute(
            "DELETE FROM user_settings WHERE user_id=? AND namespace=? AND key=?",
            (user_id, namespace, key),
        )
        await self._db.commit()

    async def clear(self, guild_id: int, namespace: str | None = None) -> None:
        """Delete all guild settings for an ID, optionally scoped to a namespace."""
        if namespace:
            self._cache.get(guild_id, {}).pop(namespace, None)
            await self._db.execute(
                "DELETE FROM guild_settings WHERE guild_id=? AND namespace=?",
                (guild_id, namespace),
            )
        else:
            self._cache.pop(guild_id, None)
            await self._db.execute(
                "DELETE FROM guild_settings WHERE guild_id=?",
                (guild_id,),
            )
        await self._db.commit()

    async def clear_user(self, user_id: int, namespace: str | None = None) -> None:
        """Delete all user settings for an ID, optionally scoped to a namespace."""
        if namespace:
            self._user_cache.get(user_id, {}).pop(namespace, None)
            await self._db.execute(
                "DELETE FROM user_settings WHERE user_id=? AND namespace=?",
                (user_id, namespace),
            )
        else:
            self._user_cache.pop(user_id, None)
            await self._db.execute(
                "DELETE FROM user_settings WHERE user_id=?",
                (user_id,),
            )
        await self._db.commit()


# ---------------------------------------------------------------------------
# Cog discovery
# ---------------------------------------------------------------------------

def _is_loadable_cog_file(path: Path) -> bool:
    if path.name == "__init__.py" or path.name.startswith("_") or "__pycache__" in path.parts:
        return False
    return "async def setup(" in path.read_text(encoding="utf-8", errors="ignore")


def get_cogs():
    cogs_path = Path(__file__).parent / "cogs"
    return [
        "cogs." + ".".join(f.relative_to(cogs_path).with_suffix("").parts)
        for f in cogs_path.rglob("*.py")
        if _is_loadable_cog_file(f)
    ]


def _get_cog_files() -> dict[str, Path]:
    cogs_path = Path(__file__).parent / "cogs"
    return {
        "cogs." + ".".join(f.relative_to(cogs_path).with_suffix("").parts): f
        for f in cogs_path.rglob("*.py")
        if _is_loadable_cog_file(f)
    }


intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(
    command_prefix=os.getenv("PREFIX", "!"),
    intents=intents,
    case_insensitive=True,
    help_command=None,
)

_reload_task = None
_flush_task  = None
_sync_task   = None
_file_mtimes: dict[str, float] = {}


@helped_bot_hybrid_command(bot, "help", name="help")
async def help_command(ctx: commands.Context, *, command: str | None = None):
    if not command:
        await send_bot_help(ctx, bot)
        return

    target = bot.get_command(command.strip().lower())
    if target is None:
        await ctx.send(f"No command named `{command}` was found.", ephemeral=True)
        return
    await send_command_help(ctx, target)


@bot.check
async def guild_cog_enabled_check(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        return True
    cog_key = cog_key_from_command(ctx.command)
    if is_cog_disabled(bot.settings, ctx.guild.id, cog_key):
        raise GuildCogDisabled(cog_key or "unknown")
    return True


async def guild_cog_enabled_interaction_check(interaction: discord.Interaction) -> bool:
    if interaction.guild is None or interaction.command is None:
        return True

    command = interaction.command
    binding = getattr(command, "binding", None)
    if binding is not None:
        cog_key = cog_key_from_module(binding.__class__.__module__)
    else:
        callback = getattr(command, "callback", None)
        cog_key = cog_key_from_module(getattr(callback, "__module__", None))

    if not is_cog_disabled(bot.settings, interaction.guild.id, cog_key):
        return True

    message = f"`{cog_key}` is disabled in this server."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        log.warning("Failed to send disabled-cog slash response for %s", cog_key)
    return False


bot.tree.interaction_check = guild_cog_enabled_interaction_check


def refresh_command_metadata() -> None:
    """Apply centralized command help and warn if commands drift from policy."""
    missing = apply_help_content(bot)
    non_hybrid = validate_hybrid_commands(bot)
    if missing:
        log.warning("Command help metadata is missing for %d command(s).", len(missing))
    if non_hybrid:
        log.warning("Found %d non-hybrid command(s).", len(non_hybrid))


async def periodic_flush():
    """Flush the full in-memory settings cache to DB every 5 minutes."""
    while not bot.is_closed():
        await asyncio.sleep(300)
        await bot.settings.flush_all()


async def sync_server_task():
    """Run the file sync server as a background task inside the bot process."""
    sync_dir = Path(__file__).parent / "sync"
    if str(sync_dir) not in sys.path:
        sys.path.insert(0, str(sync_dir))
    try:
        from sync_core import ROOT, run_session, load_config
        log.info("Sync server: imported sync_core successfully.")
    except ImportError as e:
        log.warning("Sync server disabled — install watchdog in the bot venv: pip install watchdog (%s)", e)
        return
    except Exception as e:
        log.error("Sync server failed to import: %s", e)
        return

    try:
        cfg  = load_config()
        port = cfg.get("sync_port", 7789)
    except Exception as e:
        log.error("Sync server: could not load sync_config.json: %s", e)
        return

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        addr = writer.get_extra_info("peername")
        log.info("Sync: client connected from %s", addr)
        try:
            await run_session(reader, writer, side="server", root=ROOT)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("Sync: client disconnected (%s)", addr)

    try:
        server = await asyncio.start_server(_handle, "0.0.0.0", port, limit=64 * 1024 * 1024)
        log.info("Sync server listening on port %d", port)
        async with server:
            await server.serve_forever()
    except OSError as e:
        log.error("Sync server could not bind to port %d: %s", port, e)
    except Exception as e:
        log.error("Sync server crashed: %s", e)


@bot.event
async def on_ready():
    global _reload_task, _flush_task, _sync_task

    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    try:
        refresh_command_metadata()
        synced = await bot.tree.sync()
        log.info(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        log.error(f"Failed to sync commands: {e}")

    if _reload_task is None:
        _reload_task = asyncio.create_task(watch_cogs())
        log.info("Started cog file watcher.")

    if _flush_task is None:
        _flush_task = asyncio.create_task(periodic_flush())
        log.info("Started periodic DB flush (every 5 min).")

    if _sync_task is None:
        _sync_task = asyncio.create_task(sync_server_task())
        log.info("Started sync server task.")


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandNotFound):
        return
    if isinstance(error, commands.MissingRequiredArgument):
        await send_command_help(ctx, error=error)
        return
    if isinstance(error, GuildCogDisabled):
        await ctx.send(str(error), ephemeral=True)
        return
    log.error(f"Command error in {ctx.command}: {error}")


async def watch_cogs():
    """Poll the cogs folder for file changes and hot-reload on save."""
    global _file_mtimes

    await bot.wait_until_ready()

    cog_files = _get_cog_files()
    _file_mtimes = {}
    for ext, path in cog_files.items():
        try:
            _file_mtimes[ext] = path.stat().st_mtime
        except FileNotFoundError:
            continue

    while not bot.is_closed():
        await asyncio.sleep(2)

        cog_files = _get_cog_files()
        known_exts = set(_file_mtimes.keys())

        for ext, path in cog_files.items():
            try:
                mtime = path.stat().st_mtime
            except FileNotFoundError:
                continue

            old_mtime = _file_mtimes.get(ext)

            if old_mtime is None:
                try:
                    log.info(f"New cog detected: {ext} — loading...")
                    await bot.load_extension(ext)
                    _file_mtimes[ext] = mtime
                    refresh_command_metadata()
                except Exception:
                    log.exception(f"Failed to load new cog {ext}")
                continue

            if mtime != old_mtime:
                try:
                    log.info(f"Change detected in {ext} — reloading...")
                    await bot.reload_extension(ext)
                    _file_mtimes[ext] = mtime
                    refresh_command_metadata()
                    bot.tree.clear_commands(guild=None)
                    await bot.tree.sync()
                except Exception:
                    log.exception(f"Failed to reload cog {ext}")

        removed = known_exts - set(cog_files.keys())
        for ext in removed:
            try:
                log.info(f"Cog file removed: {ext} — unloading...")
                await bot.unload_extension(ext)
            except Exception:
                log.exception(f"Failed to unload removed cog {ext}")
            finally:
                _file_mtimes.pop(ext, None)


async def main():
    bot.settings = SettingsManager()
    await bot.settings.init()

    print("Starting bot...\n\nLoading cogs...")
    try:
        async with bot:
            for cog in get_cogs():
                try:
                    await bot.load_extension(cog)
                except Exception:
                    log.exception(f"Failed to load cog {cog}")
            refresh_command_metadata()
            log.info("All cogs loaded. Starting bot.")
            await bot.start(os.getenv("DISCORD_TOKEN"))
    finally:
        await bot.settings.close()


if __name__ == "__main__":
    asyncio.run(main())
