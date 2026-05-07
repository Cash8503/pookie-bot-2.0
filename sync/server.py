"""
server.py — Sync server. Runs on the AMP machine alongside the bot.

Start it once and leave it running. It listens for a connection from the
local PC (client.py) and keeps files in sync bidirectionally.

Usage (from the bot root or the sync/ directory):
    python sync/server.py

DB files (data/data.db) only flow server → client. Everything else is
bidirectional; newest mtime always wins.
"""

import asyncio
import logging
import sys
from pathlib import Path

# Allow running as `python sync/server.py` from the bot root
sys.path.insert(0, str(Path(__file__).parent))

from sync_core import ROOT, load_config, log, run_session

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)


async def main() -> None:
    cfg  = load_config()
    port = cfg.get("sync_port", 2224)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        addr = writer.get_extra_info("peername")
        log.info("Client connected from %s", addr)
        try:
            await run_session(reader, writer, side="server", root=ROOT)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            log.info("Client disconnected (%s)", addr)

    server = await asyncio.start_server(handle, "0.0.0.0", port)
    addrs  = [s.getsockname() for s in server.sockets]
    log.info("Sync server listening on port %d", port)
    log.info("Bot root: %s", ROOT)
    log.info("Press Ctrl+C to stop.\n")

    async with server:
        try:
            await server.serve_forever()
        except (KeyboardInterrupt, asyncio.CancelledError):
            log.info("Shutting down.")


if __name__ == "__main__":
    asyncio.run(main())
