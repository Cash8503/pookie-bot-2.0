from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

from .models import EnqueueResult, StreamInfo, Track

log = logging.getLogger(__name__)

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:  # pragma: no cover - covered by diagnostics on installations without extras
    YoutubeDL = None

    class DownloadError(Exception):
        pass

try:
    from SpotipyFree import Spotify
except ImportError:  # pragma: no cover - covered by diagnostics
    Spotify = None


class MusicSourceError(RuntimeError):
    pass


ALLOWED_MEDIA_HOSTS = {
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "soundcloud.com",
    "bandcamp.com",
    "vimeo.com",
    "twitch.tv",
    "mixcloud.com",
    "dailymotion.com",
}

# Prefer the broadly supported M4A/AAC stream first. Some minimal or bundled
# FFmpeg builds are unstable with specific WebM/Opus inputs; a retry deliberately
# flips the preference so one bad container does not burn through a whole queue.
STREAM_FORMATS = (
    "bestaudio[ext=m4a][acodec!=none]/bestaudio[acodec!=none]/best",
    "bestaudio[ext=webm][acodec!=none]/bestaudio[acodec!=none]/best",
)


def discover_ffmpeg_candidates() -> list[str]:
    candidates: list[str] = []

    def add_candidate(value: str | Path | None) -> None:
        if not value:
            return
        path = Path(value).expanduser()
        if not path.is_file():
            return
        resolved = str(path.resolve())
        if os.path.normcase(resolved) not in {os.path.normcase(item) for item in candidates}:
            candidates.append(resolved)

    configured = os.getenv("FFMPEG_PATH", "").strip().strip('"')
    add_candidate(configured)

    add_candidate(shutil.which("ffmpeg"))

    try:
        import imageio_ffmpeg

        add_candidate(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        log.debug("Bundled FFmpeg discovery failed", exc_info=True)
    return candidates


def discover_ffmpeg() -> str | None:
    candidates = discover_ffmpeg_candidates()
    return candidates[0] if candidates else None


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)


def _normalise_host(host: str) -> str:
    host = host.lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _safe_media_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise MusicSourceError("Only HTTP and HTTPS media links are supported.")
    host = _normalise_host(parsed.hostname)
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and (address.is_private or address.is_loopback or address.is_link_local or address.is_reserved):
        raise MusicSourceError("Private-network and local media addresses are not allowed.")
    if not any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_MEDIA_HOSTS):
        supported = ", ".join(sorted(ALLOWED_MEDIA_HOSTS))
        raise MusicSourceError(f"That provider is not enabled. Supported hosts: {supported}.")
    return value


def _cookie_file() -> str | None:
    configured = os.getenv("YTDLP_COOKIE_FILE", "").strip().strip('"')
    if not configured:
        return None
    path = Path(configured).expanduser()
    return str(path.resolve()) if path.is_file() else None


def _provider_from(info: dict, fallback_url: str) -> str:
    extractor = str(info.get("extractor_key") or info.get("extractor") or "").strip()
    if extractor:
        return extractor.replace("Youtube", "YouTube")
    host = _normalise_host(urlparse(fallback_url).hostname or "media")
    return host.split(".")[0].title()


