"""
sync_watcher.py — SFTP file sync watcher

Rules:
  local → remote : everything except venv dirs, excluded files, and the DB
  remote → local : data/data.db only (polled on an interval)
  no sync        : venv, __pycache__, .git, and anything in exclude_dirs/exclude_files

Usage:
  cd sync
  pip install -r requirements.txt
  python sync_watcher.py
"""

import json
import logging
import os
import threading
import time
from pathlib import Path, PurePosixPath

import paramiko
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SYNC_DIR = Path(__file__).parent
PROJECT_ROOT = SYNC_DIR.parent
CONFIG_PATH = SYNC_DIR / "sync_config.json"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)

# ---------------------------------------------------------------------------
# SFTP connection
# ---------------------------------------------------------------------------

def make_connection(cfg: dict) -> tuple:
    """Return (ssh, sftp). Raises on failure."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {
        "hostname": cfg["host"],
        "port": cfg.get("port", 22),
        "username": cfg["username"],
    }

    key_path = cfg.get("ssh_key_path")
    password = cfg.get("password")

    if key_path:
        kwargs["key_filename"] = os.path.expanduser(key_path)
    elif password:
        kwargs["password"] = password

    ssh.connect(**kwargs)
    sftp = ssh.open_sftp()
    return ssh, sftp

# ---------------------------------------------------------------------------
# Connection manager (auto-reconnect)
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._ssh = None
        self._sftp = None
        self._lock = threading.Lock()

    def get(self) -> paramiko.SFTPClient | None:
        with self._lock:
            if self._sftp is not None:
                return self._sftp
            try:
                self._ssh, self._sftp = make_connection(self.cfg)
                log.info("Connected to %s", self.cfg["host"])
                return self._sftp
            except Exception as e:
                log.error("Connection failed: %s", e)
                return None

    def invalidate(self):
        """Drop the current connection so the next call to get() reconnects."""
        with self._lock:
            try:
                if self._ssh:
                    self._ssh.close()
            except Exception:
                pass
            self._ssh = None
            self._sftp = None

    def close(self):
        self.invalidate()

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def to_remote_path(local: Path, cfg: dict) -> str:
    rel = local.relative_to(PROJECT_ROOT)
    return str(PurePosixPath(cfg["remote_root"]) / PurePosixPath(*rel.parts))


def is_db(path: Path, cfg: dict) -> bool:
    return path.resolve() == (PROJECT_ROOT / cfg.get("db_path", "data/data.db")).resolve()


# SQLite auxiliary files must never be synced — deleting them on the remote
# server while the bot is running can corrupt the live database.
_SQLITE_AUX_SUFFIXES = ("-wal", "-shm", "-journal")


def should_exclude(path: Path, cfg: dict) -> bool:
    exclude_dirs = set(cfg.get("exclude_dirs", []))
    exclude_files = set(cfg.get("exclude_files", []))

    try:
        rel = path.relative_to(PROJECT_ROOT)
    except ValueError:
        return True  # outside project root — skip

    # skip if any directory component is excluded
    for part in rel.parts[:-1]:
        if part in exclude_dirs:
            return True

    # always skip SQLite WAL/journal auxiliary files
    if any(path.name.endswith(s) for s in _SQLITE_AUX_SUFFIXES):
        return True

    # skip if the file itself is excluded by name
    if path.name in exclude_files:
        return True

    return False

# ---------------------------------------------------------------------------
# SFTP operations
# ---------------------------------------------------------------------------

def sftp_makedirs(sftp: paramiko.SFTPClient, remote_dir: str):
    """Recursively create remote directories, skipping ones that exist."""
    parts = remote_dir.split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = current.rstrip("/") + "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


def upload(sftp: paramiko.SFTPClient, local: Path, remote: str):
    sftp_makedirs(sftp, str(PurePosixPath(remote).parent))
    sftp.put(str(local), remote)
    log.info("↑ %s", remote)


def delete_remote(sftp: paramiko.SFTPClient, remote: str):
    try:
        sftp.remove(remote)
        log.info("✕ %s", remote)
    except FileNotFoundError:
        pass

# ---------------------------------------------------------------------------
# File event handler
# ---------------------------------------------------------------------------

class SyncHandler(FileSystemEventHandler):
    def __init__(self, cfg: dict, conn: ConnectionManager):
        self.cfg = cfg
        self.conn = conn

    def _upload(self, src: str):
        path = Path(src)
        if not path.is_file():
            return
        if should_exclude(path, self.cfg):
            return
        if is_db(path, self.cfg):
            return  # DB is remote → local only

        remote = to_remote_path(path, self.cfg)
        self._run(upload, path, remote)

    def _delete(self, src: str):
        path = Path(src)
        if should_exclude(path, self.cfg):
            return
        if is_db(path, self.cfg):
            return

        remote = to_remote_path(path, self.cfg)
        self._run(delete_remote, remote)

    def _run(self, fn, *args):
        sftp = self.conn.get()
        if sftp is None:
            return
        try:
            fn(sftp, *args)
        except OSError as e:
            log.warning("SFTP error (%s) — will reconnect", e)
            self.conn.invalidate()

    def on_modified(self, event):
        if not event.is_directory:
            self._upload(event.src_path)

    def on_created(self, event):
        if not event.is_directory:
            self._upload(event.src_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self._delete(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._delete(event.src_path)
            self._upload(event.dest_path)

# ---------------------------------------------------------------------------
# DB poller (remote → local)
# ---------------------------------------------------------------------------

def db_poll_loop(cfg: dict, conn: ConnectionManager):
    interval = cfg.get("db_poll_interval", 60)
    db_rel = cfg.get("db_path", "data/data.db")
    local_db = PROJECT_ROOT / db_rel
    remote_db = str(PurePosixPath(cfg["remote_root"]) / PurePosixPath(*Path(db_rel).parts))

    log.info("DB poller started — pulling %s every %ds", remote_db, interval)

    while True:
        time.sleep(interval)
        sftp = conn.get()
        if sftp is None:
            continue
        try:
            local_db.parent.mkdir(parents=True, exist_ok=True)
            sftp.get(remote_db, str(local_db))
            log.info("↓ %s", remote_db)
        except FileNotFoundError:
            log.warning("Remote DB not found at %s — skipping poll", remote_db)
        except OSError as e:
            log.warning("DB poll error (%s) — will reconnect", e)
            conn.invalidate()

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = load_config()
    conn = ConnectionManager(cfg)

    # Initial connection attempt
    conn.get()

    # DB poller thread (remote → local)
    threading.Thread(target=db_poll_loop, args=(cfg, conn), daemon=True).start()

    # File watcher (local → remote)
    handler = SyncHandler(cfg, conn)
    observer = Observer()
    observer.schedule(handler, str(PROJECT_ROOT), recursive=True)
    observer.start()

    log.info("Watching %s", PROJECT_ROOT)
    log.info("Remote root: %s@%s:%s", cfg["username"], cfg["host"], cfg["remote_root"])
    log.info("Excluded dirs: %s", cfg.get("exclude_dirs", []))
    log.info("DB sync: remote → local every %ds", cfg.get("db_poll_interval", 60))
    log.info("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Stopping...")
        observer.stop()

    observer.join()
    conn.close()
    log.info("Goodbye.")


if __name__ == "__main__":
    main()
