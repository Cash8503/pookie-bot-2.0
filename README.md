# Pookie Bot 2.0

A modular Discord bot with hybrid prefix/slash commands, server-specific plugin controls, locally generated help, AI features, games, utilities, activity tracking, and self-hosted voice music.

## Setup

1. Install Python 3.11 or newer and Node.js.
2. Create and activate a virtual environment.
3. Install runtime dependencies:

   ```powershell
   python -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`.
5. Enable Message Content and Server Members intents in the Discord Developer Portal.
6. Invite the bot with the `bot` and `applications.commands` scopes. Music also needs Connect and Speak permissions.
7. Start the bot:

   ```powershell
   python bot.py
   ```

`imageio-ffmpeg` supplies a local FFmpeg binary by default. Set `FFMPEG_PATH` only when you want to use another FFmpeg executable.

## Help system

Help is generated from documentation stored beside each command in its plugin.

- `!help` — available plugin and command overview
- `!help music` — plugin or command-group overview
- `!help music play` — full usage, arguments, examples, and notes
- `/help` — the same system through Discord slash commands

Owner-only, unavailable, and server-disabled commands are filtered from normal help results.

## Music

Use `!music play <search or URL>` or `/music play` while connected to a voice channel. The bot joins automatically, queues the result, and posts an interactive now-playing embed.

Supported inputs include:

- YouTube videos and playlists
- Spotify tracks, albums, and public playlists
- SoundCloud, Bandcamp, Vimeo, Twitch, Mixcloud, and Dailymotion links supported by yt-dlp
- Plain song searches

Spotify links provide metadata only. The bot matches each item to a playable YouTube source; it does not download or decrypt Spotify audio.

Controls and commands include pause/resume, skip, stop, disconnect, shuffle, track/queue looping, queue removal, clearing, and volume. Run `!music diagnostics` to check voice encryption, yt-dlp, Spotify metadata support, FFmpeg, and optional cookies.

Music settings in `.env`:

| Setting | Default | Purpose |
|---|---:|---|
| `MUSIC_DEFAULT_VOLUME` | `50` | Initial session volume, 0-100 |
| `MUSIC_MAX_PLAYLIST` | `100` | Maximum entries accepted from one playlist |
| `MUSIC_IMPORT_WORKERS` | `6` | Concurrent Spotify collection page requests, 2-16 |
| `MUSIC_IDLE_TIMEOUT` | `180` | Seconds before an empty player disconnects |
| `FFMPEG_PATH` | blank | Optional explicit FFmpeg executable |
| `YTDLP_COOKIE_FILE` | blank | Optional private Netscape-format cookies file |

## Plugin layout

- `bot.py` — startup, persistence, plugin discovery, checks, and shared errors
- `cogs/` — independently reloadable Discord plugins
- `cogs/_help.py` — docstring parser and help renderer
- `music/` — source resolution, queue/player state, and embed controls
- `data/` — runtime data and SQLite settings
- `tests/` — help and music regression checks

Files whose names start with `_` are disabled plugins or internal helpers and are skipped during normal startup.

## Validation

Install development requirements and run:

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

The suite verifies that every plugin and command has local documentation, slash metadata is generated correctly, unsafe media URLs are rejected, queue operations are isolated, and FFmpeg is available.
