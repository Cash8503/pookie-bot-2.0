import logging
from discord.ext import commands

from cogs._help import helped_command, helped_group, helped_hybrid_command, helped_hybrid_group

log = logging.getLogger(__name__)


# Add real command metadata to HELP_CONTENT in cogs/_help.py first.
# The helped_* decorators require that entry and make it the source of truth.


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

    @helped_hybrid_group("template",
        name="template",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def template(self, ctx: commands.Context):
        await ctx.send(
            "**Template**\n"
            "`!template example` — Does something\n\n"
            "Run `!help template <subcommand>` for full details."
        )

    @helped_command(template, "template example",
        name="example",
    )
    async def example(self, ctx: commands.Context):
        await ctx.send("Hello from the template cog!")


async def setup(bot: commands.Bot):
    await bot.add_cog(TemplateCog(bot))
