"""
sync_core.py — Shared utilities for the push-based sync daemon.

Imported by both server.py (AMP side) and client.py (local PC side).
"""

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

log = logging.getLogger("sync")

# ── Paths & constants ─────────────────────────────────────────────────────────

ROOT = Path(__file__).parent.parent   # bot project root

EXCLUDE_DIRS = {
    "venv", ".venv", "env", "__pycache__", ".git",
    "sync", "old cogs archive",
}
EXCLUDE_FILES = {".env", ".DS_Store"}

DB_REL          = "data/data.db"
DB_AUX_SUFFIXES = ("-wal", "-shm", "-journal")

# ── File filtering ────────────────────────────────────────────────────────────

def is_db(rel: str) -> bool:
    """True for the main DB file and its SQLite auxiliary files."""
    return rel == DB_REL or any(rel.endswith(s) for s in DB_AUX_SUFFIXES)


def should_sync(rel: str) -> bool:
    """True if this relative path is eligible for sync (either direction)."""
    parts = Path(rel).parts
    # Skip if any directory component is in the exclusion list
    for part in parts[:-1]:
        if part in EXCLUDE_DIRS:
            return False
    name = parts[-1]
    # Skip excluded filenames
    if name in EXCLUDE_FILES:
        return False
    # Skip SQLite WAL / SHM / journal — copying these while the bot runs is unsafe
    if any(rel.endswith(s) for s in DB_AUX_SUFFIXES):
        return False
    # Skip backup files and editor temp files (e.g. data.db.bak_20260401, file.py.tmp.1234)
    if ".bak" in name or ".tmp." in name:
        return False
    return True

# ── Manifest ──────────────────────────────────────────────────────────────────

def build_manifest(root: Path) -> dict[str, float]:
    """Return {posix_rel_path: mtime_float} for every syncable file."""
    result: dict[str, float] = {}
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if not should_sync(rel):
            continue
        try:
            result[rel] = p.stat().st_mtime
        except OSError:
            pass
    return result

# ── Message protocol (JSON-lines over TCP) ────────────────────────────────────

async def send_msg(writer: asyncio.StreamWriter, msg: dict) -> None:
    writer.write(json.dumps(msg).encode() + b"\n")
    await writer.drain()


async def recv_msg(reader: asyncio.StreamReader) -> dict | None:
    """Read one JSON line. Returns None on timeout, EOF, or parse error."""
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=60)
        if not line:
            return None
        return json.loads(line.decode())
    except (asyncio.TimeoutError, asyncio.LimitOverrunError,
            json.JSONDecodeError, UnicodeDecodeError):
        return None

# ── File push / apply ─────────────────────────────────────────────────────────

def make_push_msg(root: Path, rel: str) -> dict:
    """Build a push message containing the full file content as base64."""
    p = root / rel
    return {
        "t":     "push",
        "path":  rel,
        "mtime": p.stat().st_mtime,
        "data":  base64.b64encode(p.read_bytes()).decode(),
    }


def apply_push_msg(root: Path, msg: dict) -> bool:
    """
    Write the file from a push message if it is newer than our local copy.
    Preserves the original mtime to prevent re-triggering the watchdog loop.
    Returns True if the file was written.
    """
    rel   = msg["path"]
    mtime = float(msg["mtime"])
    dest  = root / rel
    if dest.exists() and dest.stat().st_mtime >= mtime:
        return False   # local copy is same age or newer
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(msg["data"]))
    os.utime(dest, (mtime, mtime))
    return True

# ── Watchdog → asyncio bridge ─────────────────────────────────────────────────

