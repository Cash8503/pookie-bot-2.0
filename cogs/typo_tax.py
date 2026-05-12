import asyncio
import logging
import random
import re
from dataclasses import dataclass, replace

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
MIN_STAKE = 1
MAX_STAKE = 5
DEFAULT_CATEGORY = "math"
DEFAULT_STAKE = 1
TIME_LIMIT_SECONDS = 45

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

CATEGORY_LABELS = {
    "math": "Math",
    "grammar": "Grammar",
    "trivia": "Trivia",
    "spelling": "Spelling",
    "linguistics": "Linguistics",
    "random": "Random",
}

CATEGORY_ALIASES = {
    "maths": "math",
    "arithmetic": "math",
    "grammer": "grammar",
    "language": "linguistics",
    "linguistic": "linguistics",
    "words": "linguistics",
    "spell": "spelling",
    "typos": "spelling",
    "mixed": "random",
    "any": "random",
}

_GRAMMAR_QUESTIONS = [
    ("Choose the correct word: I left ___ jacket here. (A) there (B) their (C) they're", "B", ("their",)),
    ("Choose the correct word: ___ going to regret that typo. (A) Your (B) You're", "B", ("you're", "youre")),
    ("Choose the correct word: The bot lost ___ patience. (A) its (B) it's", "A", ("its",)),
    ("Choose the correct word: This affects everyone, but the effect is worse for ___. (A) I (B) me", "B", ("me",)),
    ("Choose the correct word: She writes better ___ I do. (A) then (B) than", "B", ("than",)),
    ("Choose the correct word: Please send the invite to Cash and ___. (A) I (B) me", "B", ("me",)),
    ("Choose the correct word: The typo tax is ___ than before. (A) worse (B) worst", "A", ("worse",)),
    ("Choose the correct word: I have ___ fewer points now. (A) less (B) fewer", "A", ("less",)),
]

_TRIVIA_QUESTIONS = [
    ("What planet is known as the Red Planet?", "Mars", ()),
    ("How many sides does a hexagon have?", "6", ("six",)),
    ("What gas do plants absorb from the air for photosynthesis?", "Carbon dioxide", ("co2", "carbon dioxide")),
    ("What is the capital city of Canada?", "Ottawa", ()),
    ("What is the chemical symbol for gold?", "Au", ("gold",)),
    ("What year did the first iPhone release?", "2007", ()),
    ("Which ocean is the largest?", "Pacific", ("pacific ocean",)),
    ("Who wrote Frankenstein?", "Mary Shelley", ("shelley", "mary shelley")),
]

_LINGUISTICS_QUESTIONS = [
    ("What is the term for a word that sounds like another word but has a different meaning?", "homophone", ()),
    ("What is the plural of criterion?", "criteria", ()),
    ("What is the past tense of bring?", "brought", ()),
    ("What is the root word in unhappiness?", "happy", ()),
    ("What do you call a word that imitates a sound, like buzz or hiss?", "onomatopoeia", ()),
    ("What is the opposite of a prefix?", "suffix", ()),
    ("What part of speech describes an action?", "verb", ()),
    ("What part of speech modifies a noun?", "adjective", ()),
]

_SPELLING_WORDS = tuple(sorted(_COMMON_TYPOS.items()))


@dataclass(frozen=True)
class TypoHit:
    word: str
    suggestion: str


@dataclass(frozen=True)
class RepaymentChallenge:
    category_key: str
    category: str
    difficulty: str
    prompt: str
    answers: tuple[str, ...]
    display_answer: str
    payout: int
    risk: int
    stake: int


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


