import hashlib
import io
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group
from PIL import Image, ImageDraw, ImageFont

log = logging.getLogger(__name__)

# ------------------------------------------------------------------ #
#  Constants
# ------------------------------------------------------------------ #

MAX_GUESSES = 6
WORD_LENGTH  = 5

# Tile colors (RGB)
_BORDER     = (58, 58, 60)
_KEY_UNUSED = (129, 131, 132)
_WHITE      = (255, 255, 255)
_TILE_COLOR = {
    2: (83,  141, 78),   # green  #538d4e
    1: (181, 159, 59),   # yellow #b59f3b
    0: (58,  58,  60),   # gray   #3a3a3c
}

# Board layout
_TILE    = 78
_GAP     = 6
_PAD     = 19

# Keyboard layout
_KEY_W   = 40
_KEY_H   = 50
_KEY_GAP = 10
_KBD_ROWS = ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]

_WORDS_FILE = Path(__file__).parent.parent / "data" / "wordle_words.txt"
_ANSWERS    = [w.strip().upper() for w in _WORDS_FILE.read_text().splitlines() if w.strip()]
VALID_WORDS = set(_ANSWERS)


# ------------------------------------------------------------------ #
#  Core helpers
# ------------------------------------------------------------------ #

def _daily_word(user_id: int, date: str, generation: int = 0) -> str:
    """Return the answer for this user + date + generation."""
    seed = f"{date}-{user_id}-{generation}"
    idx  = int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(_ANSWERS)
    return _ANSWERS[idx].upper()


def _score_guess(guess: str, answer: str) -> list[int]:
    """Return per-letter scores: 2=green, 1=yellow, 0=gray. Handles duplicates correctly."""
    result    = [0] * WORD_LENGTH
    remaining = list(answer)

    # Pass 1: exact matches
    for i, (g, a) in enumerate(zip(guess, answer)):
        if g == a:
            result[i]    = 2
            remaining[i] = None

    # Pass 2: wrong-position matches
    for i, g in enumerate(guess):
        if result[i] == 2:
            continue
        if g in remaining:
            result[i] = 1
            remaining[remaining.index(g)] = None

    return result


# ------------------------------------------------------------------ #
#  Stats helpers
# ------------------------------------------------------------------ #

_EMPTY_STATS = lambda: {
    "played": 0,
    "won": 0,
    "streak": 0,
    "max_streak": 0,
    "last_won_date": None,
    "distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0},
}


async def _update_stats(bot, user_id: int, state: dict) -> None:
    """Record the result of a finished game into lifetime stats."""
    today     = datetime.now(timezone.utc).date().isoformat()
    yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()

    stats = bot.settings.get_user(user_id, "wordle", "stats") or _EMPTY_STATS()

    stats["played"] += 1
    n = len(state["guesses"])

    if state["won"]:
        stats["won"] += 1
        if stats.get("last_won_date") == yesterday:
            stats["streak"] += 1
        else:
            stats["streak"] = 1
        stats["max_streak"]    = max(stats["max_streak"], stats["streak"])
        stats["last_won_date"] = today
        key = str(n)
        stats["distribution"][key] = stats["distribution"].get(key, 0) + 1
    else:
        stats["streak"] = 0

    await bot.settings.set_user(user_id, "wordle", "stats", stats)


def _format_stats(display_name: str, stats: dict) -> str:
    played = stats["played"]
    won    = stats["won"]
    pct    = round(won / played * 100) if played else 0
    streak = stats["streak"]
    best   = stats["max_streak"]
    dist   = stats["distribution"]

    total_guesses = sum(int(k) * v for k, v in dist.items())
    avg = f"{total_guesses / won:.1f}" if won else "—"

    max_count = max(dist.values()) if any(dist.values()) else 1
    BAR = 12
    rows = []
    for i in range(1, MAX_GUESSES + 1):
        count  = dist.get(str(i), 0)
        filled = round(count / max_count * BAR) if max_count else 0
        bar    = "█" * filled + "░" * (BAR - filled)
        rows.append(f"`{i}` {bar}  {count}")

    return (
        f"📊 **{display_name}'s Wordle Stats**\n\n"
        f"Played: **{played}**   Won: **{won}** ({pct}%)   Avg guesses: **{avg}**\n"
        f"Streak: **{streak}**   Best streak: **{best}**\n\n"
        f"**Guess Distribution**\n" + "\n".join(rows)
    )


# ------------------------------------------------------------------ #
#  Image rendering
# ------------------------------------------------------------------ #

