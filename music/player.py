from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import subprocess
import time
from collections import deque
from collections.abc import Awaitable, Callable, Iterable

import discord

from .models import Track
from .sources import MusicSourceError, MusicSourceResolver

log = logging.getLogger(__name__)

UpdateCallback = Callable[["MusicSession"], Awaitable[None]]
ErrorCallback = Callable[["MusicSession", str], Awaitable[None]]
CloseCallback = Callable[["MusicSession"], Awaitable[None]]

PLAYBACK_ATTEMPTS = 2
_MEDIA_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _ffmpeg_stderr_summary(stderr: io.BytesIO) -> str:
    """Return a short diagnostic without leaking signed media URLs."""
    text = stderr.getvalue().decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    summary = " | ".join(lines[-3:])
    return _MEDIA_URL_RE.sub("<media URL>", summary)[:600]


class FFmpegPlaybackError(RuntimeError):
    """Raised when FFmpeg exits unsuccessfully instead of finishing a track."""


class CheckedFFmpegPCMAudio(discord.FFmpegPCMAudio):
    """Make FFmpeg exit codes visible to discord.py's playback callback."""

    def __init__(self, *args, **kwargs):
        self.stderr_capture = io.BytesIO()
        kwargs["stderr"] = self.stderr_capture
        super().__init__(*args, **kwargs)

    def read(self) -> bytes:
        data = super().read()
        if data:
            return data

        process = getattr(self, "_process", None)
        return_code = process.poll() if process else None
        if process and return_code is None:
            try:
                return_code = process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                return_code = process.poll()
        if return_code not in (None, 0):
            detail = _ffmpeg_stderr_summary(self.stderr_capture)
            suffix = f" ({detail})" if detail else ""
            raise FFmpegPlaybackError(f"FFmpeg exited with code {return_code}{suffix}")
        return b""


def _ended_too_early(track: Track, elapsed: float) -> bool:
    """Detect an EOF that is far too early to be a legitimate track ending."""
    if not track.duration or track.duration <= 15:
        return False
    minimum_expected = min(8.0, max(3.0, track.duration * 0.1))
    return elapsed < minimum_expected


