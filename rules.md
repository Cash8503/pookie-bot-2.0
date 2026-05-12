# Pookie Bot Command Rules

These rules are not optional. Every command in every active cog must follow this structure.

## Command Structure

- Every user-facing command must be a hybrid command or a subcommand of a hybrid group.
- Every command must have a `CommandHelp` entry in `cogs/_help.py`.
- Command metadata must be centralized in `HELP_CONTENT`; do not make decorator `brief` or `help` text the source of truth.
- Use the helper decorators from `cogs._help`:
  - `helped_hybrid_command("command")`
  - `helped_hybrid_group("group")`
  - `helped_bot_hybrid_command(bot, "command")` for bot-level commands
  - `helped_command(parent_group, "group command")`
  - `helped_group(parent_group, "group subgroup")`
- Required arguments must not fail silently or only log. Missing required arguments must show the generated help embed for that command.
- The root of a command group must show a useful overview when called with no subcommand.
- Slash command descriptions must come from the same `CommandHelp.brief` text as prefix help.
- Any new command must compile and pass the command metadata validation before it is considered done.

## Required CommandHelp Shape

Each entry should define:

```python
CommandHelp(
    brief="One short sentence for command lists and slash descriptions.",
    description="The full help text for detailed help embeds.",
    usage="{prefix}command <required> [optional]",
    examples=("{prefix}command example",),
    notes=("Important constraints or permissions.",),
    subcommands=("{prefix}group subcommand",),
)
```

Use only the fields that make sense, but `brief`, `description`, and `usage` are expected for all normal commands.

## Validation

Run these checks after command changes:

```powershell
$files = @('bot.py') + (Get-ChildItem -Path .\cogs -Recurse -Filter *.py | ForEach-Object { $_.FullName })
python -m py_compile @files
```

The bot also runs metadata validation at startup and after cog hot-reloads.
