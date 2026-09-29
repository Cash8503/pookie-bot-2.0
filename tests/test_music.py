import asyncio
import io
import threading
from pathlib import Path

import discord
import pytest

import music.sources as music_sources
from music.models import StreamInfo, Track
from music.player import (
    CheckedFFmpegPCMAudio,
    FFmpegPlaybackError,
    MusicSession,
    _ended_too_early,
    _remove_temporary_music_directory,
)
from music.sources import (
    STREAM_FORMATS,
    MusicSourceError,
    MusicSourceResolver,
    discover_ffmpeg,
    discover_ffmpeg_candidates,
)


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
    async def resolve_stream(self, track, *, attempt=0):
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
    assert discover_ffmpeg_candidates()[0] == discover_ffmpeg()


def test_spotify_playlist_tracks_keep_the_playlist_cover():
    class FakeSpotify:
        def playlist(self, url):
            return {
                "name": "Road Trip",
                "images": [{"url": "https://i.scdn.co/playlist-cover.jpg"}],
            }

        def playlist_items(self, url, *, limit, offset):
            return {
                "total": 1,
                "items": [
                    {
                        "track": {
                            "id": "track-1",
                            "name": "First Song",
                            "artists": [{"name": "The Artist"}],
                            "duration_ms": 180_000,
                            "external_urls": {
                                "spotify": "https://open.spotify.com/track/track-1"
                            },
                            "album": {
                                "images": [{"url": "https://i.scdn.co/album-cover.jpg"}]
                            },
                        }
                    }
                ],
            }

    resolver = MusicSourceResolver(max_playlist=10)
    resolver._spotify_client = lambda: FakeSpotify()
    result = resolver._resolve_spotify(
        "https://open.spotify.com/playlist/playlist-1",
        requester_id=1,
        requester_name="Tester",
    )

    assert result.title == "Road Trip"
    assert result.tracks[0].title == "First Song"
    assert result.tracks[0].artist == "The Artist"
    assert result.tracks[0].display_title == "The Artist — First Song"
    assert result.tracks[0].thumbnail == "https://i.scdn.co/playlist-cover.jpg"
    assert result.tracks[0].provider == "Spotify → YouTube"


def test_spotify_collection_pages_are_fetched_concurrently_in_order():
    barrier = threading.Barrier(2)
    worker_names = set()

    def fetch_page(offset):
        if offset:
            worker_names.add(threading.current_thread().name)
            barrier.wait(timeout=2)
        return {
            "limit": 2,
            "total": 6,
            "items": [{"position": offset}, {"position": offset + 1}],
        }

    resolver = MusicSourceResolver(max_playlist=6, spotify_workers=2)
    items = resolver._fetch_spotify_pages(fetch_page)

    assert [item["position"] for item in items] == list(range(6))
    assert len(worker_names) == 2


def test_spotify_stream_resolution_ignores_youtube_display_metadata(monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, target, *, download):
            return {
                "url": "https://audio.example/direct-stream",
                "webpage_url": "https://youtube.com/watch?v=matched",
                "title": "Wrong YouTube Title",
                "duration": 999,
                "thumbnail": "https://youtube.example/thumbnail.jpg",
            }

    monkeypatch.setattr(music_sources, "YoutubeDL", FakeYoutubeDL)
    track = Track(
        title="Spotify Title",
        artist="Spotify Artist",
        source_url="https://open.spotify.com/track/track-1",
        webpage_url="https://open.spotify.com/track/track-1",
        requester_id=1,
        requester_name="Tester",
        provider="Spotify → YouTube",
        duration=180,
        thumbnail="https://i.scdn.co/spotify-cover.jpg",
        lookup_query="Spotify Artist - Spotify Title official audio",
    )

    stream = MusicSourceResolver()._extract_stream(track.source_url, track, STREAM_FORMATS[0])

    assert stream.stream_url == "https://audio.example/direct-stream"
    assert stream.webpage_url == track.webpage_url
    assert stream.title == track.title
    assert stream.duration == track.duration
    assert stream.thumbnail == track.thumbnail


