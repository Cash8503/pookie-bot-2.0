import logging
import discord
from discord.ext import commands

log = logging.getLogger(__name__)

# Put the Discord user IDs you want this to apply to here
TARGET_USER_IDS = {
    283023056065003521
}


class AntiGifCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        log.info("Cog Loaded.")

    async def cog_unload(self):
        log.info("Cog Unloaded.")

    def message_has_gif(self, message: discord.Message) -> bool:
        content = (message.content or "").lower()

        # Common gif/link checks
        if ".gif" in content:
            return True
        if "tenor.com" in content:
            return True
        if "giphy.com" in content:
            return True

        # Attachment checks
        for attachment in message.attachments:
            filename = (attachment.filename or "").lower()
            content_type = (attachment.content_type or "").lower()

            if filename.endswith(".gif"):
                return True
            if "gif" in content_type:
                return True

        # Embed checks
        for embed in message.embeds:
            if embed.type in ("gifv", "video"):
                return True

            if embed.url and ".gif" in embed.url.lower():
                return True

            if embed.image and embed.image.url and ".gif" in embed.image.url.lower():
                return True

            if embed.thumbnail and embed.thumbnail.url and ".gif" in embed.thumbnail.url.lower():
                return True

        return False

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        # Ignore bots and DMs
        if message.author.bot or message.guild is None:
            return

        # Only act on selected users
        if message.author.id not in TARGET_USER_IDS:
            return

        if not self.message_has_gif(message):
            return

        try:
            await message.delete()
            log.info(
                "Deleted GIF message from %s (%s) in guild %s, channel %s",
                message.author,
                message.author.id,
                message.guild.id,
                message.channel.id,
            )
        except discord.Forbidden:
            log.warning("Missing permissions to delete message.")
        except discord.HTTPException as e:
            log.warning("Failed to delete message: %s", e)


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiGifCog(bot))