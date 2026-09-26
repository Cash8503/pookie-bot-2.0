from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Track:
    title: str
    source_url: str
    webpage_url: str
    requester_id: int
    requester_name: str
    provider: str
    duration: int | None = None
    thumbnail: str | None = None
    lookup_query: str | None = None
    original_url: str | None = None


@dataclass(slots=True)
class EnqueueResult:
    title: str
    provider: str
    tracks: list[Track]


@dataclass(slots=True)
class StreamInfo:
    stream_url: str
    webpage_url: str
    title: str
    duration: int | None = None
    thumbnail: str | None = None
    user_agent: str | None = None
    referer: str | None = None


def format_duration(seconds: int | None) -> str:
    if seconds is None or seconds < 0:
        return "live/unknown"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
