"""Music playback cog.

Streams supported media through the local FFmpeg process into Discord voice.
YouTube and selected yt-dlp providers play directly. Spotify links provide
metadata and are matched to a playable YouTube source.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Literal

import discord
from discord.ext import commands

from cogs._help import documented_command, documented_hybrid_group, send_command_help
from music.models import format_duration
from music.player import MusicSession
from music.sources import MusicSourceError, MusicSourceResolver, discover_ffmpeg
from music.views import MusicControls

log = logging.getLogger(__name__)


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, min(value, maximum))


def _safe_text(value: str, limit: int = 100) -> str:
    value = discord.utils.escape_mentions(discord.utils.escape_markdown(str(value)))
    return value[:limit]


class MusicCog(commands.Cog, name="Music"):
    """Play songs and playlists in voice with an interactive control panel."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.max_playlist = _env_int("MUSIC_MAX_PLAYLIST", 100, 1, 500)
        self.default_volume = _env_int("MUSIC_DEFAULT_VOLUME", 50, 0, 100)
        self.idle_timeout = _env_int("MUSIC_IDLE_TIMEOUT", 180, 30, 3600)
        self.resolver = MusicSourceResolver(max_playlist=self.max_playlist)
        self.sessions: dict[int, MusicSession] = {}
        self._panel_locks: dict[int, asyncio.Lock] = {}

    def cog_load(self):
        diagnostics = self.resolver.diagnostics()
        log.info(
            "Music loaded: yt-dlp=%s spotify=%s ffmpeg=%s",
            diagnostics["yt_dlp"],
            diagnostics["spotify"],
            bool(diagnostics["ffmpeg"]),
        )

    async def cog_unload(self):
        sessions = list(self.sessions.values())
        if sessions:
            await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
        log.info("Music cog unloaded; closed %d session(s).", len(sessions))

    async def _get_session(self, guild_id: int) -> MusicSession | None:
        session = self.sessions.get(guild_id)
        if session and session.closed:
            self.sessions.pop(guild_id, None)
            return None
        return session

    async def _connect(self, ctx: commands.Context) -> MusicSession:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            raise MusicSourceError("Use music commands inside a server.")
        user_channel = ctx.author.voice.channel if ctx.author.voice else None
        if user_channel is None:
            raise MusicSourceError("Join a voice channel first.")

        session = await self._get_session(ctx.guild.id)
        if session:
            if session.voice.channel != user_channel:
                raise MusicSourceError(f"I am already playing in {session.voice.channel.mention}.")
            session.text_channel = ctx.channel
            return session

        me = ctx.guild.me
        permissions = user_channel.permissions_for(me) if me else None
        if not permissions or not permissions.connect or not permissions.speak:
            raise MusicSourceError("I need Connect and Speak permissions in your voice channel.")

        ffmpeg_path = discover_ffmpeg()
        if not ffmpeg_path:
            raise MusicSourceError(
                "FFmpeg was not found. Install requirements or set FFMPEG_PATH in .env."
            )

        voice = ctx.guild.voice_client
        if voice and voice.is_connected():
            await voice.move_to(user_channel)
        else:
            voice = await user_channel.connect(self_deaf=True)

        session = MusicSession(
            guild_id=ctx.guild.id,
            voice=voice,
            resolver=self.resolver,
            ffmpeg_path=ffmpeg_path,
            update_callback=self.refresh_panel,
            error_callback=self.send_player_error,
            close_callback=self.session_closed,
            volume=self.default_volume / 100,
            idle_timeout=self.idle_timeout,
        )
        session.text_channel = ctx.channel
        self.sessions[ctx.guild.id] = session
        return session

    async def _require_session(self, ctx: commands.Context) -> MusicSession | None:
        if not ctx.guild:
            await ctx.send("Use music commands inside a server.", ephemeral=True)
            return None
        session = await self._get_session(ctx.guild.id)
        if not session:
            await ctx.send("Nothing is playing here. Use `!music play <song or URL>`.", ephemeral=True)
            return None
        if not isinstance(ctx.author, discord.Member) or not ctx.author.voice:
            await ctx.send("Join my voice channel first.", ephemeral=True)
            return None
        if ctx.author.voice.channel != session.voice.channel:
            await ctx.send(f"Join {session.voice.channel.mention} to control playback.", ephemeral=True)
            return None
        session.text_channel = ctx.channel
        return session

    @staticmethod
    def _can_manage(member: discord.Member, session: MusicSession) -> bool:
        return bool(
            member.guild_permissions.manage_channels
            or (session.current and session.current.requester_id == member.id)
        )

    async def refresh_panel(self, session: MusicSession) -> None:
        if session.closed or session.text_channel is None:
            return
        lock = self._panel_locks.setdefault(session.guild_id, asyncio.Lock())
        async with lock:
            if session.closed or session.text_channel is None:
                return
            embed = await self.build_now_playing_embed(session)
            view = MusicControls(self, session)
            if session.control_message:
                try:
                    await session.control_message.edit(embed=embed, view=view)
                    return
                except (discord.NotFound, discord.HTTPException):
                    session.control_message = None
            try:
                session.control_message = await session.text_channel.send(embed=embed, view=view)
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not post music controls in guild %s", session.guild_id)

    async def send_player_error(self, session: MusicSession, message: str) -> None:
        if session.text_channel:
            try:
                await session.text_channel.send(
                    f"⚠️ {message}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException):
                log.warning("Could not post music error in guild %s", session.guild_id)

    async def session_closed(self, session: MusicSession) -> None:
        if self.sessions.get(session.guild_id) is session:
            self.sessions.pop(session.guild_id, None)
        self._panel_locks.pop(session.guild_id, None)
        if session.control_message:
            embed = discord.Embed(
                title="Music Disconnected",
                description="The voice session has ended.",
                color=0x747F8D,
            )
            try:
                await session.control_message.edit(embed=embed, view=None)
            except (discord.NotFound, discord.HTTPException):
                pass

    async def close_session(self, guild_id: int) -> bool:
        session = self.sessions.get(guild_id)
        if not session:
            return False
        await session.close()
        return True

    async def build_now_playing_embed(self, session: MusicSession) -> discord.Embed:
        queue = await session.queue_snapshot()
        current = session.current
        if current:
            state = "Paused" if session.is_paused else "Now Playing"
            description = f"[{_safe_text(current.title, 200)}]({current.webpage_url})"
            color = 0xFEE75C if session.is_paused else 0x57F287
        elif queue:
            state = "Preparing Next Track"
            description = "Resolving the next audio stream…"
            color = 0x5865F2
        else:
            state = "Queue Empty"
            description = f"Add something with `!music play <song or URL>`."
            color = 0x747F8D

        embed = discord.Embed(title=f"🎵 {state}", description=description, color=color)
        if current:
            embed.add_field(name="Duration", value=format_duration(current.duration), inline=True)
            embed.add_field(name="Requested by", value=current.requester_name, inline=True)
            if current.provider.startswith("Spotify") and current.original_url:
                source_value = (
                    f"[Spotify metadata]({current.original_url}) → "
                    f"[YouTube audio]({current.webpage_url})"
                )
            else:
                source_value = current.provider
            embed.add_field(name="Source", value=source_value, inline=True)
            if current.thumbnail:
                embed.set_thumbnail(url=current.thumbnail)
        embed.add_field(name="Volume", value=f"{round(session.volume * 100)}%", inline=True)
        embed.add_field(name="Loop", value=session.loop_mode.title(), inline=True)
        embed.add_field(name="Queued", value=str(len(queue)), inline=True)
        if queue:
            upcoming = "\n".join(
                f"`{index}.` {_safe_text(track.title, 80)} · {format_duration(track.duration)}"
                for index, track in enumerate(queue[:5], start=1)
            )
            if len(queue) > 5:
                upcoming += f"\n…and {len(queue) - 5} more"
            embed.add_field(name="Up Next", value=upcoming[:1024], inline=False)
        embed.set_footer(text="Controls only work for members in the same voice channel.")
        return embed

    async def build_queue_embed(self, session: MusicSession) -> discord.Embed:
        queue = await session.queue_snapshot()
        embed = discord.Embed(title="Music Queue", color=0x5865F2)
        if session.current:
            embed.description = (
                f"Now: [{_safe_text(session.current.title, 200)}]({session.current.webpage_url})"
            )
        if not queue:
            embed.add_field(name="Up Next", value="The queue is empty.", inline=False)
            return embed
        lines = [
            f"`{index}.` [{_safe_text(track.title, 80)}]({track.webpage_url}) · {format_duration(track.duration)}"
            for index, track in enumerate(queue[:10], start=1)
        ]
        if len(queue) > 10:
            lines.append(f"…and {len(queue) - 10} more")
        embed.add_field(name=f"Up Next ({len(queue)})", value="\n".join(lines)[:1024], inline=False)
        return embed

    @documented_hybrid_group(name="music", invoke_without_command=True, case_insensitive=True)
    @commands.guild_only()
    async def music(self, ctx: commands.Context):
        """Play music in your voice channel

        Queue songs and playlists from YouTube, Spotify, and other enabled providers.

        Usage:
            {prefix}music

        Notes:
            Use {prefix}music play <song or URL> to start."""
        if ctx.guild:
            session = await self._get_session(ctx.guild.id)
            if session:
                session.text_channel = ctx.channel
                await ctx.send(embed=await self.build_now_playing_embed(session), view=MusicControls(self, session))
                return
        await send_command_help(ctx)

    @documented_command(music, name="play")
    @commands.guild_only()
    async def play(self, ctx: commands.Context, *, query: str):
        """Play or queue a song or playlist

        Accepts a search, YouTube link, Spotify track/album/playlist, or another enabled media URL. Spotify metadata is matched to audio from YouTube.

        Usage:
            {prefix}music play <URL or search>

        Arguments:
            query: Song search, playlist link, or supported media URL.

        Examples:
            {prefix}music play The Weeknd Blinding Lights
            {prefix}music play https://www.youtube.com/playlist?list=...
            {prefix}music play https://open.spotify.com/playlist/..."""
        await ctx.defer()
        if not isinstance(ctx.author, discord.Member):
            await ctx.send("Use this command in a server.", ephemeral=True)
            return
        if not ctx.author.voice:
            await ctx.send("Join a voice channel first.", ephemeral=True)
            return
        try:
            result = await asyncio.wait_for(
                self.resolver.resolve(
                    query,
                    requester_id=ctx.author.id,
                    requester_name=ctx.author.display_name,
                ),
                timeout=90,
            )
            session = await self._connect(ctx)
            session.text_channel = ctx.channel
            count = await session.enqueue(result.tracks)
        except MusicSourceError as exc:
            await ctx.send(f"❌ {exc}", ephemeral=True)
            return
        except asyncio.TimeoutError:
            await ctx.send("❌ That playlist took too long to resolve.", ephemeral=True)
            return
        except (discord.ClientException, discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(f"❌ I could not join voice: {exc}", ephemeral=True)
            return

        if count == 1:
            await ctx.send(
                f"✅ Added **{_safe_text(result.tracks[0].title, 200)}** to the queue.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            capped = " (playlist limit reached)" if count >= self.max_playlist else ""
            await ctx.send(
                f"✅ Added **{count} tracks** from **{_safe_text(result.title, 200)}**{capped}.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    @documented_command(music, name="join")
    @commands.guild_only()
    async def join(self, ctx: commands.Context):
        """Join your voice channel

        Connects without starting playback.

        Usage:
            {prefix}music join"""
        try:
            session = await self._connect(ctx)
        except MusicSourceError as exc:
            await ctx.send(f"❌ {exc}", ephemeral=True)
            return
        await ctx.send(f"🔊 Joined {session.voice.channel.mention}.", ephemeral=True)

    @documented_command(music, name="queue")
    @commands.guild_only()
    async def show_queue(self, ctx: commands.Context):
        """Show the current music queue

        Displays the current track and the next ten queued tracks.

        Usage:
            {prefix}music queue"""
        session = await self._require_session(ctx)
        if session:
            await ctx.send(embed=await self.build_queue_embed(session), ephemeral=True)

    @documented_command(music, name="now")
    @commands.guild_only()
    async def now(self, ctx: commands.Context):
        """Show the current track and controls

        Posts a fresh interactive now-playing panel.

        Usage:
            {prefix}music now"""
        session = await self._require_session(ctx)
        if session:
            session.control_message = await ctx.send(
                embed=await self.build_now_playing_embed(session),
                view=MusicControls(self, session),
            )

    @documented_command(music, name="pause")
    @commands.guild_only()
    async def pause(self, ctx: commands.Context):
        """Pause the current track

        Usage:
            {prefix}music pause"""
        session = await self._require_session(ctx)
        if session:
            changed = await session.pause()
            await ctx.send("⏸️ Paused." if changed else "Nothing is currently playing.", ephemeral=True)

    @documented_command(music, name="resume")
    @commands.guild_only()
    async def resume(self, ctx: commands.Context):
        """Resume paused playback

        Usage:
            {prefix}music resume"""
        session = await self._require_session(ctx)
        if session:
            changed = await session.resume()
            await ctx.send("▶️ Resumed." if changed else "Playback is not paused.", ephemeral=True)

    @documented_command(music, name="skip")
    @commands.guild_only()
    async def skip(self, ctx: commands.Context):
        """Skip the current track

        Usage:
            {prefix}music skip"""
        session = await self._require_session(ctx)
        if session:
            changed = await session.skip()
            await ctx.send("⏭️ Skipped." if changed else "Nothing is currently playing.", ephemeral=True)

    @documented_command(music, name="stop")
    @commands.guild_only()
    async def stop(self, ctx: commands.Context):
        """Stop playback and clear the queue

        The requester or a member with Manage Channels can stop the session.

        Usage:
            {prefix}music stop"""
        session = await self._require_session(ctx)
        if not session:
            return
        if not isinstance(ctx.author, discord.Member) or not self._can_manage(ctx.author, session):
            await ctx.send("Only the requester or someone with Manage Channels can stop playback.", ephemeral=True)
            return
        await session.stop()
        await ctx.send("⏹️ Playback stopped and the queue was cleared.", ephemeral=True)

    @documented_command(music, name="shuffle")
    @commands.guild_only()
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queued tracks

        Usage:
            {prefix}music shuffle"""
        session = await self._require_session(ctx)
        if session:
            count = await session.shuffle()
            await ctx.send(f"🔀 Shuffled **{count}** queued tracks.", ephemeral=True)

    @documented_command(music, name="loop")
    @commands.guild_only()
    async def loop(self, ctx: commands.Context, mode: Literal["off", "track", "queue"]):
        """Set the music loop mode

        Loop the current track, the entire queue, or turn looping off.

        Usage:
            {prefix}music loop <off|track|queue>

        Arguments:
            mode: Choose off, track, or queue.

        Examples:
            {prefix}music loop track
            {prefix}music loop off"""
        session = await self._require_session(ctx)
        if session:
            await session.set_loop(mode)
            await ctx.send(f"🔁 Loop mode set to **{mode}**.", ephemeral=True)

    @documented_command(music, name="remove")
    @commands.guild_only()
    async def remove(self, ctx: commands.Context, position: int):
        """Remove a queued track

        Removes a track by its position in the queue display.

        Usage:
            {prefix}music remove <position>

        Arguments:
            position: One-based queue position to remove.

        Examples:
            {prefix}music remove 3"""
        session = await self._require_session(ctx)
        if not session:
            return
        try:
            removed = await session.remove(position)
        except IndexError:
            await ctx.send("That queue position does not exist.", ephemeral=True)
            return
        await ctx.send(
            f"🗑️ Removed **{_safe_text(removed.title, 200)}**.",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @documented_command(music, name="clear")
    @commands.guild_only()
    async def clear(self, ctx: commands.Context):
        """Clear upcoming tracks

        Removes everything after the current track.

        Usage:
            {prefix}music clear"""
        session = await self._require_session(ctx)
        if session:
            count = await session.clear()
            await ctx.send(f"🧹 Removed **{count}** queued tracks.", ephemeral=True)

    @documented_command(music, name="volume")
    @commands.guild_only()
    async def volume(self, ctx: commands.Context, percent: int):
        """Set playback volume

        Sets this server's active music volume from 0 to 100 percent.

        Usage:
            {prefix}music volume <0-100>

        Arguments:
            percent: Volume percentage from 0 through 100.

        Examples:
            {prefix}music volume 50"""
        if percent < 0 or percent > 100:
            await ctx.send("Volume must be between 0 and 100.", ephemeral=True)
            return
        session = await self._require_session(ctx)
        if session:
            await session.set_volume(percent)
            await ctx.send(f"🔊 Volume set to **{percent}%**.", ephemeral=True)

    @documented_command(music, name="leave")
    @commands.guild_only()
    async def leave(self, ctx: commands.Context):
        """Disconnect the music player

        The requester or a member with Manage Channels can end the voice session.

        Usage:
            {prefix}music leave"""
        session = await self._require_session(ctx)
        if not session:
            return
        if not isinstance(ctx.author, discord.Member) or not self._can_manage(ctx.author, session):
            await ctx.send("Only the requester or someone with Manage Channels can disconnect me.", ephemeral=True)
            return
        await self.close_session(session.guild_id)
        await ctx.send("👋 Disconnected from voice.", ephemeral=True)

    @documented_command(music, name="diagnostics")
    @commands.guild_only()
    async def diagnostics(self, ctx: commands.Context):
        """Check local music dependencies

        Reports whether voice encryption, yt-dlp, Spotify metadata, cookies, and FFmpeg are ready.

        Usage:
            {prefix}music diagnostics"""
        checks = self.resolver.diagnostics()
        voice_ready = not discord.VoiceClient.warn_nacl and not discord.VoiceClient.warn_dave
        rows = {
            "Discord voice": voice_ready,
            "yt-dlp": checks["yt_dlp"],
            "Spotify metadata": checks["spotify"],
            "FFmpeg": bool(checks["ffmpeg"]),
            "Optional cookies": checks["cookies"],
        }
        description = "\n".join(f"{'✅' if ready else '❌'} **{name}**" for name, ready in rows.items())
        embed = discord.Embed(title="Music Diagnostics", description=description, color=0x5865F2)
        embed.add_field(name="Playlist limit", value=str(self.max_playlist), inline=True)
        embed.add_field(name="Idle timeout", value=f"{self.idle_timeout}s", inline=True)
        await ctx.send(embed=embed, ephemeral=True)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.bot.user or member.id != self.bot.user.id:
            return
        if before.channel and after.channel is None:
            session = self.sessions.get(member.guild.id)
            if session and not session.closed:
                await session.close()


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
