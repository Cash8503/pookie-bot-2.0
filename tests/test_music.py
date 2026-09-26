import asyncio

import pytest

from music.models import Track
from music.player import MusicSession
from music.sources import MusicSourceError, MusicSourceResolver, discover_ffmpeg


class FakeVoice:
    def __init__(self):
        self.source = None
        self.connected = True
        self.playing = False
        self.paused = False

    def is_connected(self):
        return self.connected

    def is_playing(self):
        return self.playing

    def is_paused(self):
        return self.paused

    def pause(self):
        self.playing = False
        self.paused = True

    def resume(self):
        self.playing = True
        self.paused = False

    def stop(self):
        self.playing = False
        self.paused = False

    async def disconnect(self, *, force=False):
        self.connected = False


class FakeResolver:
    async def resolve_stream(self, track):
        raise AssertionError("queue-only test should not resolve audio")


def _track(number: int) -> Track:
    return Track(
        title=f"Track {number}",
        source_url=f"https://youtube.com/watch?v={number}",
        webpage_url=f"https://youtube.com/watch?v={number}",
        requester_id=1,
        requester_name="Tester",
        provider="YouTube",
    )


@pytest.mark.asyncio
async def test_queue_mutations_are_isolated():
    updates = []
    closed = []

    async def update(session):
        updates.append(len(session.queue))

    async def error(session, message):
        raise AssertionError(message)

    async def close(session):
        closed.append(session.guild_id)

    session = MusicSession(
        guild_id=123,
        voice=FakeVoice(),
        resolver=FakeResolver(),
        ffmpeg_path="ffmpeg",
        update_callback=update,
        error_callback=error,
        close_callback=close,
        idle_timeout=3600,
    )
    session._task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await session._task

    assert await session.enqueue([_track(1), _track(2), _track(3)]) == 3
    removed = await session.remove(2)
    assert removed.title == "Track 2"
    assert [track.title for track in await session.queue_snapshot()] == ["Track 1", "Track 3"]
    assert await session.clear() == 2
    await session.close()
    assert closed == [123]
    assert updates


@pytest.mark.asyncio
async def test_private_and_unknown_media_hosts_are_rejected():
    resolver = MusicSourceResolver(max_playlist=5)
    with pytest.raises(MusicSourceError, match="Private-network"):
        await resolver.resolve(
            "http://127.0.0.1/audio.mp3",
            requester_id=1,
            requester_name="Tester",
        )
    with pytest.raises(MusicSourceError, match="not enabled"):
        await resolver.resolve(
            "https://example.com/audio.mp3",
            requester_id=1,
            requester_name="Tester",
        )


def test_ffmpeg_is_available_from_path_or_bundled_dependency():
    assert discover_ffmpeg()
