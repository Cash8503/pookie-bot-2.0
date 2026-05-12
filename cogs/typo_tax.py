import asyncio
import logging
import random
import re
from dataclasses import dataclass

import discord
from discord import app_commands
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group
from cogs._guild_cogs import is_cog_disabled

log = logging.getLogger(__name__)

try:
    from spellchecker import SpellChecker
except ImportError:  # Keep the cog loadable until requirements are installed.
    SpellChecker = None


NAMESPACE = "typo_tax"
MAX_TAX_PER_MESSAGE = 3
MIN_WORD_LEN = 4

_STRIP_RE = re.compile(
    r"https?://\S+"
    r"|<[^>]+>"
    r"|```[\s\S]*?```"
    r"|`[^`]+`",
    re.MULTILINE,
)
_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z']{2,}\b")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04FF]")
_SPANISH_CHAR_RE = re.compile(r"[\u00E1\u00E9\u00ED\u00F3\u00FA\u00FC\u00F1\u00BF\u00A1]", re.IGNORECASE)

_SPANISH_MARKERS = {
    "el", "la", "los", "las", "un", "una", "unos", "unas", "de", "del",
    "que", "y", "o", "pero", "porque", "para", "por", "con", "sin",
    "como", "esta", "este", "esto", "soy", "eres", "es", "son", "muy",
    "mas", "menos", "hola", "gracias", "tambien", "cuando", "donde",
}

_IGNORED_WORDS = {
    "lol", "lmao", "rofl", "omg", "idk", "ikr", "imo", "imho", "tbh",
    "btw", "brb", "afk", "gg", "wp", "ez", "rn", "dm", "dms", "discord",
    "pookie", "bot", "bots", "emoji", "emojis", "meme", "memes", "haha",
    "hehe", "yall", "ya", "nah", "gonna", "wanna", "gotta", "kinda",
    "sorta", "lemme", "pls", "plz", "thx", "tysm", "ok", "okay", "oki", "queefing", "queef"
}

_CONTRACTIONS = {
    "aint", "aren't", "cant", "can't", "couldnt", "couldn't", "didnt",
    "didn't", "doesnt", "doesn't", "dont", "don't", "hadnt", "hadn't",
    "hasnt", "hasn't", "havent", "haven't", "im", "i'm", "isnt", "isn't",
    "itll", "it'll", "ive", "i've", "shouldnt", "shouldn't", "thats",
    "that's", "theres", "there's", "theyre", "they're", "wasnt", "wasn't",
    "werent", "weren't", "wont", "won't", "wouldnt", "wouldn't", "youre",
    "you're", "youve", "you've",
}

_COMMON_TYPOS = {
    "acheive": "achieve",
    "accomodate": "accommodate",
    "adress": "address",
    "alot": "a lot",
    "becuase": "because",
    "beleive": "believe",
    "definately": "definitely",
    "definetly": "definitely",
    "freind": "friend",
    "goverment": "government",
    "grammer": "grammar",
    "neccessary": "necessary",
    "occured": "occurred",
    "recieve": "receive",
    "seperate": "separate",
    "teh": "the",
    "thier": "their",
    "wierd": "weird",
    "helo": "hello",
    "ment": "meant",
}


@dataclass(frozen=True)
class TypoHit:
    word: str
    suggestion: str


@dataclass(frozen=True)
class RepaymentChallenge:
    category: str
    prompt: str
    answer: str


def _clean(content: str) -> str:
    return _STRIP_RE.sub(" ", content)


