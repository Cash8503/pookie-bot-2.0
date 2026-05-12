import html
import logging
import random

import aiohttp
import discord
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

log = logging.getLogger(__name__)

OPENTDB_URL = "https://opentdb.com/api.php"

DIFFICULTY_COLOR = {
    "easy":   discord.Color.green(),
    "medium": discord.Color.orange(),
    "hard":   discord.Color.red(),
}

# Snarky wrong-answer responses keyed by guess number (1st wrong, 2nd wrong, 3rd+)
_WRONG_RESPONSES = [
    [  # 1st wrong guess — light callout, not mean about it
        "❌ Not that one, but you still got three more chances!",
        "❌ Ooh, so close... (not really). Keep going!",
        "❌ Wrong answer, but hey, first try jitters. You got this.",
        "❌ Nope! But there's still time to redeem yourself.",
        "❌ That wasn't it — lucky guess next time?",
        "❌ Wrong, but I believe in you. Kinda.",
        "❌ Not quite! Process of elimination is a valid strategy.",
    ],
    [  # 2nd wrong guess
        "❌ Still wrong. Are you even trying?",
        "❌ Two for two on wrong answers. Impressive.",
        "❌ Not that one either. Shocking.",
        "❌ You're really committing to being wrong, huh.",
        "❌ Wrong again. At least you're consistent.",
    ],
    [  # 3rd wrong guess (only 1 left — they've exhausted all wrong options on a 4-choice question)
        "❌ Okay at this point just pick the last one.",
        "❌ You've now been wrong more times than there are wrong answers. Talent.",
        "❌ That's three wrong. The answer is literally staring at you.",
        "❌ This is painful to watch.",
        "❌ ...how.",
    ],
]

# Light snark — got it on the second try (1 wrong guess)
_HUMILIATION_MILD = [
    "✅ **{name}** got it on the second try. The answer was **{answer}**. Almost had it! ...almost.",
    "✅ **{name}** needed one little warmup before landing on **{answer}**. We'll allow it.",
    "✅ One wrong guess, then **{answer}**. **{name}** got there. Eventually.",
    "✅ **{answer}** was right there, **{name}**. But hey, one wrong guess is forgivable. Barely.",
    "✅ **{name}** took one detour before finding **{answer}**. GPS rerouting complete. 🗺️",
    "✅ Close-ish! **{name}** got **{answer}** on attempt two. We don't need to mention the first one.",
    "✅ **{name}** got **{answer}**! Technically took two tries but we're being generous today.",
]

# Full humiliation — 2+ wrong guesses before getting it right
_HUMILIATION_BRUTAL = [
    "✅ **{name}** finally got it after {n} wrong guess{es}. The answer was **{answer}**. 💀",
    "✅ **{name}** crawled across the finish line after {n} wrong guess{es}. **{answer}** was right there. 😭",
    "✅ After {n} embarrassing wrong guess{es}, **{name}** stumbled onto **{answer}**. By accident, probably.",
    "✅ **{name}** got it! Only took {n} wrong guess{es} first. **{answer}**. We're so proud. 🙄",
    "✅ **{name}** has arrived. {n} wrong guess{es} later. The answer was **{answer}**. Better late than never I guess.",
    "✅ **{name}** got **{answer}** after {n} wrong guess{es}. Genuinely not sure how you dress yourself in the morning.",
    "✅ {n} wrong guess{es}. The answer was **{answer}**. **{name}**, are you okay? Like, in general?",
    "✅ **{answer}** was the answer, **{name}**. It took you {n} tries to figure that out. Go touch grass.",
    "✅ **{name}** needed {n} attempts to get **{answer}**. I've seen goldfish with better memory.",
    "✅ {n} wrong guess{es} before **{answer}**. **{name}**, I'm not mad, I'm just disappointed. Actually no, I'm a little mad.",
    "✅ **{name}** got **{answer}**! After {n} wrong guess{es}! A trained pigeon could've done it faster!",
    "✅ The answer was **{answer}**. **{name}** missed it {n} time{es}. Some people just aren't built for this.",
    "✅ **{name}** finally clicked **{answer}** after {n} wrong guess{es}. Your parents would not be proud.",
    "✅ {n} wrong guess{es} to get **{answer}**. **{name}**, buddy, do you need help? Serious question.",
    "✅ **{answer}** after {n} wrong guess{es}. **{name}** is out here making the rest of us feel smarter. Thanks for the service. 🫡",
    "✅ **{name}** got **{answer}** on attempt {n}+1. At what point do we check on them as a person?",
    "✅ {n} tries. **{answer}**. **{name}**. I don't have the words. I genuinely don't.",
    "✅ **{name}** eventually clicked **{answer}** after embarrassing themselves {n} time{es}. Character building moment.",
    "✅ **{answer}**. That's it. That's all it was. **{name}** missed it {n} time{es}. Staggering.",
    "✅ After {n} wrong guess{es}, **{name}** found **{answer}**. Took longer than it takes most people to make a sandwich.",
    "✅ **{name}** got **{answer}** after {n} wrong guess{es}. The bar was on the floor and they still almost tripped over it.",
    "✅ {n} wrong guess{es} before **{answer}**. **{name}** is proof that trying isn't always enough. 😔",
    "✅ **{name}** chose **{answer}** after {n} failed attempts. Bold of them to keep going honestly. Most would've quit.",
    "✅ **{answer}** was sitting right there and **{name}** needed {n} tries. I'm not even going to say anything else.",
]


