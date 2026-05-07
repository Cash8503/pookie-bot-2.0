# cogs/name_responder.py
import re
import random
import asyncio
import time
from typing import Callable, Awaitable, Dict, List
import discord
from discord.ext import commands

# ---------- Helpers ----------

def loose_name_regex(name: str) -> re.Pattern:
    """
    Build a case-insensitive whole-word regex that tolerates repeated letters.
    """
    
    letters = "+".join(map(re.escape, name))
    pattern = rf"(?i)\b{letters}"
    return re.compile(pattern, re.IGNORECASE)

def exact_regex(token: str) -> re.Pattern:
    """
    Case-insensitive exact matcher for a token.
    - No letter stretching.
    - Works with symbols like ':3'.
    - If the token has any word characters, require word-like boundaries.
      For pure-symbol tokens, skip boundaries so ':3' matches anywhere.
    """
    escaped = re.escape(token)
    if re.search(r"\w", token):
        # token has letters/digits/underscore -> don't match inside words
        return re.compile(rf"(?i)(?<!\w){escaped}(?!\w)")
    else:
        # pure symbols -> boundaries would block cases like ':3!'
        return re.compile(rf"(?i){escaped}")


async def safe_reply(message: discord.Message, content: str = None, **kwargs):
    """Reply but fail quietly if we lack perms or something goes wrong."""
    try:
        await message.reply(content, **kwargs)
    except Exception:
        # Fallback: try channel send (non-reply)
        try:
            await message.channel.send(content, **kwargs)
        except Exception:
            pass

async def safe_react(message: discord.Message, emoji: str):
    try:
        await message.add_reaction(emoji)
    except Exception:
        pass

# ---------- Configure your triggers here ----------

# Each entry maps a friendly key to:
#   - "pattern": compiled regex to detect name
#   - "actions": list of async callables taking (message) that run when matched
# Add/modify to taste.
def make_triggers() -> Dict[str, Dict]:
    return {
        "parker": {
            "pattern": loose_name_regex("parker"),
            "actions": [
                # Reply with a gif
                lambda m: safe_reply(m, random.choice([
                    "https://tenor.com/view/albedo-gif-25353479",
                    "https://tenor.com/view/breachers-vr-discord-discord-breachers-breachers-sidequest-gif-27532656",
                    "peenar parker.",
                ])),
                # Optional: add a reaction too
                #lambda m: safe_react(m, "✨"),
            ],
        },
        "stephanie": {
            "pattern": loose_name_regex("stephanie"),
            "actions": [
                # Randomize between a few responses
                lambda m: safe_reply(m, random.choice([
                    "Stepalina bo bina!",
                    "Stephanie supremacy ✨",
                    "Did someone say Stephanie?",
                    "ice is coming.",
                ])),
                #lambda m: safe_react(m, "💅"),
            ],
        },
        "madison": {
            "pattern": loose_name_regex("madison"),
            "actions": [
                lambda m: safe_reply(m, random.choice([
                    "Madi-chan!",
                    "HEY! easy on the government.",
                ])),
                #lambda m: safe_react(m, "🎯"),
            ],
        },
        "anderson": {
            "pattern": loose_name_regex("anderson"),
            "actions": [
                lambda m: safe_reply(m, "shut up ur not included"),
                lambda m: safe_react(m, "🖕"),
            ],
        },
        "julian": {
            "pattern": loose_name_regex("julian"),
            "actions": [
                lambda m: safe_reply(m, "hate that guy"),
                #lambda m: safe_react(m, "🖕"),
            ],
        },
        "trinity": {
            "pattern": loose_name_regex("trinity"),
            "actions": [
                lambda m: safe_reply(m, "HER NAME IS BABUSHKA"),
                #lambda m: safe_react(m, "🖕"),
            ],
        },
        "christian": {
            "pattern": loose_name_regex("christian"),
            "actions": [
            
                lambda m: safe_react(m, "✝️"),
            ],
        },
        "leigh": {
            "pattern": loose_name_regex("leigh"),
            "actions": [
                lambda m: safe_reply(m, "my little queer boy"),
                lambda m: safe_react(m, "🌈"),
            ],
        },
        "jose": {
            "pattern": loose_name_regex("jose"),
            "actions": [
                lambda m: safe_reply(m, "ice is coming"),
                lambda m: safe_react(m, "🧊"),
            ],
        },
        "actually": {
            "pattern": loose_name_regex("actually"),
            "actions": [
                lambda m: safe_reply(m, random.choice([
                    "https://tenor.com/view/nerd-well-actually-as-a-matter-of-fact-gif-27665011",
                    "https://tenor.com/view/nerd-ackchyually-actually-gif-11156171",
                    "https://tenor.com/view/actually-nerd-actually-nerd-glasses-well-yes-but-actually-no-gif-5597798959730072444"
                ])),
                
            ],
        },
        ":3": {
            "pattern": exact_regex(":3"),
            "actions": [
                lambda m: safe_reply(m, random.choice([
                    "shut up faggot.",
                    "furry shit 👎",
                    "kill yourself"
                ])),
                
            ],
        },
    }

COOLDOWN_SECONDS = 3.0   # per-channel cooldown so the bot doesn't spam

class NameResponder(commands.Cog):
    """Replies/reacts when people say specific names with custom actions."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.triggers = make_triggers()   # {key: {"pattern": compiled, "actions": [callables]}}
        self._last_fired: Dict[int, float] = {}  # channel_id -> timestamp

    def _cooldown_ok(self, channel_id: int) -> bool:
        now = time.time()
        last = self._last_fired.get(channel_id, 0.0)
        if now - last >= COOLDOWN_SECONDS:
            self._last_fired[channel_id] = now
            return True
        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots (including ourselves)
        if message.author.bot:
            return

        # Don't respond to empty content (attachments only, etc.)
        if not message.content:
            return
        
        sm = self.bot.get_cog("SeriousMode")
        if sm and sm.is_active(message.guild):
            return  # do nothing while serious mode is on

        # Optionally ignore DMs:
        # if not message.guild:
        #     return

        content = message.content

        matched_any = False
        # Run all triggers whose pattern matches
        for key, cfg in self.triggers.items():
            pattern: re.Pattern = cfg["pattern"]
            actions: List[Callable[[discord.Message], Awaitable[None]]] = cfg["actions"]

            if pattern.search(content):
                matched_any = True
                # Respect simple per-channel cooldown
                if not self._cooldown_ok(message.channel.id):
                    continue

                # Run actions sequentially but don't die on one failure
                for action in actions:
                    try:
                        await action(message)
                    except Exception:
                        pass

        # Ensure commands still work if you use this event
        if matched_any:
            # tiny delay to avoid racing replies with command responses
            await asyncio.sleep(0)
        

async def setup(bot: commands.Bot):
    print("Loading Deadnaming")
    await bot.add_cog(NameResponder(bot))
