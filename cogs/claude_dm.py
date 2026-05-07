"""
claude_dm.py — Remote code editing via DM, powered by the Anthropic Python SDK.

DM the bot from the owner account with any request — "read activity.py",
"add X feature to birthdays.py", etc. — and a Claude agent with file-system
tools will handle it and report back.

No Node.js or Claude Code CLI required. Uses the same anthropic package
that the rest of the bot already depends on.
"""

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path

import discord
from discord.ext import commands

try:
    from anthropic import AsyncAnthropic as _AsyncAnthropic
except ImportError:
    _AsyncAnthropic = None

log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
MODEL         = "claude-sonnet-4-6"   # change to "claude-opus-4-6" for harder tasks
MAX_TURNS     = 25                    # agentic loop cap
MAX_CHARS     = 1900                  # Discord message limit with margin
HISTORY_TURNS = 10                    # number of past user/assistant exchange pairs to keep
# ─────────────────────────────────────────────────────────────────────────────

_BOT_DIR = Path(__file__).parent.parent   # root of the bot

_SYSTEM = (
    "You are a coding assistant with direct access to the bot's file system. "
    "The working directory is the bot root. All paths you receive or provide "
    "are relative to that root.\n\n"
    "When asked to make changes: read the relevant file(s) first, then use "
    "edit_file or write_file to apply the changes. Prefer edit_file for "
    "targeted changes (it does exact string replacement). After making changes, "
    "briefly summarise what you did — no need to repeat the full file content.\n\n"
    "Keep responses concise. The owner is a developer; skip the hand-holding."
)

# ── Tools ────────────────────────────────────────────────────────────────────

def _safe(rel: str) -> Path:
    """Resolve a relative path, rejecting anything that escapes the bot dir."""
    p = (_BOT_DIR / rel).resolve()
    if not str(p).startswith(str(_BOT_DIR.resolve())):
        raise ValueError(f"Path outside bot directory: {rel!r}")
    return p


