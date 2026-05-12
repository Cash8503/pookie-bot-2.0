import json
import logging

import discord
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

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

    @helped_hybrid_group("db",
        name="db",
        invoke_without_command=True,
        case_insensitive=True,
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

    @helped_command(db, "db tables", name="tables")
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

    @helped_command(db, "db get",
        name="get",
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

    @helped_command(db, "db find",
        name="find",
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

    @helped_command(db, "db set",
        name="set",
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

    @helped_command(db, "db del",
        name="del",
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

    @helped_command(db, "db clear",
        name="clear",
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