def _normalize_answer(value: str) -> str:
    cleaned = value.strip().lower()
    cleaned = re.sub(r"^[`\"'(\[]+|[`\"').,!?\]]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _normalize_category(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = CATEGORY_ALIASES.get(cleaned, cleaned)
    if cleaned in CATEGORY_LABELS:
        return cleaned
    return None


def _coerce_stake(value: int | str | None) -> int | None:
    if value is None:
        return None
    try:
        stake = int(value)
    except (TypeError, ValueError):
        return None
    return max(MIN_STAKE, min(MAX_STAKE, stake))


def _difficulty_for_stake(stake: int) -> str:
    if stake <= 1:
        return "Easy"
    if stake == 2:
        return "Medium"
    if stake <= 4:
        return "Hard"
    return "Expert"


def _answer_set(answer: str, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    answers = {_normalize_answer(answer)}
    answers.update(_normalize_answer(alias) for alias in aliases)
    return tuple(sorted(answer for answer in answers if answer))


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
    def __init__(self, cog: "TypoTaxCog", user_id: int, category_key: str | None = None, stake: int | None = None):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.category_key = category_key
        self.stake = stake

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await interaction.response.send_message("This repayment menu belongs to someone else.", ephemeral=True)
        return False

    @discord.ui.button(label="Go Again", style=discord.ButtonStyle.primary)
    async def go_again(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.start_repayment_from_interaction(
            interaction,
            category_key=self.category_key,
            stake=self.stake,
        )

    @discord.ui.button(label="Change Category", style=discord.ButtonStyle.secondary)
    async def change_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Repayment Categories",
            description="Pick a category. It becomes your default and starts the next repayment challenge.",
            color=0x5865F2,
        )
        await interaction.response.send_message(
            embed=embed,
            view=RepaymentOptionsView(self.cog, self.user_id, mode="category", stake=self.stake),
            ephemeral=True,
        )

    @discord.ui.button(label="Change Stakes", style=discord.ButtonStyle.danger)
    async def change_stakes(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="Repayment Stakes",
            description="Higher stakes pay off more points, but missed answers can add risk back onto your balance.",
            color=0xED4245,
        )
        await interaction.response.send_message(
            embed=embed,
            view=RepaymentOptionsView(self.cog, self.user_id, mode="stake", category_key=self.category_key),
            ephemeral=True,
        )


class CategorySelect(discord.ui.Select):
    def __init__(self, cog: "TypoTaxCog", user_id: int, stake: int | None):
        self.cog = cog
        self.user_id = user_id
        self.stake = stake
        options = [
            discord.SelectOption(label=label, value=key, description=f"{label} repayment questions")
            for key, label in CATEGORY_LABELS.items()
        ]
        super().__init__(placeholder="Choose a repayment category", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This category picker belongs to someone else.", ephemeral=True)
            return
        category_key = self.values[0]
        await self.cog._set_preferred_category(interaction.user.id, category_key)
        await interaction.response.send_message(
            f"Category set to **{CATEGORY_LABELS[category_key]}**. Starting a new repayment.",
            ephemeral=True,
        )
        if interaction.channel is not None:
            await self.cog.start_repayment_in_channel(
                interaction.channel,
                interaction.user,
                category_key=category_key,
                stake=self.stake,
            )


class StakeSelect(discord.ui.Select):
    def __init__(self, cog: "TypoTaxCog", user_id: int, category_key: str | None):
        self.cog = cog
        self.user_id = user_id
        self.category_key = category_key
        options = [
            discord.SelectOption(
                label=f"{stake}x",
                value=str(stake),
                description=f"{_difficulty_for_stake(stake)} challenge, repays up to {stake}",
            )
            for stake in range(MIN_STAKE, MAX_STAKE + 1)
        ]
        super().__init__(placeholder="Choose repayment stakes", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This stakes picker belongs to someone else.", ephemeral=True)
            return
        stake = int(self.values[0])
        await self.cog._set_preferred_stake(interaction.user.id, stake)
        await interaction.response.send_message(f"Stakes set to **{stake}x**. Starting a new repayment.", ephemeral=True)
        if interaction.channel is not None:
            await self.cog.start_repayment_in_channel(
                interaction.channel,
                interaction.user,
                category_key=self.category_key,
                stake=stake,
            )


class RepaymentOptionsView(discord.ui.View):
    def __init__(
        self,
        cog: "TypoTaxCog",
        user_id: int,
        *,
        mode: str,
        category_key: str | None = None,
        stake: int | None = None,
    ):
        super().__init__(timeout=120)
        self.cog = cog
        self.user_id = user_id
        if mode == "stake":
            self.add_item(StakeSelect(cog, user_id, category_key))
        else:
            self.add_item(CategorySelect(cog, user_id, stake))


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

    def _preferred_category(self, user_id: int) -> str:
        raw = self.bot.settings.get_user(user_id, NAMESPACE, "category", DEFAULT_CATEGORY)
        return _normalize_category(str(raw)) or DEFAULT_CATEGORY

    async def _set_preferred_category(self, user_id: int, category_key: str) -> None:
        await self.bot.settings.set_user(user_id, NAMESPACE, "category", category_key)

    def _preferred_stake(self, user_id: int) -> int:
        raw = self.bot.settings.get_user(user_id, NAMESPACE, "stake", DEFAULT_STAKE)
        return _coerce_stake(raw) or DEFAULT_STAKE

    async def _set_preferred_stake(self, user_id: int, stake: int) -> None:
        await self.bot.settings.set_user(user_id, NAMESPACE, "stake", _coerce_stake(stake) or DEFAULT_STAKE)

    async def _add_tax(self, user_id: int, amount: int) -> int:
        balance = self._balance(user_id) + amount
        await self._set_balance(user_id, balance)
        total = int(self.bot.settings.get_user(user_id, NAMESPACE, "total_typos", 0) or 0)
        await self.bot.settings.set_user(user_id, NAMESPACE, "total_typos", total + amount)
        return balance

    async def _repay_amount(self, user_id: int, amount: int) -> int:
        amount = max(1, int(amount))
        balance = max(0, self._balance(user_id) - amount)
        await self._set_balance(user_id, balance)
        total = int(self.bot.settings.get_user(user_id, NAMESPACE, "total_repaid", 0) or 0)
        await self.bot.settings.set_user(user_id, NAMESPACE, "total_repaid", total + amount)
        return balance

    async def _apply_repayment_penalty(self, user_id: int, amount: int) -> int:
        amount = max(0, int(amount))
        if amount == 0:
            return self._balance(user_id)
        balance = self._balance(user_id) + amount
        await self._set_balance(user_id, balance)
        total = int(self.bot.settings.get_user(user_id, NAMESPACE, "total_stake_penalties", 0) or 0)
        await self.bot.settings.set_user(user_id, NAMESPACE, "total_stake_penalties", total + amount)
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
        embed.set_footer(text="Use Repay or !typotax repay. Higher stakes repay more points at once.")
        return embed

    def _challenge_shell(
        self,
        *,
        category_key: str,
        prompt: str,
        answer: str,
        aliases: tuple[str, ...] = (),
        stake: int,
        balance: int,
    ) -> RepaymentChallenge:
        effective_stake = max(MIN_STAKE, min(MAX_STAKE, stake, balance))
        return RepaymentChallenge(
            category_key=category_key,
            category=CATEGORY_LABELS[category_key],
            difficulty=_difficulty_for_stake(effective_stake),
            prompt=prompt,
            answers=_answer_set(answer, aliases),
            display_answer=answer,
            payout=effective_stake,
            risk=max(0, effective_stake - 1),
            stake=effective_stake,
        )

    def _make_math_challenge(self, stake: int, balance: int) -> RepaymentChallenge:
        if stake <= 1:
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
            prompt = f"What is `{left} {op} {right}`?"
        elif stake == 2:
            left = random.randint(4, 15)
            right = random.randint(3, 12)
            extra = random.randint(5, 30)
            answer = left * right + extra
            prompt = f"What is `({left} * {right}) + {extra}`?"
        elif stake <= 4:
            divisor = random.randint(2, 9)
            quotient = random.randint(7, 18)
            extra = random.randint(12, 40)
            answer = quotient + extra
            prompt = f"What is `({divisor * quotient} / {divisor}) + {extra}`?"
        else:
            a = random.randint(3, 9)
            b = random.randint(3, 9)
            c = random.randint(2, 7)
            d = random.randint(8, 30)
            answer = (a * b) + (c * c) - d
            prompt = f"What is `({a} * {b}) + ({c}^2) - {d}`?"
        return self._challenge_shell(
            category_key="math",
            prompt=prompt,
            answer=str(answer),
            stake=stake,
            balance=balance,
        )

    def _make_grammar_challenge(self, stake: int, balance: int) -> RepaymentChallenge:
        prompt, answer, aliases = random.choice(_GRAMMAR_QUESTIONS)
        if stake >= 4:
            prompt += " Answer with the letter or the word."
        return self._challenge_shell(
            category_key="grammar",
            prompt=prompt,
            answer=answer,
            aliases=aliases,
            stake=stake,
            balance=balance,
        )

    def _make_trivia_challenge(self, stake: int, balance: int) -> RepaymentChallenge:
        prompt, answer, aliases = random.choice(_TRIVIA_QUESTIONS)
        return self._challenge_shell(
            category_key="trivia",
            prompt=prompt,
            answer=answer,
            aliases=aliases,
            stake=stake,
            balance=balance,
        )

    def _make_spelling_challenge(self, stake: int, balance: int) -> RepaymentChallenge:
        typo, correction = random.choice(_SPELLING_WORDS)
        if stake >= 4:
            prompt = f"Correct this typo: `{typo}`"
            answer = correction
            aliases = ()
        else:
            distractors = random.sample(sorted({word for word in _COMMON_TYPOS.values() if word != correction}), k=2)
            options = [correction, *distractors]
            random.shuffle(options)
            letters = ("A", "B", "C")
            rendered = " ".join(f"({letter}) {word}" for letter, word in zip(letters, options))
            answer_index = options.index(correction)
            answer = letters[answer_index]
            aliases = (correction,)
            prompt = f"Which is the correct spelling for `{typo}`? {rendered}"
        return self._challenge_shell(
            category_key="spelling",
            prompt=prompt,
            answer=answer,
            aliases=aliases,
            stake=stake,
            balance=balance,
        )

    def _make_linguistics_challenge(self, stake: int, balance: int) -> RepaymentChallenge:
        if stake <= 2:
            word = random.choice(("repayment", "language", "syntax", "vocabulary", "correction"))
            count = sum(1 for char in word if char in "aeiou")
            return self._challenge_shell(
                category_key="linguistics",
                prompt=f"How many vowels are in `{word}`?",
                answer=str(count),
                aliases=(str(count),),
                stake=stake,
                balance=balance,
            )
        prompt, answer, aliases = random.choice(_LINGUISTICS_QUESTIONS)
        return self._challenge_shell(
            category_key="linguistics",
            prompt=prompt,
            answer=answer,
            aliases=aliases,
            stake=stake,
            balance=balance,
        )

    def _make_challenge(self, user_id: int, category_key: str | None, stake: int | None) -> RepaymentChallenge | None:
        balance = self._balance(user_id)
        if balance <= 0:
            return None

        requested_category = category_key or self._preferred_category(user_id)
        resolved_category = requested_category
        if resolved_category == "random":
            resolved_category = random.choice([key for key in CATEGORY_LABELS if key != "random"])
        resolved_stake = stake or self._preferred_stake(user_id)
        resolved_stake = max(MIN_STAKE, min(MAX_STAKE, resolved_stake, balance))

        makers = {
            "math": self._make_math_challenge,
            "grammar": self._make_grammar_challenge,
            "trivia": self._make_trivia_challenge,
            "spelling": self._make_spelling_challenge,
            "linguistics": self._make_linguistics_challenge,
        }
        maker = makers.get(resolved_category, self._make_math_challenge)
        challenge = maker(resolved_stake, balance)
        if requested_category == "random":
            return replace(challenge, category_key="random")
        return challenge

    def _build_repayment_embed(self, user: discord.Member | discord.User, challenge: RepaymentChallenge) -> discord.Embed:
        embed = discord.Embed(
            title="Typo Tax Repayment",
            description=f"**{discord.utils.escape_markdown(user.display_name)}**, {challenge.prompt}",
            color=0x5865F2,
        )
        embed.add_field(name="Category", value=challenge.category, inline=True)
        embed.add_field(name="Stake", value=f"{challenge.stake}x", inline=True)
        embed.add_field(name="Difficulty", value=challenge.difficulty, inline=True)
        embed.add_field(name="Pays Off", value=str(challenge.payout), inline=True)
        embed.add_field(name="Risk", value=str(challenge.risk), inline=True)
        embed.add_field(name="Time Limit", value=f"{TIME_LIMIT_SECONDS} seconds", inline=True)
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

    async def start_repayment_from_interaction(
        self,
        interaction: discord.Interaction,
        *,
        category_key: str | None = None,
        stake: int | None = None,
    ):
        if interaction.channel is None:
            await interaction.response.send_message("I need a channel to run a repayment challenge.", ephemeral=True)
            return
        if self._balance(interaction.user.id) <= 0:
            await interaction.response.send_message("You do not have any typo-tax debt right now.", ephemeral=True)
            return

        challenge = self._make_challenge(interaction.user.id, category_key, stake)
        if challenge is None:
            await interaction.response.send_message("You do not have any typo-tax debt right now.", ephemeral=True)
            return
        await interaction.response.send_message(embed=self._build_repayment_embed(interaction.user, challenge))
        challenge_message = await interaction.original_response()
        await self._wait_for_repayment_answer(
            channel=interaction.channel,
            user=interaction.user,
            challenge_message=challenge_message,
            challenge=challenge,
        )

    async def start_repayment_in_channel(
        self,
        channel: discord.abc.Messageable,
        user: discord.Member | discord.User,
        *,
        category_key: str | None = None,
        stake: int | None = None,
    ) -> None:
        if self._balance(user.id) <= 0:
            await channel.send(
                embed=self._build_result_embed(user, "No Debt", "You do not have any typo-tax debt right now.", 0x57F287, 0)
            )
            return

        challenge = self._make_challenge(user.id, category_key, stake)
        if challenge is None:
            await channel.send(
                embed=self._build_result_embed(user, "No Debt", "You do not have any typo-tax debt right now.", 0x57F287, 0)
            )
            return
        challenge_message = await channel.send(embed=self._build_repayment_embed(user, challenge))
        await self._wait_for_repayment_answer(
            channel=channel,
            user=user,
            challenge_message=challenge_message,
            challenge=challenge,
        )

    async def start_repayment_from_context(
        self,
        ctx: commands.Context,
        *,
        category_key: str | None = None,
        stake: int | None = None,
    ):
        if self._balance(ctx.author.id) <= 0:
            await ctx.send(embed=self._build_result_embed(ctx.author, "No Debt", "You do not have any typo-tax debt right now.", 0x57F287, 0))
            return

        challenge = self._make_challenge(ctx.author.id, category_key, stake)
        if challenge is None:
            await ctx.send(embed=self._build_result_embed(ctx.author, "No Debt", "You do not have any typo-tax debt right now.", 0x57F287, 0))
            return
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
        def check(message: discord.Message) -> bool:
            return (
                message.author.id == user.id
                and message.channel.id == challenge_message.channel.id
                and bool(message.content.strip())
            )

        try:
            reply = await self.bot.wait_for("message", check=check, timeout=TIME_LIMIT_SECONDS)
        except asyncio.TimeoutError:
            balance = await self._apply_repayment_penalty(user.id, challenge.risk)
            description = "Debt stays where it is." if challenge.risk == 0 else f"Debt increased by **{challenge.risk}**."
            embed = self._build_result_embed(
                user,
                "Time Is Up",
                description,
                0xED4245,
                balance,
            )
            await channel.send(
                embed=embed,
                view=RepaymentResultView(self, user.id, challenge.category_key, challenge.stake),
            )
            return

        if _normalize_answer(reply.content) not in challenge.answers:
            balance = await self._apply_repayment_penalty(user.id, challenge.risk)
            description = f"The answer was **{challenge.display_answer}**."
            if challenge.risk:
                description += f" Debt increased by **{challenge.risk}**."
            else:
                description += " Debt stays where it is."
            embed = self._build_result_embed(
                user,
                "Wrong Answer",
                description,
                0xED4245,
                balance,
            )
            await channel.send(
                embed=embed,
                view=RepaymentResultView(self, user.id, challenge.category_key, challenge.stake),
            )
            return

        balance = await self._repay_amount(user.id, challenge.payout)
        point_word = "point" if challenge.payout == 1 else "points"
        embed = self._build_result_embed(
            user,
            "Debt Repaid",
            f"Correct. **{challenge.payout}** typo-tax {point_word} removed.",
            0x57F287,
            balance,
        )
        await channel.send(
            embed=embed,
            view=RepaymentResultView(self, user.id, challenge.category_key, challenge.stake),
        )

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
        category = self._preferred_category(ctx.author.id)
        stake = self._preferred_stake(ctx.author.id)
        category_list = ", ".join(f"`{key}`" for key in CATEGORY_LABELS)
        await ctx.send(
            "**Typo Tax**\n"
            f"Balance: **{self._balance(ctx.author.id)}**\n"
            f"Notifications: **{'on' if enabled else 'off'}**\n"
            f"Default category: **{CATEGORY_LABELS[category]}**\n"
            f"Default stakes: **{stake}x**\n"
            f"Detector: **{detector}**\n\n"
            "`!typotax optin` - get notified when taxed\n"
            "`!typotax repay [category] [stake]` - answer a challenge to repay debt\n"
            "`!typotax category [category]` - set your default repayment category\n"
            "`!typotax stake [1-5]` - set your default stakes\n"
            "`!typotax balance [member]` - check a balance\n\n"
            f"Categories: {category_list}"
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
    @app_commands.describe(
        category="math, grammar, trivia, spelling, linguistics, or random",
        stake="1-5. Higher stakes repay more but risk more.",
    )
    async def repay(self, ctx: commands.Context, category: str | None = None, stake: int | None = None):
        category_key = _normalize_category(category) if category else None
        if category and category_key is None:
            await ctx.send(
                "Unknown category. Use one of: "
                + ", ".join(f"`{key}`" for key in CATEGORY_LABELS)
            )
            return

        resolved_stake = _coerce_stake(stake)
        if stake is not None and resolved_stake is None:
            await ctx.send(f"Stake must be a number from {MIN_STAKE} to {MAX_STAKE}.")
            return

        await self.start_repayment_from_context(ctx, category_key=category_key, stake=resolved_stake)

    @helped_command(typotax, "typotax category", name="category")
    @commands.guild_only()
    @app_commands.describe(category="math, grammar, trivia, spelling, linguistics, or random")
    async def category(self, ctx: commands.Context, category: str | None = None):
        current = self._preferred_category(ctx.author.id)
        if category is None:
            await ctx.send(
                f"Your default typo-tax category is **{CATEGORY_LABELS[current]}**.\n"
                "Available: " + ", ".join(f"`{key}`" for key in CATEGORY_LABELS)
            )
            return

        category_key = _normalize_category(category)
        if category_key is None:
            await ctx.send(
                "Unknown category. Use one of: "
                + ", ".join(f"`{key}`" for key in CATEGORY_LABELS)
            )
            return

        await self._set_preferred_category(ctx.author.id, category_key)
        await ctx.send(f"Default typo-tax category set to **{CATEGORY_LABELS[category_key]}**.")

    @helped_command(typotax, "typotax stake", name="stake")
    @commands.guild_only()
    @app_commands.describe(stake="1-5. Higher stakes repay more but risk more.")
    async def stake(self, ctx: commands.Context, stake: int | None = None):
        current = self._preferred_stake(ctx.author.id)
        if stake is None:
            await ctx.send(f"Your default typo-tax stakes are **{current}x**.")
            return

        resolved = _coerce_stake(stake)
        if resolved is None:
            await ctx.send(f"Stake must be a number from {MIN_STAKE} to {MAX_STAKE}.")
            return
        if resolved != stake:
            await ctx.send(f"Stake must be between {MIN_STAKE} and {MAX_STAKE}.")
            return

        await self._set_preferred_stake(ctx.author.id, resolved)
        await ctx.send(f"Default typo-tax stakes set to **{resolved}x**.")

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
