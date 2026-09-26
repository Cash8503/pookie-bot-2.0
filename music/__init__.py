"""Self-hosted music playback support for the Discord bot."""

from .models import EnqueueResult, StreamInfo, Track
from .sources import MusicSourceError, MusicSourceResolver, discover_ffmpeg

__all__ = [
    "EnqueueResult",
    "MusicSourceError",
    "MusicSourceResolver",
    "StreamInfo",
    "Track",
    "discover_ffmpeg",
]