def test_buffered_fallback_downloads_audio_and_preserves_spotify_metadata(monkeypatch):
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def extract_info(self, target, *, download):
            assert download is True
            output = Path(self.options["outtmpl"].replace("%(ext)s", "webm"))
            output.write_bytes(b"buffered audio")
            return {
                "webpage_url": "https://youtube.com/watch?v=matched",
                "title": "Wrong YouTube Title",
                "duration": 999,
                "thumbnail": "https://youtube.example/thumbnail.jpg",
            }

    monkeypatch.setattr(music_sources, "YoutubeDL", FakeYoutubeDL)
    track = Track(
        title="Spotify Title",
        artist="Spotify Artist",
        source_url="https://open.spotify.com/track/track-1",
        webpage_url="https://open.spotify.com/track/track-1",
        requester_id=1,
        requester_name="Tester",
        provider="Spotify → YouTube",
        duration=180,
        thumbnail="https://i.scdn.co/spotify-cover.jpg",
        lookup_query="Spotify Artist - Spotify Title official audio",
    )

    stream = MusicSourceResolver()._download_stream(
        "https://youtube.com/watch?v=matched",
        track,
        STREAM_FORMATS[1],
    )
    try:
        assert Path(stream.stream_url).read_bytes() == b"buffered audio"
        assert stream.cleanup_path
        assert stream.title == track.title
        assert stream.webpage_url == track.webpage_url
        assert stream.duration == track.duration
        assert stream.thumbnail == track.thumbnail
    finally:
        _remove_temporary_music_directory(stream.cleanup_path)
    assert not Path(stream.cleanup_path).exists()


@pytest.mark.asyncio
async def test_spotify_retry_reuses_the_same_youtube_match(monkeypatch):
    resolver = MusicSourceResolver()
    lookups = []
    track = Track(
        title="Spotify Title",
        artist="Spotify Artist",
        source_url="https://open.spotify.com/track/track-1",
        webpage_url="https://open.spotify.com/track/track-1",
        requester_id=1,
        requester_name="Tester",
        provider="Spotify → YouTube",
        duration=180,
        lookup_query="Spotify Artist - Spotify Title official audio",
    )
    stream = StreamInfo(
        stream_url="https://audio.example/direct",
        webpage_url=track.webpage_url,
        title=track.title,
    )

    def find_match(query, duration):
        lookups.append((query, duration))
        return "https://youtube.com/watch?v=matched"

    monkeypatch.setattr(resolver, "_find_youtube_match", find_match)
    monkeypatch.setattr(resolver, "_extract_stream", lambda *args: stream)
    monkeypatch.setattr(resolver, "_download_stream", lambda *args: stream)

    await resolver.resolve_stream(track, attempt=0)
    await resolver.resolve_stream(track, attempt=1)

    assert len(lookups) == 1
    assert track.resolved_source_url == "https://youtube.com/watch?v=matched"


def test_retry_switches_from_m4a_to_webm():
    assert "ext=m4a" in STREAM_FORMATS[0]
    assert "ext=webm" in STREAM_FORMATS[1]


def test_ffmpeg_nonzero_exit_becomes_a_playback_error(monkeypatch):
    class FailedProcess:
        def poll(self):
            return -11

    source = object.__new__(CheckedFFmpegPCMAudio)
    source._process = FailedProcess()
    source.stderr_capture = io.BytesIO(b"failed to read https://signed.example/audio?token=secret")
    monkeypatch.setattr(discord.FFmpegPCMAudio, "read", lambda self: b"")

    with pytest.raises(FFmpegPlaybackError, match="code -11") as exc_info:
        source.read()
    assert "token=secret" not in str(exc_info.value)


def test_short_eof_is_only_an_error_for_a_normal_length_track():
    assert _ended_too_early(_track(1), 1.0) is False
    track = _track(2)
    track.duration = 180
    assert _ended_too_early(track, 2.0) is True
    assert _ended_too_early(track, 20.0) is False
