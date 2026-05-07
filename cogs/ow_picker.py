"""
Overwatch Hero Picker Cog
=========================
Commands (all available as both !prefix and /slash):

  ow qp [count]   — Quickplay role lock
                    [count] accepts three formats:
                      • Total players:  !ow qp 6      (auto-distributes roles)
                      • Joined digits:  !ow qp 222    (tank=2, damage=2, support=2)
                      • Dashed:         !ow qp 2-2-2  (same)
                    Order for explicit formats is always Tank – Damage – Support.
                    If in a VC, count is optional — defaults to VC size.

  ow stadium      — Stadium role-queue
                    Must be in a VC. Sends an interactive embed where each
                    member clicks their queued role (Tank / Damage / Support).
                    Once everyone locks in (or 60s expires), each player is
                    assigned a random Stadium hero in their chosen role.

Hero list is fetched live from the OverFast API.
"""

import asyncio
import logging
import random
import re
import time

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger("ow_picker")

OVERFAST_URL        = "https://overfast-api.tekrop.fr/heroes"
OVERFAST_PLAYER_URL = "https://overfast-api.tekrop.fr/players/{player_id}"

_hero_cache: list[dict] | None = None
_hero_cache_ts: float = 0.0
CACHE_TTL = 600  # 10 minutes

ROLE_EMOJI = {
    "open":   "<:Flex:1488714653184819352>",
    "tank":    "<:Tank:1488714804901314560>",
    "damage":  "<:Damage:1488714822290767983>",
    "support": "<:Support:1488714851457962065>",
}

DIVISION_EMOJI = {
    "bronze":      "<:Bronze:1487136293480828998>",
    "silver":      "<:Silver:1487136320689274980>",
    "gold":        "<:Gold:1487136345942917230>",
    "platinum":    "<:Platinum:1487136358525964309>",
    "diamond":     "<:Diamond:1487136378449035264>",
    "master":      "<:Master:1487136402197057648>",
    "grandmaster": "<:Grandmaster:1487136432031273080>",
    "champion":    "<:Top500:1487136474662047814>",
}

ROLE_SELECT_TIMEOUT = 60  # seconds before absent members get a random role

BATTLETAG_RE = re.compile(r"^.{2,12}#\d{4,5}$")


# ---------------------------------------------------------------------------
# API / hero utilities
# ---------------------------------------------------------------------------

async def fetch_heroes(session: aiohttp.ClientSession) -> list[dict]:
    global _hero_cache, _hero_cache_ts

    if _hero_cache is not None and (time.time() - _hero_cache_ts) < CACHE_TTL:
        return _hero_cache

    async with session.get(OVERFAST_URL) as resp:
        resp.raise_for_status()
        data = await resp.json()

    _hero_cache = data
    _hero_cache_ts = time.time()
    return data


def _heroes_by_role(heroes: list[dict], gamemode: str) -> dict[str, list[dict]]:
    eligible = [h for h in heroes if gamemode in h.get("gamemodes", [])]
    by_role: dict[str, list[dict]] = {"tank": [], "support": [], "damage": []}
    for hero in eligible:
        role = hero.get("role")
        if role in by_role:
            by_role[role].append(hero)
    return by_role


# ---------------------------------------------------------------------------
# QP hero picking
# ---------------------------------------------------------------------------

def calculate_slots_qp(count: int) -> dict[str, int]:
    roles = ["tank", "support", "damage"]
    if count <= 3:
        slots: dict[str, int] = {"tank": 0, "support": 0, "damage": 0}
        for _ in range(count):
            slots[random.choice(roles)] += 1
        return slots
    pattern = ["tank", "support", "damage", "tank", "support", "damage"]
    slots = {"tank": 0, "support": 0, "damage": 0}
    for i in range(count):
        slots[pattern[i % len(pattern)]] += 1
    return slots


def parse_count_arg(raw: str) -> tuple[int, dict[str, int] | None]:
    raw = raw.strip()

    if "-" in raw:
        parts = raw.split("-")
        if len(parts) != 3:
            raise ValueError("Dashed format must be `T-D-S`, e.g. `2-2-2`.")
        try:
            t, d, s = (int(p) for p in parts)
        except ValueError:
            raise ValueError("Each role count must be a whole number, e.g. `2-2-2`.")
        return t + d + s, {"tank": t, "damage": d, "support": s}

    if not raw.isdigit():
        raise ValueError("Count must be a number, `T-D-S`, or `TDS`, e.g. `6`, `2-2-2`, `222`.")

    if len(raw) == 3:
        t, d, s = int(raw[0]), int(raw[1]), int(raw[2])
        return t + d + s, {"tank": t, "damage": d, "support": s}

    return int(raw), None