class MusicSession:
    def __init__(
        self,
        *,
        guild_id: int,
        voice: discord.VoiceClient,
        resolver: MusicSourceResolver,
        ffmpeg_path: str,
        update_callback: UpdateCallback,
        error_callback: ErrorCallback,
        close_callback: CloseCallback,
        volume: float = 0.5,
        idle_timeout: int = 180,
    ):
        self.guild_id = guild_id
        self.voice = voice
        self.resolver = resolver
        self.ffmpeg_path = ffmpeg_path
        self.update_callback = update_callback
        self.error_callback = error_callback
        self.close_callback = close_callback
        self.volume = max(0.0, min(float(volume), 1.0))
        self.idle_timeout = max(30, int(idle_timeout))

        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.loop_mode = "off"
        self.control_message: discord.Message | None = None
        self.text_channel: discord.abc.Messageable | None = None
        self.started_at: float | None = None
        self.closed = False

        self._queue_event = asyncio.Event()
        self._track_done = asyncio.Event()
        self._queue_lock = asyncio.Lock()
        self._discard_current = False
        self._playback_error: Exception | None = None
        self._task = asyncio.create_task(self._player_loop(), name=f"music-player-{guild_id}")

    @property
    def is_playing(self) -> bool:
        return bool(self.voice and self.voice.is_playing())

    @property
    def is_paused(self) -> bool:
        return bool(self.voice and self.voice.is_paused())

    async def enqueue(self, tracks: Iterable[Track]) -> int:
        items = list(tracks)
        if not items:
            return 0
        async with self._queue_lock:
            self.queue.extend(items)
            self._queue_event.set()
        await self.notify_update()
        return len(items)

    async def queue_snapshot(self) -> list[Track]:
        async with self._queue_lock:
            return list(self.queue)

    async def remove(self, position: int) -> Track:
        async with self._queue_lock:
            if position < 1 or position > len(self.queue):
                raise IndexError(position)
            items = list(self.queue)
            removed = items.pop(position - 1)
            self.queue = deque(items)
        await self.notify_update()
        return removed

    async def clear(self) -> int:
        async with self._queue_lock:
            count = len(self.queue)
            self.queue.clear()
        await self.notify_update()
        return count

    async def shuffle(self) -> int:
        async with self._queue_lock:
            items = list(self.queue)
            random.shuffle(items)
            self.queue = deque(items)
        await self.notify_update()
        return len(items)

    async def set_loop(self, mode: str) -> None:
        if mode not in {"off", "track", "queue"}:
            raise ValueError(mode)
        self.loop_mode = mode
        await self.notify_update()

    async def set_volume(self, percent: int) -> None:
        self.volume = max(0.0, min(percent / 100.0, 1.0))
        source = getattr(self.voice, "source", None)
        if isinstance(source, discord.PCMVolumeTransformer):
            source.volume = self.volume
        await self.notify_update()

    async def pause(self) -> bool:
        if self.voice.is_playing():
            self.voice.pause()
            await self.notify_update()
            return True
        return False

    async def resume(self) -> bool:
        if self.voice.is_paused():
            self.voice.resume()
            await self.notify_update()
            return True
        return False

    async def skip(self) -> bool:
        if not (self.voice.is_playing() or self.voice.is_paused()):
            return False
        self._discard_current = True
        self.voice.stop()
        return True

    async def stop(self) -> None:
        await self.clear()
        self._discard_current = True
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()
        else:
            self.current = None
            await self.notify_update()

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._discard_current = True
        self._queue_event.set()
        self._track_done.set()
        if self.voice.is_playing() or self.voice.is_paused():
            self.voice.stop()

        task = self._task
        if task is not asyncio.current_task() and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self.voice.is_connected():
            try:
                await self.voice.disconnect(force=True)
            except discord.DiscordException:
                log.debug("Voice disconnect failed for guild %s", self.guild_id, exc_info=True)
        try:
            await self.close_callback(self)
        except Exception:
            log.exception("Music close callback failed for guild %s", self.guild_id)

    async def notify_update(self) -> None:
        try:
            await self.update_callback(self)
        except Exception:
            log.exception("Music control update failed for guild %s", self.guild_id)

    async def notify_error(self, message: str) -> None:
        try:
            await self.error_callback(self, message)
        except Exception:
            log.exception("Music error message failed for guild %s", self.guild_id)

    def _after_playback(self, error: Exception | None) -> None:
        self._playback_error = error
        try:
            loop = self._task.get_loop()
            loop.call_soon_threadsafe(self._track_done.set)
        except RuntimeError:
            pass

    async def _next_track(self) -> Track | None:
        async with self._queue_lock:
            if not self.queue:
                self._queue_event.clear()
                return None
            return self.queue.popleft()

    async def _requeue_after_playback(self, track: Track) -> None:
        async with self._queue_lock:
            if self.loop_mode == "track":
                self.queue.appendleft(track)
            elif self.loop_mode == "queue":
                self.queue.append(track)
            if self.queue:
                self._queue_event.set()

    async def _wait_for_queue(self) -> bool:
        try:
            await asyncio.wait_for(self._queue_event.wait(), timeout=self.idle_timeout)
            return not self.closed
        except asyncio.TimeoutError:
            await self.notify_error("Disconnected after being idle with an empty queue.")
            await self.close()
            return False

    async def _play_once(self, track: Track, attempt: int) -> tuple[bool, str | None]:
        self._playback_error = None
        self._track_done.clear()

        stream = await asyncio.wait_for(
            self.resolver.resolve_stream(track, attempt=attempt),
            timeout=60,
        )
        # Spotify remains authoritative for every display field. The YouTube
        # match contributes only its direct audio stream and transport headers.
        if not track.provider.startswith("Spotify"):
            track.webpage_url = stream.webpage_url
            track.title = stream.title or track.title
            track.duration = stream.duration or track.duration
            track.thumbnail = stream.thumbnail or track.thumbnail

        before_options = (
            "-nostdin -rw_timeout 15000000 -reconnect 1 "
            "-reconnect_streamed 1 -reconnect_delay_max 5"
        )
        if stream.user_agent:
            user_agent = stream.user_agent.replace('"', "").replace("\r", "").replace("\n", "")
            before_options += f' -user_agent "{user_agent}"'
        if stream.referer:
            referer = stream.referer.replace('"', "").replace("\r", "").replace("\n", "")
            before_options += f' -referer "{referer}"'

        audio = CheckedFFmpegPCMAudio(
            stream.stream_url,
            executable=self.ffmpeg_path,
            before_options=before_options,
            options="-vn -loglevel warning",
        )
        source = discord.PCMVolumeTransformer(audio, volume=self.volume)
        try:
            self.voice.play(source, after=self._after_playback)
        except Exception:
            source.cleanup()
            raise
        self.started_at = time.monotonic()
        await self.notify_update()
        await self._track_done.wait()

        elapsed = time.monotonic() - self.started_at
        if self._playback_error:
            return False, str(self._playback_error)
        if _ended_too_early(track, elapsed):
            return False, f"the audio stream ended after only {elapsed:.1f} seconds"
        return True, None

    async def _player_loop(self) -> None:
        try:
            while not self.closed:
                track = await self._next_track()
                if track is None:
                    self.current = None
                    self.started_at = None
                    await self.notify_update()
                    if not await self._wait_for_queue():
                        return
                    continue

                if not self.voice.is_connected():
                    await self.notify_error("The voice connection was lost.")
                    await self.close()
                    return

                self.current = track
                self._discard_current = False
                completed = False
                failure: str | None = None
                for attempt in range(PLAYBACK_ATTEMPTS):
                    if self.closed or self._discard_current:
                        break
                    try:
                        completed, failure = await self._play_once(track, attempt)
                    except MusicSourceError as exc:
                        failure = str(exc)
                    except asyncio.TimeoutError:
                        failure = "source resolution timed out"
                    except Exception as exc:
                        failure = str(exc) or type(exc).__name__
                        log.exception(
                            "Playback attempt %s failed for %s",
                            attempt + 1,
                            track.display_title,
                        )

                    if completed or self.closed or self._discard_current:
                        break
                    log.warning(
                        "Playback attempt %s failed for %s: %s",
                        attempt + 1,
                        track.display_title,
                        failure,
                    )
                    if attempt + 1 < PLAYBACK_ATTEMPTS:
                        self.started_at = None
                        await self.notify_error(
                            f"Playback failed for **{track.display_title}**; "
                            "retrying with an alternate audio format."
                        )

                if not completed and not self.closed and not self._discard_current:
                    self._discard_current = True
                    await self.notify_error(
                        f"Skipped **{track.display_title}** after {PLAYBACK_ATTEMPTS} playback attempts: "
                        f"{failure or 'the audio stream ended unexpectedly'}."
                    )
                if not self._discard_current and not self.closed:
                    await self._requeue_after_playback(track)

                self.current = None
                self.started_at = None
                await self.notify_update()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Music player loop crashed for guild %s", self.guild_id)
            await self.notify_error("The music player crashed and disconnected.")
            await self.close()
