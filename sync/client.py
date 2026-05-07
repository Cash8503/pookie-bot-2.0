"""
client.py — Sync client. Runs on your local PC while editing in VS Code.

Connects to the AMP server (server.py) and keeps files in sync.
Automatically reconnects if the connection drops, with exponential backoff.

Usage (from the bot root or the sync/ directory):
    python sync/client.py

Tip — add a VS Code task so it starts automatically when you open the workspace:
    .vscode/tasks.json:
    {
      "version": "2.0.0",
      "tasks": [{
        "label": "Pookie Sync",
        "type": "shell",
        "command": "python sync/client.py",
        "runOptions": { "runOn": "folderOpen" },
        "presentation": { "reveal": "silent", "panel": "dedicated" }
      }]
    }
"""

import asyncio
import logging
import sys
from pathlib import Path

# Allow running as `python sync/client.py` from the bot root
sys.path.insert(0, str(Path(__file__).parent))

from sync_core import ROOT, load_config, log, run_session

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

_MAX_BACKOFF = 60   # seconds


async def main() -> None:
    cfg     = load_config()
    host    = cfg["host"]
    port    = cfg.get("sync_port", 2224)
    backoff = 2

    log.info("Sync client starting — target %s:%d", host, port)
    log.info("Bot root: %s", ROOT)
    log.info("Press Ctrl+C to stop.\n")

    while True:
        writer = None
        try:
            reader, writer = await asyncio.open_connection(host, port, limit=64 * 1024 * 1024)
            log.info("Connected to %s:%d", host, port)
            backoff = 2   # reset on successful connect
            await run_session(reader, writer, side="client", root=ROOT)

        except (ConnectionRefusedError, OSError) as e:
            log.info("Server not reachable (%s) — retrying in %ds", e, backoff)
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Stopping.")
            break
        except Exception as e:
            log.warning("Unexpected error (%s) — reconnecting in %ds", e, backoff)
        finally:
            if writer:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, _MAX_BACKOFF)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