def pick_heroes_qp(heroes: list[dict], slots: dict[str, int]) -> list[dict]:
    by_role = _heroes_by_role(heroes, "quickplay")
    picked: list[dict] = []
    for role in ("tank", "damage", "support"):
        n = slots.get(role, 0)
        pool = by_role[role]
        chosen = random.sample(pool, n) if n <= len(pool) else random.choices(pool, k=n)
        for hero in chosen:
            picked.append({**hero, "_role": role})
    return picked


def build_embed_qp(
    picked: list[dict],
    members: list[discord.Member] | None,
    requester: discord.Member,
) -> discord.Embed:
    embed = discord.Embed(
        title="<:overwatch:1485709866898161814> Overwatch — Quickplay  •  2-2-2",
        color=0xF99E1A,
    )
    by_role: dict[str, list[dict]] = {"tank": [], "support": [], "damage": []}
    for hero in picked:
        by_role[hero["_role"]].append(hero)

    if members:
        ordered = by_role["tank"] + by_role["damage"] + by_role["support"]
        lines = [
            f"{ROLE_EMOJI[hero['_role']]} **{member.display_name}** → **{hero['name']}**"
            for member, hero in zip(members, ordered)
        ]
        embed.description = "\n".join(lines)
        embed.set_footer(
            text=f"Requested by {requester.display_name}  •  {len(members)} players in VC"
        )
    else:
        for role in ("tank", "damage", "support"):
            heroes_in_role = by_role[role]
            if not heroes_in_role:
                continue
            names = "  •  ".join(f"**{h['name']}**" for h in heroes_in_role)
            embed.add_field(
                name=f"{ROLE_EMOJI[role]} {role.capitalize()}",
                value=names,
                inline=False,
            )
        embed.set_footer(text=f"Requested by {requester.display_name}")

    return embed


# ---------------------------------------------------------------------------
# Stadium hero picking
# ---------------------------------------------------------------------------

def pick_heroes_stadium(
    heroes: list[dict],
    member_roles: dict[int, str],
) -> dict[int, dict]:
    by_role = _heroes_by_role(heroes, "stadium")
    for pool in by_role.values():
        random.shuffle(pool)

    used: set[str] = set()
    assignments: dict[int, dict] = {}

    for member_id, role in member_roles.items():
        pool = by_role.get(role, [])
        hero = next((h for h in pool if h["name"] not in used), None)
        if hero is None:
            hero = random.choice(pool) if pool else {"name": "???", "role": role}
        used.add(hero["name"])
        assignments[member_id] = {**hero, "_role": role}

    return assignments


def build_embed_stadium_result(
    assignments: dict[int, dict],
    members: list[discord.Member],
    requester: discord.Member,
    auto_assigned_ids: set[int],
) -> discord.Embed:
    embed = discord.Embed(
        title="<:overwatch:1485709866898161814> Overwatch — Stadium  •  Role Queue",
        color=0xF99E1A,
    )
    lines = []
    for member in members:
        hero = assignments.get(member.id)
        if not hero:
            continue
        note = "  *(auto-assigned)*" if member.id in auto_assigned_ids else ""
        lines.append(
            f"{ROLE_EMOJI[hero['_role']]} **{member.display_name}** → **{hero['name']}**{note}"
        )
    embed.description = "\n".join(lines)
    embed.set_footer(
        text=f"Requested by {requester.display_name}  •  {len(members)} players"
    )
    return embed


def pick_heroes_stadium_slots(heroes: list[dict], slots: dict[str, int]) -> list[dict]:
    by_role = _heroes_by_role(heroes, "stadium")
    picked: list[dict] = []
    for role in ("tank", "damage", "support"):
        n = slots.get(role, 0)
        pool = by_role[role]
        chosen = random.sample(pool, n) if n <= len(pool) else random.choices(pool, k=n)
        for hero in chosen:
            picked.append({**hero, "_role": role})
    return picked


def build_embed_stadium_slots(
    picked: list[dict],
    requester: discord.Member,
) -> discord.Embed:
    embed = discord.Embed(
        title="<:overwatch:1485709866898161814> Overwatch — Stadium  •  Role Queue",
        color=0xF99E1A,
    )
    by_role: dict[str, list[dict]] = {"tank": [], "support": [], "damage": []}
    for hero in picked:
        by_role[hero["_role"]].append(hero)

    for role in ("tank", "damage", "support"):
        heroes_in_role = by_role[role]
        if not heroes_in_role:
            continue
        names = "  •  ".join(f"**{h['name']}**" for h in heroes_in_role)
        embed.add_field(
            name=f"{ROLE_EMOJI[role]} {role.capitalize()}",
            value=names,
            inline=False,
        )
    embed.set_footer(text=f"Requested by {requester.display_name}")
    return embed


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------

