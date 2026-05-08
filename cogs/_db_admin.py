import json
import logging

import discord
from discord.ext import commands

log = logging.getLogger(__name__)

_GUILD = "guild"
_USER  = "user"


def _is_guild(table: str) -> bool | None:
    t = table.lower()
    if t in (_GUILD, "guild_settings"):
        return True
    if t in (_USER, "user_settings"):
        return False
    return None


def _parse_id(raw: str) -> int:
    """Parse a user/guild ID from a raw string, stripping Discord mention syntax if present."""
    # <@123>, <@!123>, <#123>, <@&123>
    cleaned = raw.strip().lstrip("<@!&#>").rstrip(">")
    return int(cleaned)


def _parse_value(raw: str):
    """JSON-decode the input if valid; otherwise treat as a plain string."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return raw


def _paginate(text: str, limit: int = 1990):
    for i in range(0, len(text), limit):
        yield text[i : i + limit]


class DbAdmin(commands.Cog, name="DbAdmin"):
    """Owner-only commands for reading and writing the settings database."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    async def cog_unload(self):
        log.info("Cog Unloaded.")

    async def cog_check(self, ctx: commands.Context) -> bool:
        return await self.bot.is_owner(ctx.author)

    # ------------------------------------------------------------------ #
    #  Group root
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="db",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Database admin commands (owner only)",
        help=(
            "Owner-only commands for inspecting and editing the bot's settings.\n\n"
            "Tables:\n"
            "  guild  →  guild_settings (guild_id, namespace, key, value)\n"
            "  user   →  user_settings  (user_id,  namespace, key, value)\n\n"
            "Values are stored as JSON. When setting a value:\n"
            "  • Plain text like  hello       → stored as string\n"
            "  • JSON like        123 / true  → stored as number / bool\n\n"
            "Subcommands:\n"
            "  tables                               — row counts per table\n"
            "  get <guild|user> <id> [ns] [key]     — read rows\n"
            "  set <guild|user> <id> <ns> <key> <v> — upsert a value\n"
            "  del <guild|user> <id> <ns> <key>     — delete one row\n"
            "  clear <guild|user> <id> [ns]         — delete all rows for ID\n"
        ),
    )
    async def db(self, ctx: commands.Context):
        await ctx.send(
            "**DB Admin** (owner only)\n"
            "`!db tables`                                    — row counts\n"
            "`!db get <guild|user> <id> [namespace] [key]`   — read\n"
            "`!db set <guild|user> <id> <ns> <key> <value>`  — upsert\n"
            "`!db del <guild|user> <id> <ns> <key>`          — delete row\n"
            "`!db clear <guild|user> <id> [namespace]`       — delete all for ID\n"
        )

    # ------------------------------------------------------------------ #
    #  Subcommands
    # ------------------------------------------------------------------ #

    @db.command(name="tables", brief="Show row counts for all tables")
    async def db_tables(self, ctx: commands.Context):
        s = self.bot.settings
        guild_rows = sum(
            len(keys)
            for namespaces in s._cache.values()
            for keys in namespaces.values()
        )
        user_rows = sum(
            len(keys)
            for namespaces in s._user_cache.values()
            for keys in namespaces.values()
        )
        await ctx.send(
            f"`guild_settings` (guild) — **{guild_rows}** row(s)\n"
            f"`user_settings` (user) — **{user_rows}** row(s)"
        )

    @db.command(
        name="get",
        brief="Read rows from a table",
        help=(
            "Reads rows from guild_settings or user_settings.\n\n"
            "Examples:\n"
            "  !db get user 123456789               — all rows for user\n"
            "  !db get user 123456789 ow            — all rows in namespace 'ow'\n"
            "  !db get user 123456789 ow battletag  — single value\n"
            "  !db get guild 987654321 link_cleaner — all rows in namespace"
        ),
    )
    async def db_get(
        self,
        ctx: commands.Context,
        table: str,
        entity_id: str,
        namespace: str = None,
        key: str = None,
    ):
        guild = _is_guild(table)
        if guild is None:
            await ctx.send(f"❌ Unknown table `{table}`. Use `guild` or `user`.")
            return

        try:
            eid = _parse_id(entity_id)
        except ValueError:
            await ctx.send(f"❌ `{entity_id}` is not a valid ID or mention.")
            return

        s = self.bot.settings
        cache = s._cache if guild else s._user_cache
        id_col = "guild_id" if guild else "user_id"
        table_name = "guild_settings" if guild else "user_settings"

        entity_data = cache.get(eid, {})

        rows = []
        for ns, keys in sorted(entity_data.items()):
            if namespace and ns != namespace:
                continue
            for k, v in sorted(keys.items()):
                if key and k != key:
                    continue
                rows.append(f"`{ns}` / `{k}` = `{json.dumps(v)}`")

        if not rows:
            await ctx.send(f"No rows found in `{table_name}` for {id_col} `{eid}`.")
            return

        header = f"**{table_name}** — {id_col} `{eid}`\n"
        for chunk in _paginate(header + "\n".join(rows)):
            await ctx.send(chunk)

    @db.command(
        name="find",
        brief="Search all tables for an ID",
        help=(
            "Searches both guild_settings and user_settings for any rows matching the given ID.\n\n"
            "Examples:\n"
            "  !db find 123456789\n"
            "  !db find @someone"
        ),
    )
    async def db_find(self, ctx: commands.Context, entity_id: str):
        try:
            eid = _parse_id(entity_id)
        except ValueError:
            await ctx.send(f"❌ `{entity_id}` is not a valid ID or mention.")
            return

        s = self.bot.settings
        any_results = False
        for table_name, id_col, cache in (
            ("guild_settings", "guild_id", s._cache),
            ("user_settings",  "user_id",  s._user_cache),
        ):
            entity_data = cache.get(eid, {})
            rows = [
                f"`{ns}` / `{k}` = `{json.dumps(v)}`"
                for ns, keys in sorted(entity_data.items())
                for k, v in sorted(keys.items())
            ]
            if rows:
                any_results = True
                header = f"**{table_name}** — {id_col} `{eid}`\n"
                for chunk in _paginate(header + "\n".join(rows)):
                    await ctx.send(chunk)

        if not any_results:
            await ctx.send(f"No rows found for ID `{eid}` in any table.")

    @db.command(
        name="set",
        brief="Upsert a value in a table",
        help=(
            "Inserts or updates a single row. The value is parsed as JSON if valid,\n"
            "otherwise stored as a plain string.\n\n"
            "Examples:\n"
            "  !db set user 123456789 ow battletag CoolPlayer#1234\n"
            "  !db set guild 987654321 link_cleaner enabled true\n"
            "  !db set user 123456789 xp level 42"
        ),
    )
    async def db_set(
        self,
        ctx: commands.Context,
        table: str,
        entity_id: str,
        namespace: str,
        key: str,
        *,
        value: str,
    ):
        guild = _is_guild(table)
        if guild is None:
            await ctx.send(f"❌ Unknown table `{table}`. Use `guild` or `user`.")
            return

        try:
            eid = _parse_id(entity_id)
        except ValueError:
            await ctx.send(f"❌ `{entity_id}` is not a valid ID or mention.")
            return

        parsed = _parse_value(value)
        s = self.bot.settings

        if guild:
            await s.set(eid, namespace, key, parsed)
        else:
            await s.set_user(eid, namespace, key, parsed)

        table_name = "guild_settings" if guild else "user_settings"
        await ctx.send(
            f"✅ `{table_name}[{eid}].{namespace}.{key}` → `{json.dumps(parsed)}`"
        )

    @db.command(
        name="del",
        brief="Delete a single row",
        help=(
            "Deletes one specific row identified by (id, namespace, key).\n\n"
            "Examples:\n"
            "  !db del user 123456789 ow battletag\n"
            "  !db del guild 987654321 link_cleaner enabled"
        ),
    )
    async def db_del(
        self,
        ctx: commands.Context,
        table: str,
        entity_id: str,
        namespace: str,
        key: str,
    ):
        guild = _is_guild(table)
        if guild is None:
            await ctx.send(f"❌ Unknown table `{table}`. Use `guild` or `user`.")
            return

        try:
            eid = _parse_id(entity_id)
        except ValueError:
            await ctx.send(f"❌ `{entity_id}` is not a valid ID or mention.")
            return

        s = self.bot.settings
        if guild:
            await s.delete(eid, namespace, key)
        else:
            await s.delete_user(eid, namespace, key)

        table_name = "guild_settings" if guild else "user_settings"
        await ctx.send(f"✅ Deleted `{table_name}[{eid}].{namespace}.{key}`")

    @db.command(
        name="clear",
        brief="Delete all rows for an ID (optionally filtered by namespace)",
        help=(
            "Deletes every row for a given ID. Optionally scope to one namespace.\n\n"
            "Examples:\n"
            "  !db clear user 123456789           — remove all data for user\n"
            "  !db clear user 123456789 ow        — remove only the 'ow' namespace\n"
            "  !db clear guild 987654321           — wipe all settings for a guild"
        ),
    )
    async def db_clear(
        self,
        ctx: commands.Context,
        table: str,
        entity_id: str,
        namespace: str = None,
    ):
        guild = _is_guild(table)
        if guild is None:
            await ctx.send(f"❌ Unknown table `{table}`. Use `guild` or `user`.")
            return

        try:
            eid = _parse_id(entity_id)
        except ValueError:
            await ctx.send(f"❌ `{entity_id}` is not a valid ID or mention.")
            return

        s = self.bot.settings
        if guild:
            await s.clear(eid, namespace)
        else:
            await s.clear_user(eid, namespace)

        table_name = "guild_settings" if guild else "user_settings"
        if namespace:
            await ctx.send(f"✅ Cleared namespace `{namespace}` in `{table_name}` for ID `{eid}`")
        else:
            await ctx.send(f"✅ Cleared all rows in `{table_name}` for ID `{eid}`")


async def setup(bot: commands.Bot):
    await bot.add_cog(DbAdmin(bot))