class TriviaView(discord.ui.View):
    """Multiple-choice buttons. Wrong guesses are private; correct answer ends the round."""

    def __init__(self, correct: str, choices: list[str]):
        super().__init__(timeout=30)
        self.correct = correct
        self.answered = False
        self.message: discord.Message | None = None
        self._wrong_counts: dict[int, int] = {}  # user_id -> wrong guess count

        random.shuffle(choices)
        for choice in choices:
            btn = discord.ui.Button(label=choice, style=discord.ButtonStyle.primary)
            btn.callback = self._make_callback(choice)
            self.add_item(btn)

    def _make_callback(self, choice: str):
        async def callback(interaction: discord.Interaction):
            if self.answered:
                await interaction.response.send_message(
                    "Someone already answered this one!", ephemeral=True
                )
                return

            uid = interaction.user.id

            if choice != self.correct:
                self._wrong_counts[uid] = self._wrong_counts.get(uid, 0) + 1
                count = self._wrong_counts[uid]
                bucket = min(count - 1, len(_WRONG_RESPONSES) - 1)
                msg = random.choice(_WRONG_RESPONSES[bucket])
                await interaction.response.send_message(msg, ephemeral=True)
                return

            # Correct answer
            self.answered = True
            self.stop()
            await interaction.response.edit_message(view=None)

            wrong = self._wrong_counts.get(uid, 0)
            if wrong == 0:
                result = f"✅ **{interaction.user.display_name}** got it! The answer was **{self.correct}**."
            elif wrong == 1:
                template = random.choice(_HUMILIATION_MILD)
                result = template.format(
                    name=interaction.user.display_name,
                    answer=self.correct,
                )
            else:
                es = "es" if wrong != 1 else ""
                template = random.choice(_HUMILIATION_BRUTAL)
                result = template.format(
                    name=interaction.user.display_name,
                    n=wrong,
                    es=es,
                    answer=self.correct,
                )

            await interaction.followup.send(result)

        return callback

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(view=None)
                await self.message.channel.send(
                    f"⏰ Time's up! Nobody got it. The answer was **{self.correct}**."
                )
            except discord.HTTPException:
                pass


class TriviaCog(commands.Cog, name="Trivia"):
    """Random trivia questions from Open Trivia DB."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._session: aiohttp.ClientSession | None = None

    def cog_load(self):
        log.info("Cog Loaded.")

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()
        log.info("Cog Unloaded.")

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    # ------------------------------------------------------------------ #
    #  Commands
    # ------------------------------------------------------------------ #

    @helped_hybrid_command("trivia",
        name="trivia",
    )
    async def trivia(self, ctx: commands.Context, difficulty: str | None = None):
        params: dict = {"amount": 1, "type": "multiple"}
        if difficulty and difficulty.lower() in ("easy", "medium", "hard"):
            params["difficulty"] = difficulty.lower()

        session = await self._session_get()
        async with session.get(OPENTDB_URL, params=params) as resp:
            data = await resp.json(content_type=None)

        if data.get("response_code") != 0 or not data.get("results"):
            await ctx.send("❌ Couldn't fetch a question right now. Try again in a moment!", ephemeral=True)
            return

        q = data["results"][0]
        question   = html.unescape(q["question"])
        correct    = html.unescape(q["correct_answer"])
        incorrect  = [html.unescape(a) for a in q["incorrect_answers"]]
        choices    = [correct] + incorrect
        diff       = q.get("difficulty", "easy").lower()
        category   = html.unescape(q.get("category", "General"))

        embed = discord.Embed(
            title="🧠 Trivia Time!",
            description=question,
            color=DIFFICULTY_COLOR.get(diff, discord.Color.blurple()),
        )
        embed.set_footer(text=f"{category} • {diff.title()} • 30s to answer")

        view = TriviaView(correct=correct, choices=choices)
        view.message = await ctx.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(TriviaCog(bot))