def _battletag_to_player_id(battletag: str) -> str:
    """Convert 'Name#1234' → 'Name-1234' for OverFast URLs."""
    return battletag.replace("#", "-")


def _fmt_rank(rank: dict | None) -> str:
    if not rank:
        return "Unranked"
    division = rank.get("division", "").lower()
    tier = rank.get("tier")
    emoji = DIVISION_EMOJI.get(division, "")
    label = division.capitalize()
    if tier and division not in ("grandmaster", "champion"):
        label = f"{label} {tier}"
    return f"{emoji} {label}".strip()


def _fmt_hours(seconds: int | float) -> str:
    hours = int(seconds) // 3600
    return f"{hours:,} hrs"


async def fetch_player(session: aiohttp.ClientSession, player_id: str) -> dict:
    url = OVERFAST_PLAYER_URL.format(player_id=player_id)
    async with session.get(url) as resp:
        if resp.status == 404:
            raise ValueError("player_not_found")
        resp.raise_for_status()
        return await resp.json()


def _career_stat(all_hero_cats: list, category: str, key: str):
    for cat in all_hero_cats:
        if cat.get("category") == category:
            for s in cat.get("stats", []):
                if s.get("key") == key:
                    return s.get("value")
    return None


def _general_career_stats(pc_stats: dict) -> dict:
    """Aggregate overall time_played, games_played, winrate across QP + competitive."""
    total_time = total_games = total_wins = 0
    for mode in ("quickplay", "competitive"):
        all_heroes = ((pc_stats.get(mode) or {}).get("career_stats") or {}).get("all-heroes", [])
        total_time  += _career_stat(all_heroes, "game", "time_played") or 0
        total_games += _career_stat(all_heroes, "game", "games_played") or 0
        total_wins  += _career_stat(all_heroes, "game", "games_won") or 0
    winrate = (total_wins / total_games * 100) if total_games else None
    return {"time_played": total_time, "games_played": total_games, "winrate": winrate}


def _hero_career_stats(pc_stats: dict) -> dict:
    """Aggregate per-hero time_played, games_played, winrate across QP + competitive."""
    combined: dict = {}
    for mode in ("quickplay", "competitive"):
        for hero_key, hero_cats in ((pc_stats.get(mode) or {}).get("career_stats") or {}).items():
            if hero_key == "all-heroes" or not isinstance(hero_cats, list):
                continue
            entry = combined.setdefault(hero_key, {"time_played": 0, "games_played": 0, "games_won": 0})
            entry["time_played"]  += _career_stat(hero_cats, "game", "time_played") or 0
            entry["games_played"] += _career_stat(hero_cats, "game", "games_played") or 0
            entry["games_won"]    += _career_stat(hero_cats, "game", "games_won") or 0
    for h in combined.values():
        gp = h["games_played"]
        h["winrate"] = (h["games_won"] / gp * 100) if gp > 0 else None
    return combined


async def fetch_player_search(session: aiohttp.ClientSession, name: str) -> list[dict]:
    async with session.get("https://overfast-api.tekrop.fr/players", params={"name": name}) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return data.get("results", []) if isinstance(data, dict) else data


