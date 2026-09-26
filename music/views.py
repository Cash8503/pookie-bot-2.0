from __future__ import annotations

import logging
from typing import Any

import discord

from .player import MusicSession

log = logging.getLogger(__name__)


class MusicControls(discord.ui.View):
    def __init__(self, cog: Any, session: MusicSession):
        super().__init__(timeout=None)
        self.cog = cog
        self.session = session
        self.pause_resume.label = "Resume" if session.is_paused else "Pause"
        self.pause_resume.emoji = "▶️" if session.is_paused else "⏸️"
        self.pause_resume.disabled = session.current is None
        self.skip.disabled = session.current is None
        self.stop.disabled = session.current is None and not session.queue
        self.shuffle.disabled = len(session.queue) < 2
        self.loop.label = f"Loop: {session.loop_mode.title()}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.session.closed:
            await interaction.response.send_message("That music session has ended.", ephemeral=True)
            return False
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Use music controls in a server.", ephemeral=True)
            return False
        user_channel = interaction.user.voice.channel if interaction.user.voice else None
        bot_channel = self.session.voice.channel if self.session.voice else None
        if user_channel is None or user_channel != bot_channel:
            await interaction.response.send_message(
                "Join my voice channel before using these controls.",
                ephemeral=True,
            )
            return False
        return True

    def _can_manage(self, member: discord.Member) -> bool:
        return bool(
            member.guild_permissions.manage_channels
            or (self.session.current and self.session.current.requester_id == member.id)
        )

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_resume(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        if self.session.is_paused:
            await self.session.resume()
        else:
            await self.session.pause()

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.session.skip()

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member) or not self._can_manage(member):
            await interaction.response.send_message(
                "Only the requester or someone with Manage Channels can stop playback.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.session.stop()

    @discord.ui.button(label="Leave", emoji="🔌", style=discord.ButtonStyle.danger, row=0)
    async def leave(self, interaction: discord.Interaction, _: discord.ui.Button):
        member = interaction.user
        if not isinstance(member, discord.Member) or not self._can_manage(member):
            await interaction.response.send_message(
                "Only the requester or someone with Manage Channels can disconnect me.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await self.cog.close_session(self.session.guild_id)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        await self.session.shuffle()

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.defer()
        modes = ("off", "track", "queue")
        next_mode = modes[(modes.index(self.session.loop_mode) + 1) % len(modes)]
        await self.session.set_loop(next_mode)

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.send_message(
            embed=await self.cog.build_queue_embed(self.session),
            ephemeral=True,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        log.error(
            "Music control failed: %s",
            item,
            exc_info=(type(error), error, error.__traceback__),
        )
        message = "That music control failed. Try the matching command instead."
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