def _load_font(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a bold font at the given size, falling back gracefully."""
    candidates = [
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "arialbd.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans.ttf",
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Pillow 10+ supports size param on the built-in font
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    x: int,
    y: int,
    w: int,
    h: int,
    font: ImageFont.FreeTypeFont,
) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = x + (w - tw) // 2 - bbox[0]
    ty = y + (h - th) // 2 - bbox[1]
    draw.text((tx, ty), text, fill=_WHITE, font=font)


def _render_board(guesses: list[str], scores: list[list[int]]) -> io.BytesIO:
    """Render the Wordle board + keyboard as a PNG and return a BytesIO buffer."""
    # Computed areas
    board_area_w = WORD_LENGTH * _TILE + (WORD_LENGTH - 1) * _GAP
    board_area_h = MAX_GUESSES * _TILE + (MAX_GUESSES - 1) * _GAP

    kbd_widths   = [len(row) * _KEY_W + (len(row) - 1) * _KEY_GAP for row in _KBD_ROWS]
    kbd_area_w   = max(kbd_widths)
    kbd_area_h   = len(_KBD_ROWS) * _KEY_H + (len(_KBD_ROWS) - 1) * _KEY_GAP

    img_w = max(board_area_w, kbd_area_w) + _PAD * 2
    img_h = _PAD + board_area_h + _PAD + kbd_area_h + _PAD

    img  = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    tile_font = _load_font(40)
    key_font  = _load_font(18)

    # --- Board grid ---
    board_x = (img_w - board_area_w) // 2

    for row in range(MAX_GUESSES):
        for col in range(WORD_LENGTH):
            x = board_x + col * (_TILE + _GAP)
            y = _PAD   + row * (_TILE + _GAP)

            if row < len(guesses):
                s    = scores[row][col]
                ch   = guesses[row][col]
                fill = _TILE_COLOR[s]
                draw.rectangle([x, y, x + _TILE - 1, y + _TILE - 1], fill=fill)
                _draw_centered(draw, ch, x, y, _TILE, _TILE, tile_font)
            else:
                draw.rectangle(
                    [x, y, x + _TILE - 1, y + _TILE - 1],
                    outline=_BORDER, width=2,
                )

    # --- Keyboard ---
    best: dict[str, int] = {}
    for guess, score in zip(guesses, scores):
        for letter, s in zip(guess, score):
            if best.get(letter, -1) < s:
                best[letter] = s

    kbd_y = _PAD + board_area_h + _PAD

    for ri, row_letters in enumerate(_KBD_ROWS):
        row_w = len(row_letters) * _KEY_W + (len(row_letters) - 1) * _KEY_GAP
        row_x = (img_w - row_w) // 2
        ky    = kbd_y + ri * (_KEY_H + _KEY_GAP)

        for ci, ch in enumerate(row_letters):
            kx = row_x + ci * (_KEY_W + _KEY_GAP)
            s  = best.get(ch, -1)
            if   s == 2: fill = _TILE_COLOR[2]
            elif s == 1: fill = _TILE_COLOR[1]
            elif s == 0: fill = _TILE_COLOR[0]
            else:        fill = _KEY_UNUSED

            draw.rounded_rectangle(
                [kx, ky, kx + _KEY_W - 1, ky + _KEY_H - 1],
                radius=3, fill=fill,
            )
            _draw_centered(draw, ch, kx, ky, _KEY_W, _KEY_H, key_font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


def _status_text(display_name: str, state: dict, answer: str | None = None) -> str:
    n = len(state["guesses"])
    if state["won"]:
        return f"🎉 **{display_name}** solved today's Wordle in **{n}/6**!"
    elif state["finished"]:
        return f"💀 **{display_name}** didn't get today's Wordle. The word was **{answer}**."
    elif n == 0:
        return f"🟩 **{display_name}'s Wordle** — make your first guess!"
    else:
        return f"🟩 **{display_name}'s Wordle** — Guess {n}/{MAX_GUESSES}"


# ------------------------------------------------------------------ #
#  Views & Modal
# ------------------------------------------------------------------ #

class WordleModal(discord.ui.Modal, title="Make Your Guess"):
    guess = discord.ui.TextInput(
        label="5-letter word",
        placeholder="e.g. CRANE",
        min_length=5,
        max_length=5,
    )

    def __init__(self, view: "WordleView"):
        super().__init__()
        self._wv = view

    async def on_submit(self, interaction: discord.Interaction):
        word = self.guess.value.strip().upper()
        if not word.isalpha():
            await interaction.response.send_message("❌ Letters only please.", ephemeral=True)
            return
        if word not in VALID_WORDS:
            await interaction.response.send_message(
                f"❌ **{word}** isn't in the word list — try another word.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._wv.process_guess(interaction, word)


class WordleView(discord.ui.View):
    def __init__(self, bot, owner_id: int):
        super().__init__(timeout=300)
        self._bot      = bot
        self._owner_id = owner_id
        self.message: discord.Message | None = None

    @discord.ui.button(label="Make a Guess", emoji="🔤", style=discord.ButtonStyle.primary)
    async def guess_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self._owner_id:
            await interaction.response.send_message(
                "This isn't your Wordle board! Run `!wordle` to start your own.", ephemeral=True
            )
            return

        state = self._bot.settings.get_user(self._owner_id, "wordle", "active")
        if state and state.get("finished"):
            await interaction.response.send_message(
                "You've already finished today's Wordle!", ephemeral=True
            )
            return

        await interaction.response.send_modal(WordleModal(self))

    async def process_guess(self, interaction: discord.Interaction, word: str):
        # Re-read state fresh (reload-safe)
        state = self._bot.settings.get_user(self._owner_id, "wordle", "active")
        answer = _daily_word(self._owner_id, state["date"], state["generation"])

        if word in state["guesses"]:
            await interaction.followup.send(
                f"You already guessed **{word}**!", ephemeral=True
            )
            return

        score = _score_guess(word, answer)
        state["guesses"].append(word)
        state["scores"].append(score)

        won = all(s == 2 for s in score)
        state["won"]      = won
        state["finished"] = won or len(state["guesses"]) >= MAX_GUESSES

        await self._bot.settings.set_user(self._owner_id, "wordle", "active", state)

        reveal = answer if state["finished"] else None
        buf    = _render_board(state["guesses"], state["scores"])
        file   = discord.File(buf, filename="wordle.png")
        text   = _status_text(interaction.user.display_name, state, reveal)

        if state["finished"]:
            await _update_stats(self._bot, self._owner_id, state)
            self.guess_button.disabled = True

        if self.message:
            await self.message.edit(content=text, attachments=[file], view=self)

    async def on_timeout(self):
        self.guess_button.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ------------------------------------------------------------------ #
#  Cog
# ------------------------------------------------------------------ #

class WordleCog(commands.Cog, name="Wordle"):
    """Daily Wordle — unique word per user, resets at midnight UTC."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    @helped_hybrid_group("wordle",
        name="wordle",
        invoke_without_command=True,
    )
    async def wordle(self, ctx: commands.Context):
        today  = datetime.now(timezone.utc).date().isoformat()
        active = self.bot.settings.get_user(ctx.author.id, "wordle", "active")

        # Start fresh if no active game or it's from a previous day
        if not active or active.get("date") != today:
            active = {"date": today, "generation": 0, "guesses": [], "scores": [], "won": False, "finished": False}
            await self.bot.settings.set_user(ctx.author.id, "wordle", "active", active)

        answer = _daily_word(ctx.author.id, active["date"], active["generation"])
        reveal = answer if active["finished"] else None
        buf    = _render_board(active["guesses"], active["scores"])
        file   = discord.File(buf, filename="wordle.png")
        text   = _status_text(ctx.author.display_name, active, reveal)

        view = WordleView(self.bot, ctx.author.id)
        if active["finished"]:
            view.guess_button.disabled = True

        view.message = await ctx.send(content=text, file=file, view=view)

    @helped_command(wordle, "wordle reset", name="reset")
    async def wordle_reset(self, ctx: commands.Context):
        """Abandon your current word and get a new one. Won't count against your stats."""
        today  = datetime.now(timezone.utc).date().isoformat()
        active = self.bot.settings.get_user(ctx.author.id, "wordle", "active") or {}
        gen    = (active.get("generation", 0) + 1) if active.get("date") == today else 0
        active = {"date": today, "generation": gen, "guesses": [], "scores": [], "won": False, "finished": False}
        await self.bot.settings.set_user(ctx.author.id, "wordle", "active", active)

        buf  = _render_board([], [])
        file = discord.File(buf, filename="wordle.png")
        text = f"🔄 **{ctx.author.display_name}** got a fresh word — good luck!"

        view = WordleView(self.bot, ctx.author.id)
        view.message = await ctx.send(content=text, file=file, view=view)

    @helped_command(wordle, "wordle stats", name="stats")
    async def wordle_stats(self, ctx: commands.Context):
        """Show your lifetime Wordle stats."""
        stats = self.bot.settings.get_user(ctx.author.id, "wordle", "stats")
        if not stats or stats.get("played", 0) == 0:
            await ctx.send(f"No stats yet for **{ctx.author.display_name}** — finish a game first!")
            return
        await ctx.send(_format_stats(ctx.author.display_name, stats))


async def setup(bot: commands.Bot):
    await bot.add_cog(WordleCog(bot))