class _WatchHandler(FileSystemEventHandler):
    def __init__(self, root: Path, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self._root  = root
        self._queue = queue
        self._loop  = loop

    def _enqueue(self, path: str, delete: bool = False) -> None:
        try:
            rel = Path(path).relative_to(self._root).as_posix()
        except ValueError:
            return
        if not should_sync(rel):
            return
        kind  = "delete" if delete else "push"
        event = (kind, rel)
        asyncio.run_coroutine_threadsafe(self._queue.put(event), self._loop)

    def on_modified(self, e):
        if not e.is_directory: self._enqueue(e.src_path)

    def on_created(self, e):
        if not e.is_directory: self._enqueue(e.src_path)

    def on_deleted(self, e):
        if not e.is_directory: self._enqueue(e.src_path, delete=True)

    def on_moved(self, e):
        if not e.is_directory:
            self._enqueue(e.src_path, delete=True)
            self._enqueue(e.dest_path)


def start_watcher(root: Path, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> Observer:
    obs = Observer()
    obs.schedule(_WatchHandler(root, queue, loop), str(root), recursive=True)
    obs.start()
    return obs

# ── Session (shared server/client logic) ──────────────────────────────────────

async def run_session(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    side:   str,    # "server" or "client"
    root:   Path,
) -> None:
    """
    Run a full sync session over an established TCP connection.

    Sync rules:
      • DB files (data/data.db + aux)  →  server → client ONLY
      • Everything else                →  bidirectional, newer mtime wins
      • Excluded dirs/files            →  never synced
    """
    loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
    queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    # ── Step 1: exchange manifests concurrently (avoid deadlock on large manifests) ──
    my_manifest = build_manifest(root)
    send_t = asyncio.create_task(
        send_msg(writer, {"t": "manifest", "side": side, "ver": 1, "files": my_manifest})
    )
    recv_t = asyncio.create_task(recv_msg(reader))
    await asyncio.gather(send_t, recv_t)

    remote_msg = recv_t.result()
    if not remote_msg or remote_msg.get("t") != "manifest":
        log.warning("Bad handshake from remote: %s", remote_msg)
        return

    remote_manifest: dict[str, float] = remote_msg.get("files", {})
    log.info(
        "Handshake OK (%s↔%s) — local: %d files, remote: %d files",
        side, remote_msg.get("side", "?"), len(my_manifest), len(remote_manifest),
    )

    # ── Step 2: initial sync — push files where we are newer ──
    pushed = 0
    for rel, mtime in my_manifest.items():
        if side == "client" and is_db(rel):
            continue   # client never pushes DB
        if mtime > remote_manifest.get(rel, 0):
            try:
                await send_msg(writer, make_push_msg(root, rel))
                pushed += 1
                log.info("↑ (init) %s", rel)
            except OSError as e:
                log.warning("Skipping initial push of %s: %s", rel, e)

    log.info("Initial sync done — pushed %d file(s)", pushed)

    # ── Step 3: start watchdog ──
    obs = start_watcher(root, queue, loop)

    # Debounce pending pushes: rel → TimerHandle
    _pending: dict[str, asyncio.TimerHandle] = {}
    # Echo suppression: rel → monotonic time we last wrote this file remotely.
    # Watchdog fires when we write a received file; we must not echo it back.
    _received_at: dict[str, float] = {}
    ECHO_SUPPRESS_SECS = 2.0

    async def _do_push(rel: str) -> None:
        _pending.pop(rel, None)
        if not (root / rel).is_file():
            return
        if side == "client" and is_db(rel):
            return
        # Suppress watchdog echoes for files we just wrote from a remote push
        if time.monotonic() - _received_at.get(rel, 0.0) < ECHO_SUPPRESS_SECS:
            return
        try:
            await send_msg(writer, make_push_msg(root, rel))
            log.info("↑ %s", rel)
        except Exception:
            raise   # propagates to asyncio.gather → closes session

    def _schedule(rel: str, delay: float = 0.5) -> None:
        if rel in _pending:
            _pending[rel].cancel()
        # Default-arg trick captures rel by value at schedule time
        _pending[rel] = loop.call_later(
            delay, lambda r=rel: asyncio.create_task(_do_push(r))
        )

    DB_DEBOUNCE = 30.0   # throttle DB pushes — bot writes constantly

    # ── Coroutine 1: consume watchdog events, schedule pushes ──
    async def push_loop() -> None:
        while True:
            kind, rel = await queue.get()
            if kind == "delete":
                if side == "client" and is_db(rel):
                    continue
                await send_msg(writer, {"t": "delete", "path": rel})
                log.info("✕ (sent delete) %s", rel)
            else:
                delay = DB_DEBOUNCE if is_db(rel) else 0.5
                _schedule(rel, delay)

    # ── Coroutine 2: receive and apply messages from remote ──
    async def recv_loop() -> None:
        while True:
            msg = await recv_msg(reader)
            if msg is None:
                raise ConnectionError("remote closed or timed out")
            t = msg.get("t")

            if t == "push":
                rel = msg.get("path", "")
                if side == "server" and is_db(rel):
                    log.debug("Rejected DB push from client: %s", rel)
                    continue
                try:
                    if apply_push_msg(root, msg):
                        _received_at[rel] = time.monotonic()  # suppress echo
                        log.info("↓ %s", rel)
                except Exception as e:
                    log.warning("Failed to apply push %s: %s", rel, e)

            elif t == "delete":
                rel = msg.get("path", "")
                if side == "server" and is_db(rel):
                    continue
                p = root / rel
                if p.exists():
                    try:
                        p.unlink()
                        log.info("✕ %s", rel)
                    except OSError as e:
                        log.warning("Failed to delete %s: %s", rel, e)

            elif t == "ping":
                await send_msg(writer, {"t": "pong"})

            elif t == "pong":
                pass   # heartbeat acknowledged

            else:
                log.debug("Unknown message type: %r", t)

    # ── Coroutine 3: heartbeat (server side only) ──
    async def ping_loop() -> None:
        if side != "server":
            return
        while True:
            await asyncio.sleep(15)
            await send_msg(writer, {"t": "ping"})

    try:
        await asyncio.gather(recv_loop(), push_loop(), ping_loop())
    except (ConnectionError, OSError) as e:
        log.info("Session closed: %s", e)
    except Exception as e:
        log.warning("Session ended with error: %s", e)
    finally:
        obs.stop()
        obs.join()
        for handle in list(_pending.values()):
            handle.cancel()
        _pending.clear()


# ── Config loader ─────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg_path = Path(__file__).parent / "sync_config.json"
    with open(cfg_path) as f:
        return json.load(f)
