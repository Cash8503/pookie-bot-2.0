import logging
from discord.ext import commands

log = logging.getLogger(__name__)


class TemplateCog(commands.Cog, name="Template"):
    """One-line cog description shown in !help."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def cog_load(self):
        log.info("Cog Loaded.")

    def cog_unload(self):
        log.info("Cog Unloaded.")

    # ------------------------------------------------------------------ #
    #  Example hybrid command group
    # ------------------------------------------------------------------ #

    @commands.hybrid_group(
        name="template",
        invoke_without_command=True,
        case_insensitive=True,
        brief="Short description for !help list",
        help=(
            "Full description shown in !help template.\n\n"
            "Subcommands:\n"
            "  example — Does something\n\n"
            "Add more detail here as needed."
        ),
    )
    async def template(self, ctx: commands.Context):
        await ctx.send(
            "**Template**\n"
            "`!template example` — Does something\n\n"
            "Run `!help template <subcommand>` for full details."
        )

    @template.command(
        name="example",
        brief="Short description for !help list",
        help=(
            "Full description shown in !help template example.\n\n"
            "Explain usage, args, and behaviour here."
        ),
    )
    async def example(self, ctx: commands.Context):
        await ctx.send("Hello from the template cog!")


async def setup(bot: commands.Bot):
    await bot.add_cog(TemplateCog(bot))