def build_embed_stats(
    data: dict,
    battletag: str,
    requester: discord.Member,
) -> discord.Embed:
    summary  = data.get("summary") or {}
    pc_stats = ((data.get("stats") or {}).get("pc") or {})

    username = summary.get("username") or battletag
    avatar   = summary.get("avatar")

    embed = discord.Embed(
        title=f"<:overwatch:1485709866898161814> {username}",
        color=0xF99E1A,
    )
    if avatar:
        embed.set_thumbnail(url=avatar)

    # Endorsement
    endorsement = (summary.get("endorsement") or {}).get("level")
    if endorsement is not None:
        embed.add_field(name="Endorsement", value=f"⭐ Level {endorsement}", inline=True)

    # General stats (career — QP + competitive combined)
    general      = _general_career_stats(pc_stats)
    time_played  = general.get("time_played", 0)
    games_played = general.get("games_played", 0)
    winrate      = general.get("winrate")

    if time_played:
        embed.add_field(name="Time Played (QP + Comp)", value=_fmt_hours(time_played), inline=True)
    if games_played:
        wr_str = f" ({winrate:.1f}% WR)" if winrate is not None else ""
        embed.add_field(
            name="Games Played (QP + Comp)",
            value=f"{games_played:,}{wr_str}",
            inline=True,
        )

    # Competitive ranks (PC) — current season
    comp = (summary.get("competitive") or {}).get("pc") or {}
    rank_lines = []
    for role, label in (("tank", "Tank"), ("damage", "Damage"), ("support", "Support"), ("open", "Open Queue")):
        rank_data = comp.get(role)
        emoji = ROLE_EMOJI.get(role, "🎯")
        rank_lines.append(f"{emoji} **{label}:** {_fmt_rank(rank_data)}")
    if rank_lines:
        embed.add_field(
            name="Competitive Ranks — This Season",
            value="\n".join(rank_lines),
            inline=False,
        )

    # Top 3 heroes by games played (QP + competitive combined)
    heroes_dict = _hero_career_stats(pc_stats)
    heroes_sorted = sorted(
        heroes_dict.items(),
        key=lambda item: item[1].get("games_played", 0),
        reverse=True,
    )[:3]
    if heroes_sorted:
        hero_lines = []
        for hero_name, h in heroes_sorted:
            name   = hero_name.replace("-", " ").title()
            gp     = h.get("games_played", 0)
            wr     = h.get("winrate")
            wr_str = f" • {wr:.0f}% WR" if wr is not None and gp > 0 else ""
            hero_lines.append(f"**{name}** — {gp:,} games{wr_str}")
        embed.add_field(name="Most Played Heroes (QP + Comp)", value="\n".join(hero_lines), inline=False)

    embed.set_footer(text=f"Requested by {requester.display_name}  •  {battletag}  •  Stadium/Arcade not included")
    return embed


# ---------------------------------------------------------------------------
# Player search select View (whois)
# ---------------------------------------------------------------------------

class PlayerSelectView(discord.ui.View):
    def __init__(
        self,
        results: list[dict],
        session: aiohttp.ClientSession,
        requester: discord.Member,
    ):
        super().__init__(timeout=60)
        self.session = session
        self.requester = requester

        options = []
        for r in results[:25]:
            name      = r.get("name") or r.get("player_id", "Unknown")
            player_id = r.get("player_id", "")
            title     = r.get("title") or ""
            options.append(discord.SelectOption(
                label=name[:100],
                value=player_id[:100],
                description=title[:100] if title else None,
            ))

        select = discord.ui.Select(
            placeholder="Choose a player...",
            options=options,
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, interaction: discord.Interaction):
        player_id = interaction.data["values"][0]
        battletag = player_id.replace("-", "#", 1)

        await interaction.response.edit_message(
            content=f"⏳ Fetching stats for **{battletag}**...",
            embed=None,
            view=None,
        )
        try:
            data = await fetch_player(self.session, player_id)
        except ValueError:
            await interaction.edit_original_response(
                content=(
                    f"❌ **{battletag}**'s profile is private or hasn't propagated yet. "
                    "It can take up to **24 hours** after setting the profile to public."
                )
            )
            return
        except Exception as e:
            log.error("whois stats fetch failed for %s: %s", player_id, e)
            await interaction.edit_original_response(
                content="❌ Couldn't reach the Overwatch API. Try again in a moment."
            )
            return

        embed = build_embed_stats(data, battletag, self.requester)
        await interaction.edit_original_response(content=None, embed=embed)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Role-select View (Stadium)
# ---------------------------------------------------------------------------

