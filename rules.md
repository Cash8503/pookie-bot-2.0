# Pookie Bot Command Rules

These rules apply to every user-facing command in every plugin.

## Command structure

- Commands must be hybrid commands or subcommands of a hybrid group.
- Help belongs beside the command in its function docstring. Do not add a central help registry.
- Use the documented decorators from `cogs._help`:
  - `documented_hybrid_command()`
  - `documented_hybrid_group()`
  - `documented_bot_hybrid_command(bot)` for bot-level commands
  - `documented_command(parent_group)`
  - `documented_group(parent_group)`
- Cog class docstrings describe the plugin for `!help <plugin>`.
- Menu-only group roots call `send_command_help(ctx)` rather than maintaining a second command list.
- Required arguments and invalid values must return generated command help instead of failing silently.
- Slash descriptions and prefix help come from the same local docstring.

## Command help format

The first line is the short command description. The next paragraph is detailed help. Optional sections are `Usage`, `Arguments`, `Examples`, and `Notes`.

```python
@documented_command(example, name="search")
async def search(self, ctx: commands.Context, *, query: str):
    """Search for an example

    Searches the configured provider and displays the best result.

    Usage:
        {prefix}example search <query>

    Arguments:
        query: Words or URL to search for.

    Examples:
        {prefix}example search hello world

    Notes:
        Requires access to the configured provider.
    """
```

`Arguments` names must exactly match callback parameter names. They become slash-command parameter descriptions automatically. Use `{prefix}` instead of hardcoding `!`.

## Validation

Run these checks after command changes:

```powershell
$files = @('bot.py') + @(Get-ChildItem -Path .\cogs, .\music, .\tests -Recurse -Filter *.py | ForEach-Object { $_.FullName })
.\.venv\Scripts\python.exe -m py_compile $files
.\.venv\Scripts\python.exe -m pytest -q
```

The bot also validates local help metadata at startup and after plugin hot-reloads.
