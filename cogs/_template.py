import logging
from discord.ext import commands

from cogs._help import documented_command, documented_group, documented_hybrid_command, documented_hybrid_group, send_command_help

log = logging.getLogger(__name__)


# Put command help in each callback docstring. The documented decorators parse
# it for prefix help, slash descriptions, argument descriptions, and examples.


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

    @documented_hybrid_group(
        name="template",
        invoke_without_command=True,
        case_insensitive=True,
    )
    async def template(self, ctx: commands.Context):
        """Template command group

        Example command group for new cog scaffolding.

        Usage:
            {prefix}template"""
        await send_command_help(ctx)

    @documented_command(template,
        name="example",
    )
    async def example(self, ctx: commands.Context):
        """Template example command

        Example subcommand for new cog scaffolding.

        Usage:
            {prefix}template example"""
        await ctx.send("Hello from the template cog!")


async def setup(bot: commands.Bot):
    await bot.add_cog(TemplateCog(bot))