class RoleSelectView(discord.ui.View):
    def __init__(self, vc_members: list[discord.Member], requester: discord.Member):
        super().__init__(timeout=ROLE_SELECT_TIMEOUT)
        self.vc_members = vc_members
        self.requester = requester
        self.selections: dict[int, str] = {}
        self.auto_assigned_ids: set[int] = set()
        self.message: discord.Message | None = None
        self.done = asyncio.Event()

    def build_waiting_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="<:overwatch:1485709866898161814> Overwatch — Stadium  •  Pick Your Role",
            description="Everyone in VC: click the role you queued for!",
            color=0xF99E1A,
        )
        ready_lines, waiting_lines = [], []
        for m in self.vc_members:
            if m.id in self.selections:
                role = self.selections[m.id]
                ready_lines.append(f"{ROLE_EMOJI[role]} **{m.display_name}** — {role.capitalize()}")
            else:
                waiting_lines.append(f"⏳ {m.display_name}")

        if ready_lines:
            embed.add_field(name="Ready ✅", value="\n".join(ready_lines), inline=False)
        if waiting_lines:
            embed.add_field(name="Waiting for...", value="\n".join(waiting_lines), inline=False)

        embed.set_footer(
            text=f"Requested by {self.requester.display_name}  •  {ROLE_SELECT_TIMEOUT}s to select"
        )
        return embed

    async def _handle_role(self, interaction: discord.Interaction, role: str):
        if interaction.user not in self.vc_members:
            await interaction.response.send_message(
                "❌ You're not in the voice channel!", ephemeral=True
            )
            return

        self.selections[interaction.user.id] = role

        if all(m.id in self.selections for m in self.vc_members):
            self.stop()
            self.done.set()
            await interaction.response.defer()
        else:
            await interaction.response.edit_message(embed=self.build_waiting_embed())

    @discord.ui.button(label="Tank", emoji="🛡️", style=discord.ButtonStyle.primary)
    async def tank_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "tank")

    @discord.ui.button(label="Damage", emoji="💥", style=discord.ButtonStyle.danger)
    async def damage_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "damage")

    @discord.ui.button(label="Support", emoji="💚", style=discord.ButtonStyle.success)
    async def support_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_role(interaction, "support")

    async def on_timeout(self):
        roles = list(ROLE_EMOJI.keys())
        for m in self.vc_members:
            if m.id not in self.selections:
                self.selections[m.id] = random.choice(roles)
                self.auto_assigned_ids.add(m.id)

        self.done.set()

        for item in self.children:
            item.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ---------------------------------------------------------------------------
# Cog
# ---------------------------------------------------------------------------