def _read_file(path: str) -> str:
    try:
        return _safe(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Error: file not found — {path}"
    except Exception as e:
        return f"Error: {e}"


def _write_file(path: str, content: str) -> str:
    try:
        p = _safe(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Written {len(content):,} chars to {path}"
    except Exception as e:
        return f"Error: {e}"


def _edit_file(path: str, old_string: str, new_string: str) -> str:
    try:
        p = _safe(path)
        original = p.read_text(encoding="utf-8")
        count = original.count(old_string)
        if count == 0:
            return f"Error: old_string not found in {path}"
        if count > 1:
            return f"Error: old_string appears {count} times — make it more unique"
        p.write_text(original.replace(old_string, new_string, 1), encoding="utf-8")
        return f"Edit applied to {path}"
    except Exception as e:
        return f"Error: {e}"


def _list_files(pattern: str) -> str:
    try:
        matches = sorted(
            str(p.relative_to(_BOT_DIR))
            for p in _BOT_DIR.glob(pattern)
            if "__pycache__" not in str(p) and "venv" not in str(p).split(os.sep)
        )
        return "\n".join(matches) if matches else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


def _search_files(pattern: str, file_glob: str = "**/*.py") -> str:
    try:
        rx = re.compile(pattern)
        results = []
        for p in sorted(_BOT_DIR.glob(file_glob)):
            if "__pycache__" in str(p) or "venv" in str(p).split(os.sep):
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        results.append(f"{p.relative_to(_BOT_DIR)}:{i}: {line.rstrip()}")
            except Exception:
                continue
        return "\n".join(results[:150]) if results else "(no matches)"
    except re.error as e:
        return f"Error: bad regex — {e}"
    except Exception as e:
        return f"Error: {e}"


def _bash(command: str) -> str:
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True, text=True,
            timeout=30, cwd=str(_BOT_DIR),
        )
        out  = r.stdout[:2000]
        err  = r.stderr[:500]
        return (out + (f"\nSTDERR: {err}" if err else "")).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: timed out (30 s)"
    except Exception as e:
        return f"Error: {e}"


_TOOLS = [
    {
        "name": "read_file",
        "description": "Read the full contents of a file relative to the bot root.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "e.g. 'cogs/birthdays.py'"}},
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Create or fully overwrite a file. Use edit_file for targeted changes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path":    {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "edit_file",
        "description": (
            "Replace an exact, unique string in a file with a new string. "
            "old_string must appear exactly once in the file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path":       {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    },
    {
        "name": "list_files",
        "description": "List files matching a glob pattern (relative to bot root).",
        "input_schema": {
            "type": "object",
            "properties": {"pattern": {"type": "string", "description": "e.g. '**/*.py' or 'data/*'"}},
            "required": ["pattern"],
        },
    },
    {
        "name": "search_files",
        "description": "Search file contents with a regex. Returns matching lines with file:line format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern":   {"type": "string", "description": "Regex pattern"},
                "file_glob": {"type": "string", "description": "File filter glob, default '**/*.py'"},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "bash",
        "description": "Run a shell command in the bot directory. 30-second timeout.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
        },
    },
]

_TOOL_FNS = {
    "read_file":   lambda i: _read_file(i["path"]),
    "write_file":  lambda i: _write_file(i["path"], i["content"]),
    "edit_file":   lambda i: _edit_file(i["path"], i["old_string"], i["new_string"]),
    "list_files":  lambda i: _list_files(i["pattern"]),
    "search_files": lambda i: _search_files(i["pattern"], i.get("file_glob", "**/*.py")),
    "bash":        lambda i: _bash(i["command"]),
}

# ── Discord cog ───────────────────────────────────────────────────────────────


def _split(text: str) -> list[str]:
    if not text:
        return ["(no output)"]
    parts = []
    while text:
        if len(text) <= MAX_CHARS:
            parts.append(text)
            break
        cut = text.rfind("\n", 0, MAX_CHARS)
        if cut < 1:
            cut = MAX_CHARS
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return parts


class ClaudeDMCog(commands.Cog, name="ClaudeDM"):
    """DM the bot to make code changes via Claude. Bot owner only."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        api_key  = os.getenv("ANTHROPIC_API_KEY")
        self.ai  = _AsyncAnthropic(api_key=api_key) if _AsyncAnthropic and api_key else None
        if not self.ai:
            log.warning("ClaudeDMCog: Anthropic unavailable — install anthropic and set ANTHROPIC_API_KEY.")
        # Stores conversation history per DM channel: {channel_id: [msg, ...]}
        self._history: dict[int, list] = {}

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is not None:
            return
        if message.author.bot:
            return
        if not await self.bot.is_owner(message.author):
            return

        text = message.content.strip()
        if not text:
            return

        # Let normal command handler deal with prefix commands
        prefix = self.bot.command_prefix
        prefixes = prefix if isinstance(prefix, (list, tuple)) else [prefix]
        if any(text.startswith(p) for p in prefixes):
            return

        if not self.ai:
            await message.channel.send("❌ Anthropic client not available — check `ANTHROPIC_API_KEY`.")
            return

        async with message.channel.typing():
            result, cost = await self._run(message.channel.id, text)

        chunks = _split(result)
        for i, chunk in enumerate(chunks):
            header = f"*${cost:.4f}*\n" if i == 0 and cost else ""
            await message.channel.send(header + chunk)

    async def _run(self, channel_id: int, prompt: str) -> tuple[str, float]:
        """Run the agentic tool loop. Returns (final_text, approx_cost_usd)."""
        # Build message list from history + new user message
        history   = self._history.get(channel_id, [])
        messages  = history + [{"role": "user", "content": prompt}]
        text_parts = []
        total_cost = 0.0

        for _turn in range(MAX_TURNS):
            try:
                resp = await asyncio.wait_for(
                    self.ai.messages.create(
                        model=MODEL,
                        max_tokens=4096,
                        system=_SYSTEM,
                        tools=_TOOLS,
                        messages=messages,
                    ),
                    timeout=60,
                )
            except asyncio.TimeoutError:
                return "❌ API request timed out (60 s).", total_cost
            except Exception as e:
                log.exception("ClaudeDMCog: API error")
                return f"❌ API error: {e}", total_cost

            # Approximate cost (sonnet-4-6: $3/$15 per 1M in/out)
            usage = getattr(resp, "usage", None)
            if usage:
                inp = getattr(usage, "input_tokens", 0)
                out = getattr(usage, "output_tokens", 0)
                total_cost += (inp * 3 + out * 15) / 1_000_000

            # Collect any text the model emitted this turn
            for block in resp.content:
                if getattr(block, "type", None) == "text" and block.text.strip():
                    text_parts.append(block.text.strip())

            if resp.stop_reason == "end_turn":
                break

            if resp.stop_reason == "tool_use":
                tool_results = []
                for block in resp.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    fn = _TOOL_FNS.get(block.name)
                    if fn:
                        try:
                            result = fn(block.input)
                        except Exception as e:
                            result = f"Error: {e}"
                    else:
                        result = f"Unknown tool: {block.name}"
                    log.debug("Tool %s → %s chars", block.name, len(result))
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user",      "content": tool_results})
            else:
                break   # unexpected stop reason

        final_text = "\n\n".join(text_parts) or "(no text response)"

        # Persist the completed exchange to history, including all tool
        # calls and results so future turns have full context.
        # Add the final assistant message if it wasn't already appended.
        if messages[-1]["role"] != "assistant":
            messages.append({"role": "assistant", "content": final_text})
        # Keep at most HISTORY_TURNS exchange pairs (2 messages each).
        # Each "exchange" may span multiple messages due to tool use, so we
        # trim from the front while preserving the original user prompt as
        # the earliest message.
        max_msgs = HISTORY_TURNS * 2
        self._history[channel_id] = messages[-max_msgs:]

        return final_text, total_cost


async def setup(bot: commands.Bot):
    await bot.add_cog(ClaudeDMCog(bot))