def _edit_distance(a: str, b: str, limit: int = 2) -> int:
    """Bounded Levenshtein distance; exits once the row minimum exceeds limit."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        row_min = current[0]
        for j, cb in enumerate(b, start=1):
            insert = current[j - 1] + 1
            delete = previous[j] + 1
            replace = previous[j - 1] + (ca != cb)
            value = min(insert, delete, replace)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


def _looks_like_other_language(text: str) -> bool:
    """Skip languages we know this first pass should not tax, especially Spanish/Russian."""
    if _CYRILLIC_RE.search(text):
        return True
    if _SPANISH_CHAR_RE.search(text):
        return True

    words = [w.strip("'").lower() for w in _WORD_RE.findall(text)]
    marker_count = sum(1 for word in words if word in _SPANISH_MARKERS)
    if marker_count >= 2:
        return True
    if 0 < len(words) <= 5 and marker_count >= 1:
        return True
    if len(words) < 4:
        return False
    return marker_count >= 2 and marker_count / len(words) >= 0.25


class TypoTaxAlertView(discord.ui.View):
    def __init__(self, cog: "TypoTaxCog", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    @discord.ui.button(label="Repay", style=discord.ButtonStyle.success)
    async def repay(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This typo debt is not yours to repay.", ephemeral=True)
            return

        await self.cog.start_repayment_from_interaction(interaction)


class RepaymentResultView(discord.ui.View):
    def __init__(self, cog: "TypoTaxCog", user_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This repayment menu belongs to someone else.", ephemeral=True)
        return False

    @discord.ui.button(label="Go Again", style=discord.ButtonStyle.primary)
    async def go_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_repayment_from_interaction(interaction)

    @discord.ui.button(label="Change Category", style=discord.ButtonStyle.secondary)
    async def change_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Repayment Categories",
            description="Math is the only repayment category available right now. More categories can plug into this menu later.",
            color=0x5865F2,
        )
        await interaction.response.send_message(embed=embed, view=RepaymentCategoryView(self.user_id), ephemeral=True)


class RepaymentCategoryView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.select(
        placeholder="Math selected",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(
                label="Math",
                value="math",
                description="Simple arithmetic questions",
                default=True,
            )
        ],
    )
    async def select_category(self, interaction: discord.Interaction, select: discord.ui.Select):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This category picker belongs to someone else.", ephemeral=True)
            return
        await interaction.response.send_message("Math is already selected.", ephemeral=True)


class TypoTaxCog(commands.Cog, name="TypoTax"):
    """Tracks typo debt and lets users repay it with small challenges."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._spellchecker = self._build_spellchecker()

    def cog_load(self):
        if self._spellchecker is None:
            log.warning("Cog Loaded with common-typo detection only; install pyspellchecker for broader detection.")
        else:
            log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    @staticmethod
    def _build_spellchecker():
        if SpellChecker is None:
            return None
        checker = SpellChecker(language="en", distance=1)
        checker.word_frequency.load_words(_IGNORED_WORDS | _CONTRACTIONS)
        return checker

    def _balance(self, user_id: int) -> int:
        return int(self.bot.settings.get_user(user_id, NAMESPACE, "balance", 0) or 0)

    async def _set_balance(self, user_id: int, balance: int) -> None:
        await self.bot.settings.set_user(user_id, NAMESPACE, "balance", max(0, int(balance)))

    def _notifications_enabled(self, user_id: int) -> bool:
        return bool(self.bot.settings.get_user(user_id, NAMESPACE, "notifications", False))

    async def _set_notifications(self, user_id: int, enabled: bool) -> None:
        await self.bot.settings.set_user(user_id, NAMESPACE, "notifications", enabled)

    async def _add_tax(self, user_id: int, amount: int) -> int:
        balance = self._balance(user_id) + amount
        await self._set_balance(user_id, balance)
        total = int(self.bot.settings.get_user(user_id, NAMESPACE, "total_typos", 0) or 0)
        await self.bot.settings.set_user(user_id, NAMESPACE, "total_typos", total + amount)
        return balance

    async def _repay_one(self, user_id: int) -> int:
        balance = max(0, self._balance(user_id) - 1)
        await self._set_balance(user_id, balance)
        total = int(self.bot.settings.get_user(user_id, NAMESPACE, "total_repaid", 0) or 0)
        await self.bot.settings.set_user(user_id, NAMESPACE, "total_repaid", total + 1)
        return balance

    def _detect_typos(self, content: str) -> list[TypoHit]:
        cleaned = _clean(content)
        if _looks_like_other_language(cleaned):
            return []

        hits: list[TypoHit] = []
        seen: set[str] = set()

        for raw in _WORD_RE.findall(cleaned):
            word = raw.strip("'")
            lower = word.lower()
            if lower in seen:
                continue
            if len(lower) < MIN_WORD_LEN:
                continue
            if lower in _IGNORED_WORDS or lower in _CONTRACTIONS:
                continue
            if word[:1].isupper():
                continue
            if len(set(lower)) <= 2 and len(lower) > 5:
                continue

            suggestion = _COMMON_TYPOS.get(lower)
            if not suggestion and self._spellchecker is not None:
                if lower not in self._spellchecker.unknown([lower]):
                    continue
                corrected = self._spellchecker.correction(lower)
                if corrected and corrected != lower:
                    max_distance = 1 if len(lower) <= 5 else 2
                    if _edit_distance(lower, corrected, limit=max_distance) <= max_distance:
                        suggestion = corrected

            if suggestion:
                hits.append(TypoHit(word=word, suggestion=suggestion))
                seen.add(lower)
                if len(hits) >= MAX_TAX_PER_MESSAGE:
                    break

        return hits

    def _build_tax_embed(self, hits: list[TypoHit], balance: int) -> discord.Embed:
        amount = len(hits)
        noun = "typo" if amount == 1 else "typos"
        examples = ", ".join(f"`{hit.word}` -> `{hit.suggestion}`" for hit in hits[:2])
        if len(hits) > 2:
            examples += f", +{len(hits) - 2} more"
        embed = discord.Embed(
            title="Typo Tax",
            description=f"+{amount} {noun}. Current balance: **{balance}**.",
            color=0xFEE75C,
        )
        embed.add_field(name="Caught", value=examples, inline=False)
        embed.set_footer(text="Use the Repay button or !typotax repay to work off one point.")
        return embed

    def _make_math_challenge(self) -> RepaymentChallenge:
        left = random.randint(2, 12)
        right = random.randint(2, 12)
        op = random.choice(("+", "-", "*"))
        if op == "-":
            left, right = max(left, right), min(left, right)
            answer = left - right
        elif op == "*":
            answer = left * right
        else:
            answer = left + right
        return RepaymentChallenge(category="Math", prompt=f"What is `{left} {op} {right}`?", answer=str(answer))

    def _build_repayment_embed(self, user: discord.Member | discord.User, challenge: RepaymentChallenge) -> discord.Embed:
        embed = discord.Embed(
            title="Typo Tax Repayment",
            description=f"**{discord.utils.escape_markdown(user.display_name)}**, {challenge.prompt}",
            color=0x5865F2,
        )
        embed.add_field(name="Category", value=challenge.category, inline=True)
        embed.add_field(name="Time Limit", value="30 seconds", inline=True)
        embed.set_footer(text="Reply in this channel with the answer.")
        return embed

    def _build_result_embed(
        self,
        user: discord.Member | discord.User,
        title: str,
        description: str,
        color: int,
        balance: int,
    ) -> discord.Embed:
        embed = discord.Embed(title=title, description=description, color=color)
        embed.add_field(name="Balance", value=str(balance), inline=True)
        embed.set_footer(text=f"Repayment for {user.display_name}")
        return embed

    async def start_repayment_from_interaction(self, interaction: discord.Interaction):
        if interaction.channel is None:
            await interaction.response.send_message("I need a channel to run a repayment challenge.", ephemeral=True)
            return
        if self._balance(interaction.user.id) <= 0:
            await interaction.response.send_message("You do not have any typo-tax debt right now.", ephemeral=True)
            return

        challenge = self._make_math_challenge()
        await interaction.response.send_message(embed=self._build_repayment_embed(interaction.user, challenge))
        challenge_message = await interaction.original_response()
        await self._wait_for_repayment_answer(
            channel=interaction.channel,
            user=interaction.user,
            challenge_message=challenge_message,
            challenge=challenge,
        )

    async def start_repayment_from_context(self, ctx: commands.Context):
        if self._balance(ctx.author.id) <= 0:
            await ctx.send(embed=self._build_result_embed(ctx.author, "No Debt", "You do not have any typo-tax debt right now.", 0x57F287, 0))
            return

        challenge = self._make_math_challenge()
        challenge_message = await ctx.send(embed=self._build_repayment_embed(ctx.author, challenge))
        await self._wait_for_repayment_answer(
            channel=ctx.channel,
            user=ctx.author,
            challenge_message=challenge_message,
            challenge=challenge,
        )

    async def _wait_for_repayment_answer(
        self,
        channel: discord.abc.Messageable,
        user: discord.Member | discord.User,
        challenge_message: discord.Message,
        challenge: RepaymentChallenge,
    ) -> None:
        answer = challenge.answer

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == user.id
                and message.channel.id == challenge_message.channel.id
                and message.content.strip().lstrip("-").isdigit()
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=30)
        except asyncio.TimeoutError:
            balance = self._balance(user.id)
            embed = self._build_result_embed(
                user,
                "Time Is Up",
                "Debt stays where it is.",
                0xED4245,
                balance,
            )
            await channel.send(embed=embed, view=RepaymentResultView(self, user.id))
            return

        if int(reply.content.strip()) != int(answer):
            balance = self._balance(user.id)
            embed = self._build_result_embed(
                user,
                "Wrong Answer",
                f"The answer was **{answer}**. Debt stays where it is.",
                0xED4245,
                balance,
            )
            await channel.send(embed=embed, view=RepaymentResultView(self, user.id))
            return

        balance = await self._repay_one(user.id)
        embed = self._build_result_embed(
            user,
            "Debt Repaid",
            "Correct. One typo-tax point has been removed.",
            0x57F287,
            balance,
        )
        await channel.send(embed=embed, view=RepaymentResultView(self, user.id))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        if is_cog_disabled(self.bot.settings, message.guild.id, "typo_tax"):
            return
        if not message.content:
            return

        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        hits = self._detect_typos(message.content)
        if not hits:
            return

        balance = await self._add_tax(message.author.id, len(hits))
        if not self._notifications_enabled(message.author.id):
            return

        try:
            await message.reply(
                embed=self._build_tax_embed(hits, balance),
                view=TypoTaxAlertView(self, message.author.id),
                mention_author=False,
            )
        except discord.HTTPException as e:
            log.warning("Typo tax notification failed: %s", e)

    @helped_hybrid_group("typotax",
        name="typotax",
        invoke_without_command=True,
        case_insensitive=True,
    )
    @commands.guild_only()
    async def typotax(self, ctx: commands.Context):
        enabled = self._notifications_enabled(ctx.author.id)
        detector = "full English spellcheck" if self._spellchecker is not None else "common typo list only"
        await ctx.send(
            "**Typo Tax**\n"
            f"Balance: **{self._balance(ctx.author.id)}**\n"
            f"Notifications: **{'on' if enabled else 'off'}**\n"
            f"Detector: **{detector}**\n\n"
            "`!typotax optin` - get notified when taxed\n"
            "`!typotax repay` - answer math to remove 1 point\n"
            "`!typotax balance [member]` - check a balance"
        )

    @helped_command(typotax, "typotax optin", name="optin")
    @commands.guild_only()
    async def optin(self, ctx: commands.Context):
        await self._set_notifications(ctx.author.id, True)
        await ctx.send("Typo tax notifications are now **on** for you.")

    @helped_command(typotax, "typotax optout", name="optout")
    @commands.guild_only()
    async def optout(self, ctx: commands.Context):
        await self._set_notifications(ctx.author.id, False)
        await ctx.send("Typo tax notifications are now **off** for you. Your balance will still be tracked.")

    @helped_command(typotax, "typotax balance", name="balance")
    @commands.guild_only()
    @app_commands.describe(member="Optional member to check")
    async def balance(self, ctx: commands.Context, member: discord.Member | None = None):
        target = member or ctx.author
        balance = self._balance(target.id)
        await ctx.send(f"**{target.display_name}** has a typo-tax balance of **{balance}**.")

    @helped_command(typotax, "typotax leaderboard", name="leaderboard")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context):
        rows: list[tuple[int, int]] = []
        for user_id, namespaces in self.bot.settings._user_cache.items():
            balance = int(namespaces.get(NAMESPACE, {}).get("balance", 0) or 0)
            if balance > 0:
                rows.append((user_id, balance))

        rows.sort(key=lambda item: item[1], reverse=True)
        if not rows:
            await ctx.send("No typo-tax debt yet.")
            return

        lines = []
        for index, (user_id, balance) in enumerate(rows[:10], start=1):
            member = ctx.guild.get_member(user_id)
            name = member.display_name if member else f"User {user_id}"
            lines.append(f"{index}. **{discord.utils.escape_markdown(name)}** - {balance}")
        await ctx.send("**Typo Tax Leaderboard**\n" + "\n".join(lines))

    @helped_command(typotax, "typotax repay", name="repay")
    @commands.guild_only()
    async def repay(self, ctx: commands.Context):
        await self.start_repayment_from_context(ctx)

    @helped_command(typotax, "typotax forgive", name="forgive")
    @commands.guild_only()
    @commands.has_guild_permissions(manage_messages=True)
    @app_commands.describe(member="Member whose debt should be reduced", amount="Amount to forgive")
    async def forgive(self, ctx: commands.Context, member: discord.Member, amount: int = 1):
        if amount < 1:
            await ctx.send("Amount must be at least 1.")
            return

        balance = max(0, self._balance(member.id) - amount)
        await self._set_balance(member.id, balance)
        await ctx.send(f"Forgave **{amount}** typo-tax point(s) for **{member.display_name}**. Balance: **{balance}**.")

    @forgive.error
    async def _forgive_error(self, ctx: commands.Context, error):
        if isinstance(error, commands.MissingPermissions):
            await ctx.send("You need **Manage Messages** permission to forgive typo-tax debt.")


async def setup(bot: commands.Bot):
    await bot.add_cog(TypoTaxCog(bot))