class OWPicker(commands.Cog, name="Overwatch"):
    """Random hero picker for Overwatch — Quickplay and Stadium modes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None

    def cog_load(self):
        self.session = aiohttp.ClientSession()
        log.info("Cog Loaded.")

    async def cog_unload(self):
        if self.session:
            await self.session.close()
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Group root
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="ow",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Overwatch commands — hero picker, stats & account linking",
        help=(
            "Overwatch hero picker, player stats, and account linking.\n\n"
            "Subcommands:\n"
            "  qp [count]         — Quickplay 2-2-2 role lock\n"
            "  stadium [count]    — Stadium role-queue with interactive role selection\n"
            "  link [member] <tag>— Link an Overwatch battletag to a Discord account\n"
            "  unlink [member]    — Unlink a battletag\n"
            "  stats [member]     — Show linked account's career stats\n"
            "  whois <query>      — Look up any player by battletag or partial name\n"
            "  linked             — List all linked battletags in this server\n\n"
            "If you're in a voice channel, player count is detected automatically "
            "and each person gets personally assigned a hero."
        ),
    )
    async def ow(self, ctx: commands.Context):
        await ctx.send(
            "**Overwatch Commands**\n"
            "`!ow qp [count]`            — Quickplay 2-2-2 (VC recommended)\n"
            "`!ow stadium [count]`       — Stadium role-queue (VC recommended)\n"
            "`!ow link [member] <tag>`   — Link a battletag to your account\n"
            "`!ow unlink [member]`       — Unlink a battletag\n"
            "`!ow stats [member]`        — Show career stats for a linked account\n"
            "`!ow whois <query>`         — Look up any player by name or battletag\n"
            "`!ow linked`               — List all linked battletags in this server\n\n"
            "Run `!help ow <subcommand>` for full details."
        )

    # ------------------------------------------------------------------ #
    #  QP subcommand
    # ------------------------------------------------------------------ #

    @ow.command(
        name="qp",
        brief="Pick heroes for Quickplay (2-2-2)",
        help=(
            "Randomly picks Overwatch heroes following the Quickplay 2-2-2 role format.\n\n"
            "COUNT formats (order: Tank – Damage – Support):\n"
            "  !ow qp 6      — 6 players, roles auto-distributed\n"
            "  !ow qp 222    — exactly 2 tank · 2 damage · 2 support\n"
            "  !ow qp 2-2-2  — same as above, dashed format\n\n"
            "Voice channel behaviour:\n"
            "  • Omit count entirely — uses however many humans are in your VC\n"
            "  • Each person gets personally assigned a hero\n"
            "  • Members are shuffled before assignment so it's fair"
        ),
    )
    @app_commands.describe(count="Players total (6), by role (2-2-2 or 222), or omit if in a VC")
    async def ow_qp(self, ctx: commands.Context, count: str | None = None):
        async with ctx.typing():
            vc_members: list[discord.Member] | None = None
            explicit_slots: dict[str, int] | None = None
            voice_state = ctx.author.voice

            if voice_state and voice_state.channel:
                humans = [m for m in voice_state.channel.members if not m.bot]

                if count is None:
                    vc_members = humans
                    player_count = len(vc_members)
                else:
                    try:
                        player_count, explicit_slots = parse_count_arg(count)
                    except ValueError as e:
                        await ctx.send(f"❌ {e}")
                        return
                    if player_count == len(humans) and explicit_slots is None:
                        vc_members = humans
            else:
                if count is None:
                    await ctx.send(
                        "❌ You're not in a voice channel. "
                        "Specify a count: `!ow qp 6`, `!ow qp 222`, or `!ow qp 2-2-2`"
                    )
                    return
                try:
                    player_count, explicit_slots = parse_count_arg(count)
                except ValueError as e:
                    await ctx.send(f"❌ {e}")
                    return

            if player_count < 1:
                await ctx.send("❌ Need at least 1 player.")
                return
            if player_count > 12:
                await ctx.send("❌ Max 12 players.")
                return

            try:
                heroes = await fetch_heroes(self.session)
            except Exception as e:
                log.error(f"Failed to fetch heroes: {e}")
                await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                return

            slots = explicit_slots if explicit_slots is not None else calculate_slots_qp(player_count)
            picked = pick_heroes_qp(heroes, slots)

            if vc_members:
                members_shuffled = vc_members.copy()
                random.shuffle(members_shuffled)
            else:
                members_shuffled = None

            embed = build_embed_qp(picked, members_shuffled, ctx.author)
            await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  Stadium subcommand
    # ------------------------------------------------------------------ #

    @ow.command(
        name="stadium",
        brief="Pick heroes for Stadium (role-queue)",
        help=(
            "Randomly picks Overwatch heroes for Stadium mode.\n\n"
            "Voice channel (recommended — no count needed):\n"
            "  An interactive embed is posted with Tank / Damage / Support buttons.\n"
            "  Each VC member clicks their queued role, then gets assigned a hero.\n"
            "  Anyone who doesn't pick within 60s gets auto-assigned a random role.\n\n"
            "Without a VC (explicit count required):\n"
            "  !ow stadium 5      — 5 players, Stadium 1-2-2 auto-distribution\n"
            "  !ow stadium 122    — exactly 1 tank · 2 damage · 2 support\n"
            "  !ow stadium 1-2-2  — same, dashed format\n\n"
            "COUNT order is always Tank – Damage – Support."
        ),
    )
    @app_commands.describe(count="Players total (5), by role (1-2-2 or 122), or omit if in a VC")
    async def ow_stadium(self, ctx: commands.Context, count: str | None = None):
        voice_state = ctx.author.voice
        in_vc = voice_state and voice_state.channel

        # ---- VC path: interactive button flow ----------------------------
        if in_vc and count is None:
            humans = [m for m in voice_state.channel.members if not m.bot]
            if not humans:
                await ctx.send("❌ No human players found in your voice channel.")
                return

            view = RoleSelectView(vc_members=humans, requester=ctx.author)
            msg = await ctx.send(embed=view.build_waiting_embed(), view=view)
            view.message = msg

            await view.done.wait()

            for item in view.children:
                item.disabled = True  # type: ignore[attr-defined]

            try:
                heroes = await fetch_heroes(self.session)
            except Exception as e:
                log.error(f"Failed to fetch heroes: {e}")
                await msg.edit(content="❌ Couldn't reach the Overwatch API.", embed=None, view=None)
                return

            assignments = pick_heroes_stadium(heroes, view.selections)
            result_embed = build_embed_stadium_result(
                assignments, humans, ctx.author, view.auto_assigned_ids
            )
            await msg.edit(embed=result_embed, view=None)
            return

        # ---- Slot path: explicit count or auto-distribute ----------------
        if count is None:
            await ctx.send(
                "❌ You're not in a voice channel. "
                "Specify a count: `!ow stadium 5`, `!ow stadium 122`, or `!ow stadium 1-2-2`"
            )
            return
        
        
        try:
            player_count, explicit_slots = parse_count_arg(count)
        except ValueError as e:
            await ctx.send(f"❌ {e}")
            return

        if player_count < 1:
            await ctx.send("❌ Need at least 1 player.")
            return
        if player_count > 12:
            await ctx.send("❌ Max 12 players.")
            return

        async with ctx.typing():
            try:
                heroes = await fetch_heroes(self.session)
            except Exception as e:
                log.error(f"Failed to fetch heroes: {e}")
                await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                return

            if explicit_slots is None:
                pattern = ["tank", "support", "support", "damage", "damage"]
                slots: dict[str, int] = {"tank": 0, "support": 0, "damage": 0}
                for i in range(player_count):
                    slots[pattern[i % len(pattern)]] += 1
            else:
                slots = explicit_slots

            picked = pick_heroes_stadium_slots(heroes, slots)
            embed = build_embed_stadium_slots(picked, ctx.author)
            await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  Link / Unlink / Stats subcommands
    # ------------------------------------------------------------------ #

    @ow.command(
        name="link",
        brief="Link an Overwatch battletag to a Discord account",
        help=(
            "Links an Overwatch battletag to a Discord account.\n\n"
            "Link yourself:\n"
            "  !ow link CoolPlayer#1234\n\n"
            "Link someone else (bot owner only):\n"
            "  !ow link @someone CoolPlayer#1234\n"
            "  The linked person will receive a DM notification.\n\n"
            "Format must be Name#1234 (case-sensitive). "
            "Career profile must be public in Overwatch — takes up to 24 hours to apply."
        ),
    )
    @app_commands.describe(
        member="Member to link (leave blank to link yourself)",
        battletag="Overwatch battletag, e.g. Name#1234",
    )
    async def ow_link(self, ctx: commands.Context, member: discord.Member | None = None, *, battletag: str):
        target = member or ctx.author

        if target != ctx.author and not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ Only the bot owner can link accounts for other members.", ephemeral=True)
            return

        battletag = battletag.strip()
        if not BATTLETAG_RE.match(battletag):
            await ctx.send("❌ Invalid format. Use `Name#1234` — e.g. `CoolPlayer#1234`.", ephemeral=True)
            return

        await self.bot.settings.set_user(target.id, "ow", "battletag", battletag)

        if target != ctx.author:
            dm_note = ""
            try:
                await target.send(
                    f"👋 Your Overwatch account **{battletag}** has been linked to your Discord "
                    f"by **{ctx.author.display_name}** in **{ctx.guild.name}**.\n"
                    "Use `!ow unlink` if this is incorrect."
                )
            except discord.Forbidden:
                dm_note = "\n⚠️ Couldn't DM them — their DMs are closed."
            await ctx.send(f"✅ Linked **{battletag}** to {target.mention}.{dm_note}", ephemeral=True)
        else:
            await ctx.send(
                f"✅ Linked **{battletag}** to your account.\n"
                "Make sure your career profile is set to **public** in Overwatch — "
                "it can take up to **24 hours** after changing the setting.",
                ephemeral=True,
            )

    @ow.command(
        name="unlink",
        brief="Unlink an Overwatch battletag",
        help=(
            "Removes a linked Overwatch battletag.\n\n"
            "Unlink yourself:\n"
            "  !ow unlink\n\n"
            "Unlink someone else (bot owner only):\n"
            "  !ow unlink @someone\n"
            "  The person will receive a DM notification."
        ),
    )
    @app_commands.describe(member="Member to unlink (leave blank to unlink yourself)")
    async def ow_unlink(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author

        if target != ctx.author and not await self.bot.is_owner(ctx.author):
            await ctx.send("❌ Only the bot owner can unlink accounts for other members.", ephemeral=True)
            return

        existing = self.bot.settings.get_user(target.id, "ow", "battletag")
        if not existing:
            msg = "❌ You don't have a battletag linked." if target == ctx.author else f"❌ **{target.display_name}** doesn't have a battletag linked."
            await ctx.send(msg, ephemeral=True)
            return

        await self.bot.settings.delete_user(target.id, "ow", "battletag")

        if target != ctx.author:
            dm_note = ""
            try:
                await target.send(
                    f"👋 Your linked Overwatch account **{existing}** has been unlinked from your Discord "
                    f"by **{ctx.author.display_name}** in **{ctx.guild.name}**."
                )
            except discord.Forbidden:
                dm_note = "\n⚠️ Couldn't DM them — their DMs are closed."
            await ctx.send(f"✅ Unlinked **{existing}** from {target.mention}.{dm_note}", ephemeral=True)
        else:
            await ctx.send(f"✅ Your battletag (**{existing}**) has been unlinked.", ephemeral=True)

    @ow.command(
        name="linked",
        brief="List all linked Overwatch battletags in this server",
        help="Shows every server member who has linked an Overwatch battletag.",
    )
    async def ow_linked(self, ctx: commands.Context):
        if not ctx.guild:
            await ctx.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        rows = []
        for user_id, namespaces in self.bot.settings._user_cache.items():
            battletag = namespaces.get("ow", {}).get("battletag")
            if not battletag:
                continue
            member = ctx.guild.get_member(user_id)
            if member is None or member.bot:
                continue
            rows.append((member.display_name, battletag))

        if not rows:
            await ctx.send("No members in this server have linked a battletag yet.")
            return

        rows.sort(key=lambda r: r[0].lower())
        lines = "\n".join(f"**{name}** — `{tag}`" for name, tag in rows)
        await ctx.send(f"🎮 **Linked Overwatch Accounts** ({len(rows)})\n\n{lines}")

    @ow.command(
        name="stats",
        brief="Show Overwatch stats for a player",
        help=(
            "Displays competitive ranks, time played, win rate, and top heroes.\n\n"
            "Usage:\n"
            "  !ow stats           — your own linked account\n"
            "  !ow stats @someone  — another server member's linked account\n\n"
            "The target player must have their career profile set to public in Overwatch. "
            "It can take up to 24 hours after changing the setting for it to apply."
        ),
    )
    @app_commands.describe(user="Server member to look up (uses their linked battletag)")
    async def ow_stats(self, ctx: commands.Context, user: discord.Member | None = None):
        member = user or ctx.author
        battletag = self.bot.settings.get_user(member.id, "ow", "battletag")

        if battletag is None:
            if member == ctx.author:
                await ctx.send("❌ You haven't linked a battletag. Use `!ow link Name#1234`.")
            else:
                await ctx.send(f"❌ **{member.display_name}** hasn't linked their battletag.")
            return

        async with ctx.typing():
            player_id = _battletag_to_player_id(battletag)
            try:
                data = await fetch_player(self.session, player_id)
            except ValueError:
                await ctx.send(
                    f"❌ Couldn't find **{battletag}**. "
                    "Check the battletag is correct and the career profile is set to **public**. "
                    "It can take up to **24 hours** after changing the setting."
                )
                return
            except Exception as e:
                log.error("Failed to fetch OW stats for %s: %s", battletag, e)
                await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                return

            embed = build_embed_stats(data, battletag, ctx.author)
            await ctx.send(embed=embed)

    # ------------------------------------------------------------------ #
    #  Whois subcommand
    # ------------------------------------------------------------------ #

    @ow.command(
        name="whois",
        brief="Look up any Overwatch player by name or battletag",
        help=(
            "Search for any Overwatch player — no Discord link needed.\n\n"
            "Full battletag (skips search, shows stats directly):\n"
            "  !ow whois CoolPlayer#1234\n\n"
            "Partial name (returns a list to pick from):\n"
            "  !ow whois CoolPlayer\n\n"
            "Private profiles will show an error after selection."
        ),
    )
    @app_commands.describe(query="Full battletag (Name#1234) or partial name to search")
    async def ow_whois(self, ctx: commands.Context, *, query: str):
        query = query.strip()

        async with ctx.typing():
            # Full battletag — skip search, show stats directly
            if BATTLETAG_RE.match(query):
                player_id = _battletag_to_player_id(query)
                try:
                    data = await fetch_player(self.session, player_id)
                except ValueError:
                    await ctx.send(
                        f"❌ Couldn't find **{query}**. "
                        "Check the battletag is correct and the profile is set to **public**. "
                        "It can take up to **24 hours** after changing the setting."
                    )
                    return
                except Exception as e:
                    log.error("whois direct fetch failed for %s: %s", query, e)
                    await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                    return

                embed = build_embed_stats(data, query, ctx.author)
                await ctx.send(embed=embed)
                return

            # Partial name — search and show select menu
            try:
                results = await fetch_player_search(self.session, query)
            except Exception as e:
                log.error("whois search failed for %s: %s", query, e)
                await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                return

            if not results:
                await ctx.send(f"❌ No players found matching **{query}**.")
                return

            if len(results) == 1:
                player_id = results[0].get("player_id", "")
                battletag = player_id.replace("-", "#", 1)
                try:
                    data = await fetch_player(self.session, player_id)
                except ValueError:
                    await ctx.send(
                        f"❌ **{battletag}**'s profile is private or hasn't propagated yet. "
                        "It can take up to **24 hours** after setting the profile to public."
                    )
                    return
                except Exception as e:
                    log.error("whois single-result fetch failed for %s: %s", player_id, e)
                    await ctx.send("❌ Couldn't reach the Overwatch API. Try again in a moment.")
                    return

                embed = build_embed_stats(data, battletag, ctx.author)
                await ctx.send(embed=embed)
                return

            view = PlayerSelectView(results, self.session, ctx.author)
            await ctx.send(
                f"Found **{len(results)}** players matching **{query}** — pick one:",
                view=view,
            )


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

async def setup(bot: commands.Bot):
    await bot.add_cog(OWPicker(bot))