class MusicSourceResolver:
    def __init__(self, *, max_playlist: int = 100, spotify_workers: int = 6):
        self.max_playlist = max(1, min(int(max_playlist), 500))
        self.spotify_workers = max(2, min(int(spotify_workers), 16))

    def diagnostics(self) -> dict[str, object]:
        return {
            "yt_dlp": YoutubeDL is not None,
            "spotify": Spotify is not None,
            "ffmpeg": discover_ffmpeg(),
            "cookies": bool(_cookie_file()),
        }

    async def resolve(
        self,
        query: str,
        *,
        requester_id: int,
        requester_name: str,
    ) -> EnqueueResult:
        query = query.strip().strip("<>")
        if not query:
            raise MusicSourceError("Give me a song name or playlist link.")

        if _is_url(query) and _normalise_host(urlparse(query).hostname or "") == "open.spotify.com":
            return await asyncio.to_thread(
                self._resolve_spotify,
                query,
                requester_id,
                requester_name,
            )

        if _is_url(query):
            query = _safe_media_url(query)
            extraction_query = query
        else:
            extraction_query = f"ytsearch1:{query}"

        return await asyncio.to_thread(
            self._resolve_ytdlp,
            extraction_query,
            requester_id,
            requester_name,
        )

    async def resolve_stream(self, track: Track, *, attempt: int = 0) -> StreamInfo:
        if YoutubeDL is None:
            raise MusicSourceError("yt-dlp is not installed in the bot environment.")

        target = track.source_url
        if track.lookup_query:
            target = track.resolved_source_url
            if not target:
                target = await asyncio.to_thread(
                    self._find_youtube_match,
                    track.lookup_query,
                    track.duration,
                )
                track.resolved_source_url = target

        format_selector = STREAM_FORMATS[min(max(attempt, 0), len(STREAM_FORMATS) - 1)]
        if attempt > 0 and track.provider.startswith("Spotify"):
            return await asyncio.to_thread(
                self._download_stream,
                target,
                track,
                format_selector,
            )
        return await asyncio.to_thread(
            self._extract_stream,
            target,
            track,
            format_selector,
        )

    def _base_ytdlp_options(self) -> dict:
        options = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "skip_download": True,
            "socket_timeout": 20,
            "retries": 2,
        }
        cookies = _cookie_file()
        if cookies:
            options["cookiefile"] = cookies
        return options

    def _resolve_ytdlp(self, query: str, requester_id: int, requester_name: str) -> EnqueueResult:
        if YoutubeDL is None:
            raise MusicSourceError("yt-dlp is not installed in the bot environment.")
        options = self._base_ytdlp_options()
        options.update({"extract_flat": "in_playlist", "playlistend": self.max_playlist})
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(query, download=False)
        except DownloadError as exc:
            raise MusicSourceError(f"I could not read that media: {exc}") from exc
        if not info:
            raise MusicSourceError("No playable media was found.")

        entries = list(info.get("entries") or [info])[: self.max_playlist]
        tracks: list[Track] = []
        for entry in entries:
            if not entry:
                continue
            source_url = entry.get("webpage_url") or entry.get("url")
            if source_url and not _is_url(str(source_url)) and entry.get("id"):
                source_url = f"https://www.youtube.com/watch?v={entry['id']}"
            if not source_url:
                continue
            title = str(entry.get("title") or entry.get("id") or "Unknown track")
            tracks.append(
                Track(
                    title=title,
                    source_url=str(source_url),
                    webpage_url=str(entry.get("webpage_url") or source_url),
                    requester_id=requester_id,
                    requester_name=requester_name,
                    provider=_provider_from(entry, str(source_url)),
                    duration=int(entry["duration"]) if entry.get("duration") else None,
                    thumbnail=entry.get("thumbnail"),
                    original_url=str(source_url),
                )
            )
        if not tracks:
            raise MusicSourceError("The link did not contain any playable tracks.")
        title = str(info.get("title") or tracks[0].title)
        return EnqueueResult(title=title, provider=tracks[0].provider, tracks=tracks)

    def _spotify_client(self):
        if Spotify is None:
            raise MusicSourceError("Spotify playlist support is not installed.")
        try:
            return Spotify()
        except Exception as exc:
            raise MusicSourceError(f"Spotify metadata could not start: {exc}") from exc

    def _fetch_spotify_pages(self, fetch_page: Callable[[int], dict]) -> list[dict]:
        """Fetch collection pages concurrently while preserving Spotify order."""
        first_page = fetch_page(0)
        first_items = list(first_page.get("items") or [])
        if not first_items:
            return []

        page_size = max(1, int(first_page.get("limit") or len(first_items)))
        total = min(int(first_page.get("total") or len(first_items)), self.max_playlist)
        offsets = list(range(page_size, total, page_size))
        pages: dict[int, list[dict]] = {0: first_items}

        if offsets:
            worker_count = min(self.spotify_workers, len(offsets))
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="spotify-import",
            ) as executor:
                futures = {executor.submit(fetch_page, offset): offset for offset in offsets}
                for future in as_completed(futures):
                    offset = futures[future]
                    page = future.result()
                    pages[offset] = list(page.get("items") or [])

        ordered: list[dict] = []
        for offset in (0, *offsets):
            ordered.extend(pages.get(offset, []))
        return ordered[: self.max_playlist]

    def _resolve_spotify(self, url: str, requester_id: int, requester_name: str) -> EnqueueResult:
        spotify = self._spotify_client()
        path_parts = [part for part in urlparse(url).path.split("/") if part]
        if len(path_parts) < 2:
            raise MusicSourceError("That Spotify link is incomplete.")
        kind = path_parts[0].lower()

        collection_image: str | None = None
        try:
            if kind == "track":
                raw_tracks = [spotify.track(url)]
                collection_title = raw_tracks[0].get("name") or "Spotify track"
                track_album = raw_tracks[0].get("album") or {}
                track_images = track_album.get("images") or []
                collection_image = track_images[0].get("url") if track_images else None
            elif kind == "album":
                album = spotify.album(url)
                album_images = album.get("images") or []
                collection_image = album_images[0].get("url") if album_images else None
                raw_tracks = self._fetch_spotify_pages(
                    lambda offset: spotify.album_tracks(url, limit=50, offset=offset)
                )
                collection_title = album.get("name") or "Spotify album"
            elif kind == "playlist":
                playlist = spotify.playlist(url)
                playlist_images = playlist.get("images") or []
                collection_image = playlist_images[0].get("url") if playlist_images else None
                playlist_items = self._fetch_spotify_pages(
                    lambda offset: spotify.playlist_items(url, limit=50, offset=offset)
                )
                raw_tracks = [
                    item.get("track") or item.get("item") or item for item in playlist_items
                ]
                collection_title = playlist.get("name") or "Spotify playlist"
            else:
                raise MusicSourceError("Spotify track, album, and playlist links are supported.")
        except MusicSourceError:
            raise
        except Exception as exc:
            raise MusicSourceError(f"I could not read that Spotify link: {exc}") from exc

        tracks: list[Track] = []
        for raw in raw_tracks:
            if not raw or raw.get("is_local"):
                continue
            title = str(raw.get("name") or "Unknown track")
            artists = ", ".join(
                str(artist.get("name")) for artist in raw.get("artists") or [] if artist.get("name")
            )
            lookup = f"{artists} - {title} official audio" if artists else f"{title} official audio"
            external = raw.get("external_urls") or {}
            track_url = external.get("spotify") or f"https://open.spotify.com/track/{raw.get('id', '')}"
            album = raw.get("album") or {}
            images = album.get("images") or []
            track_image = images[0].get("url") if images else None
            # A Spotify collection should keep its own cover in the control embed,
            # even though yt-dlp later supplies the YouTube audio and thumbnail.
            artwork = collection_image if kind in {"album", "playlist"} else track_image
            tracks.append(
                Track(
                    title=title,
                    source_url=track_url,
                    webpage_url=track_url,
                    requester_id=requester_id,
                    requester_name=requester_name,
                    provider="Spotify → YouTube",
                    artist=artists or None,
                    duration=int(raw["duration_ms"] / 1000) if raw.get("duration_ms") else None,
                    thumbnail=artwork or track_image or collection_image,
                    lookup_query=lookup,
                    original_url=track_url,
                )
            )
        if not tracks:
            raise MusicSourceError("That Spotify link did not contain any playable tracks.")
        return EnqueueResult(title=str(collection_title), provider="Spotify", tracks=tracks)

    def _find_youtube_match(self, query: str, expected_duration: int | None) -> str:
        options = self._base_ytdlp_options()
        options.update({"extract_flat": True, "playlistend": 5})
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(f"ytsearch5:{query}", download=False)
        except DownloadError as exc:
            raise MusicSourceError(f"No playable match was found for {query}: {exc}") from exc
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise MusicSourceError(f"No playable match was found for {query}.")
        def match_score(entry: dict) -> int:
            title = str(entry.get("title") or "").lower()
            penalty = sum(
                300 for word in ("lyrics", "live", "slowed", "remix", "cover", "karaoke") if word in title
            )
            if "official" in title:
                penalty -= 60
            if expected_duration:
                penalty += abs(int(entry.get("duration") or expected_duration) - expected_duration)
            return penalty

        entries.sort(key=match_score)
        entry = entries[0]
        url = entry.get("webpage_url") or entry.get("url")
        if url and not _is_url(str(url)) and entry.get("id"):
            url = f"https://www.youtube.com/watch?v={entry['id']}"
        if not url:
            raise MusicSourceError(f"The match for {query} had no playable URL.")
        return str(url)

    def _extract_stream(self, target: str, track: Track, format_selector: str) -> StreamInfo:
        options = self._base_ytdlp_options()
        options.update(
            {
                "format": format_selector,
                "noplaylist": True,
            }
        )
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=False)
        except DownloadError as exc:
            raise MusicSourceError(f"The track could not be opened: {exc}") from exc
        if info.get("entries"):
            info = next((entry for entry in info["entries"] if entry), None)
        if not info or not info.get("url"):
            raise MusicSourceError("The provider returned no playable audio stream.")
        uses_spotify_metadata = bool(track.lookup_query and track.provider.startswith("Spotify"))
        return StreamInfo(
            stream_url=str(info["url"]),
            webpage_url=(
                track.webpage_url
                if uses_spotify_metadata
                else str(info.get("webpage_url") or target)
            ),
            title=track.title if uses_spotify_metadata else str(info.get("title") or track.title),
            duration=(
                track.duration
                if uses_spotify_metadata
                else (int(info["duration"]) if info.get("duration") else track.duration)
            ),
            thumbnail=(
                track.thumbnail if uses_spotify_metadata else info.get("thumbnail") or track.thumbnail
            ),
            user_agent=(info.get("http_headers") or {}).get("User-Agent"),
            referer=(info.get("http_headers") or {}).get("Referer"),
        )

    def _download_stream(self, target: str, track: Track, format_selector: str) -> StreamInfo:
        """Buffer one Spotify match so FFmpeg does not handle remote HTTP input."""
        temp_directory = Path(tempfile.mkdtemp(prefix="pookie-music-"))
        options = self._base_ytdlp_options()
        options.update(
            {
                "format": format_selector,
                "noplaylist": True,
                "skip_download": False,
                "outtmpl": str(temp_directory / "audio.%(ext)s"),
                "concurrent_fragment_downloads": 4,
                "max_filesize": 128 * 1024 * 1024,
            }
        )
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(target, download=True)
            if info and info.get("entries"):
                info = next((entry for entry in info["entries"] if entry), None)
            files = [
                path
                for path in temp_directory.iterdir()
                if path.is_file() and not path.name.endswith((".part", ".ytdl"))
            ]
            if not info or not files:
                raise MusicSourceError("The fallback audio could not be buffered.")
            audio_path = max(files, key=lambda path: path.stat().st_size)
            uses_spotify_metadata = track.provider.startswith("Spotify")
            return StreamInfo(
                stream_url=str(audio_path),
                webpage_url=(
                    track.webpage_url
                    if uses_spotify_metadata
                    else str(info.get("webpage_url") or target)
                ),
                title=track.title if uses_spotify_metadata else str(info.get("title") or track.title),
                duration=(
                    track.duration
                    if uses_spotify_metadata
                    else (int(info["duration"]) if info.get("duration") else track.duration)
                ),
                thumbnail=(
                    track.thumbnail
                    if uses_spotify_metadata
                    else info.get("thumbnail") or track.thumbnail
                ),
                cleanup_path=str(temp_directory),
            )
        except DownloadError as exc:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise MusicSourceError(f"The fallback audio could not be downloaded: {exc}") from exc
        except Exception:
            shutil.rmtree(temp_directory, ignore_errors=True)
            raise
