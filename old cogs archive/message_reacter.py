# cogs/role_reactor.py
import asyncio
import random
from typing import Dict, Iterable, List, Optional, Sequence, Union

import discord
from discord.ext import commands

# ===================== CONFIG =====================
# PRIORITY list of role IDs to check first. First match wins (unless MERGE_POOLS_FOR_MULTIPLE_MATCHES=True).
ROLE_PRIORITY: Sequence[int] = [
    # 111111111111111111,   # e.g., "VIP"
    # 222222222222222222,   # e.g., "Moderator"
]

# Optional: role names mapping (used only if ROLE_PRIORITY and ROLE_TO_EMOJIS_IDS don't cover it)
# Names are case-insensitive; IDs are preferred.
ROLE_NAME_PRIORITY: Sequence[str] = [
    # "VIP",
    # "Moderator",
]

# Map of role ID -> emoji pool (Unicode, custom "<:name:id>" strings, or raw emoji IDs)
ROLE_TO_EMOJIS_IDS: Dict[int, Sequence[Union[str, int]]] = {
    # 222222222222222222: ["🔥", "💯", "😎"],
}

USERID_TO_EMOJIS: Dict[int, Sequence[Union[str, int]]] = {
    433001418841128973: ["🤓", "😩"],
}


# Optional: role-name -> emoji pool (used if ID map not found). Prefer IDs!
ROLE_NAME_TO_EMOJIS: Dict[str, Sequence[Union[str, int]]] = {
    "needs love": ["💗", "❤️", "🧡", "💛", "💚", "💙", "💜", "🤎", "🤍"],
    "stfu": ["🖕", "🥀", "👎"],
    "furry": ["🐾", "🐺", "🦊"],
    "fag squad": ["🌈", "🏳️‍🌈", "🦄"],
}

CONFETTI_CHANCE: float = 0.001
CONFETTI_POOL: Sequence[Union[str, int]] = ["🎉"]

# Probability to react (global). Set < 1.0 to react only sometimes.
REACTION_PROBABILITY: float = .15

# Per-user cooldown to avoid spam (seconds). 0 disables.
PER_USER_COOLDOWN_SECONDS: float = 120

# If a member matches multiple roles, merge all found emoji pools instead of using the first match.
MERGE_POOLS_FOR_MULTIPLE_MATCHES: bool = False
# ==================================================


def _normalize_custom_emoji_str(e: str) -> Optional[int]:
    """Extract an emoji ID if string looks like '<:name:ID>' or '<a:name:ID>'."""
    if e.startswith("<") and e.endswith(">") and ":" in e:
        try:
            parts = e.strip("<>").split(":")
            return int(parts[-1])
        except (ValueError, IndexError):
            return None
    return None


