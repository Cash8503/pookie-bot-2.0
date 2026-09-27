import ast
from pathlib import Path

from cogs._help import documented_hybrid_command, get_help, parse_help_doc


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def test_help_parser_sections():
    meta = parse_help_doc(
        """Play a song

        Adds a song to the queue.

        Usage:
            {prefix}music play <query>

        Arguments:
            query: A song name or URL.

        Examples:
            {prefix}music play example

        Notes:
            Join voice first.
        """
    )
    assert meta is not None
    assert meta.brief == "Play a song"
    assert meta.description == "Adds a song to the queue."
    assert meta.arguments == {"query": "A song name or URL."}
    assert meta.examples == ("{prefix}music play example",)
    assert meta.notes == ("Join voice first.",)


def test_documented_decorator_populates_prefix_and_slash_metadata():
    @documented_hybrid_command(name="documented_test")
    async def documented_test(ctx, value: str):
        """Documented test command

        Detailed command help.

        Arguments:
            value: Value to inspect.
        """

    meta = get_help(documented_test)
    assert meta.brief == "Documented test command"
    assert documented_test.app_command.description == "Documented test command"
    assert documented_test.app_command.parameters[0].description == "Value to inspect."


def test_every_plugin_and_command_has_local_documentation():
    root = Path(__file__).parents[1]
    command_count = 0
    undocumented: list[str] = []
    undocumented_cogs: list[str] = []

    for path in sorted((root / "cogs").rglob("*.py")):
        if path.name in {"_help.py", "_guild_cogs.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                is_cog = any(_decorator_name(base).endswith("Cog") for base in node.bases)
                if is_cog and not ast.get_docstring(node):
                    undocumented_cogs.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            decorator_names = {_decorator_name(decorator) for decorator in node.decorator_list}
            is_command = bool(
                decorator_names
                & {
                    "documented_hybrid_command",
                    "documented_hybrid_group",
                    "documented_bot_hybrid_command",
                    "documented_hybrid_subcommand",
                    "documented_hybrid_subgroup",
                    "hybrid_command",
                    "hybrid_group",
                }
            )
            if not is_command:
                continue
            command_count += 1
            doc = ast.get_docstring(node)
            if not doc or parse_help_doc(doc) is None:
                undocumented.append(f"{path.relative_to(root)}:{node.lineno} {node.name}")

    assert command_count >= 90
    assert undocumented_cogs == []
    assert undocumented == []


def test_central_help_registry_is_gone():
    root = Path(__file__).parents[1]
    help_source = (root / "cogs" / "_help.py").read_text(encoding="utf-8")
    assert "HELP_CONTENT" not in help_source
    for path in (root / "cogs").rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "helped_hybrid" not in source
        assert "helped_command" not in source