class RoleReactor(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._cooldown: dict[int, float] = {}  # user_id -> last reaction timestamp

    # ---------- Helpers ----------

    async def _resolve_member(self, message: discord.Message) -> Optional[discord.Member]:
        if not message.guild:
            return None
        if isinstance(message.author, discord.Member):
            return message.author
        try:
            return await message.guild.fetch_member(message.author.id)
        except discord.HTTPException:
            return None

    def _member_role_ids_and_names(self, member: discord.Member) -> tuple[set[int], set[str]]:
        ids = {r.id for r in (member.roles or []) if isinstance(r, discord.Role)}
        names = {((r.name or "").lower()) for r in (member.roles or []) if isinstance(r, discord.Role)}
        return ids, names

    def _pick_pool_for_member(
        self,
        member: discord.Member,
    ) -> Optional[Sequence[Union[str, int]]]:
        """Return an emoji pool for the member based on per-user/role mapping and priority."""
        role_ids, role_names = self._member_role_ids_and_names(member)

        matched_pools: List[Sequence[Union[str, int]]] = []

        # --- NEW: Per-user override/merge ---
        user_pool = USERID_TO_EMOJIS.get(member.id)
        if user_pool:
            if not MERGE_POOLS_FOR_MULTIPLE_MATCHES:
                return user_pool
            matched_pools.append(user_pool)

        # First: check explicit ID mapping using ROLE_PRIORITY order
        if ROLE_PRIORITY:
            for rid in ROLE_PRIORITY:
                if rid in role_ids and rid in ROLE_TO_EMOJIS_IDS:
                    if not MERGE_POOLS_FOR_MULTIPLE_MATCHES:
                        return ROLE_TO_EMOJIS_IDS[rid]
                    matched_pools.append(ROLE_TO_EMOJIS_IDS[rid])
        
        # If still empty, check ID map without priority (any other mapped roles)
        for rid, pool in ROLE_TO_EMOJIS_IDS.items():
            if rid in role_ids and pool and pool not in matched_pools:
                if not MERGE_POOLS_FOR_MULTIPLE_MATCHES:
                    return pool
                matched_pools.append(pool)

        # Fall back to name-based mapping (priority first)
        lower_name_priority = [n.lower() for n in ROLE_NAME_PRIORITY]
        for name in lower_name_priority:
            if name in role_names and name in {k.lower() for k in ROLE_NAME_TO_EMOJIS.keys()}:
                # find original cased key
                for k, pool in ROLE_NAME_TO_EMOJIS.items():
                    if k.lower() == name:
                        if not MERGE_POOLS_FOR_MULTIPLE_MATCHES:
                            return pool
                        matched_pools.append(pool)

        # Any other name mappings
        for k, pool in ROLE_NAME_TO_EMOJIS.items():
            if k.lower() in role_names and pool and pool not in matched_pools:
                if not MERGE_POOLS_FOR_MULTIPLE_MATCHES:
                    return pool
                matched_pools.append(pool)

        if matched_pools:
            # Merge all pools into one flat list
            merged: List[Union[str, int]] = []
            for p in matched_pools:
                merged.extend(p)
            return merged

        return None


    def _passes_cooldown(self, user_id: int) -> bool:
        if PER_USER_COOLDOWN_SECONDS <= 0:
            return True
        now = asyncio.get_running_loop().time()
        last = self._cooldown.get(user_id, 0.0)
        if (now - last) >= PER_USER_COOLDOWN_SECONDS:
            self._cooldown[user_id] = now
            return True
        return False

    def _resolve_emoji(
        self,
        guild: Optional[discord.Guild],
        item: Union[str, int]
    ) -> Optional[Union[str, discord.Emoji, discord.PartialEmoji]]:
        """Turn pool items into valid emoji objects/strings where possible."""
        if isinstance(item, int):
            return (guild.get_emoji(item) if guild else None) or self.bot.get_emoji(item)

        if isinstance(item, str):
            # Try to convert "<:name:id>" to Emoji
            maybe_id = _normalize_custom_emoji_str(item)
            if maybe_id:
                return ((guild.get_emoji(maybe_id) if guild else None) or self.bot.get_emoji(maybe_id)) or item
            # Otherwise assume it's a Unicode emoji string
            return item

        return None

    def _pick_emoji_from_pool(
        self,
        guild: Optional[discord.Guild],
        pool: Sequence[Union[str, int]]
    ) -> Optional[Union[str, discord.Emoji, discord.PartialEmoji]]:
        if not pool:
            return None
        for _ in range(len(pool)):  # try up to pool size to find a resolvable emoji
            choice = random.choice(pool)
            em = self._resolve_emoji(guild, choice)
            if em:
                return em
        return None

    # ---------- Listener ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        # Ignore bots and DMs
        if message.author.bot or not message.guild:
            return
        
        sm = self.bot.get_cog("SeriousMode")
        if sm and sm.is_active(message.guild):
            return  # do nothing while serious mode is on

        member = await self._resolve_member(message)
        if not member:
            return

        # Respect per-user cooldown for *any* reaction
        if not self._passes_cooldown(member.id):
            return

        # --- Flat confetti chance (independent of role mapping / REACTION_PROBABILITY) ---
        if CONFETTI_CHANCE > 0 and random.random() < CONFETTI_CHANCE:
            confetti = self._pick_emoji_from_pool(message.guild, CONFETTI_POOL)
            if confetti:
                try:
                    await message.add_reaction(confetti)
                    await message.channel.send("YOU WIN")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            return  # done; don't also do role-based reaction this time

        # Probability gate for the *normal* role-based reaction
        if REACTION_PROBABILITY < 1.0 and random.random() > REACTION_PROBABILITY:
            return

        pool = self._pick_pool_for_member(member)
        if not pool:
            return

        emoji = self._pick_emoji_from_pool(message.guild, pool)
        if not emoji:
            return

        try:
            await message.add_reaction(emoji)
        except discord.Forbidden:
            # Missing Add Reactions / Read Message History / Use External Emojis
            pass
        except discord.HTTPException:
            # Invalid emoji or other API issue
            pass


async def setup(bot: commands.Bot):
    print("Loading MessagerReacter")
    await bot.add_cog(RoleReactor(bot))
