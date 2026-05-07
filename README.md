# 🧹 Discord Link Cleaner Bot

Automatically strips tracking parameters, referral codes, and ad junk from URLs posted in your Discord server.

## What it strips

| Category | Examples |
|---|---|
| UTM params | `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` |
| Facebook | `fbclid`, `fb_action_ids`, `fb_ref`, `fb_source`, `mibextid` |
| Google | `gclid`, `gclsrc`, `dclid`, `_ga` |
| Microsoft / Bing | `msclkid`, `ocid` |
| Amazon | `tag`, `ref`, `pf_rd_*`, `pd_rd_*`, `smid`, `sprefix`, `linkId` |
| YouTube | `si` (share tracking) |
| TikTok | `_r`, `_t`, `refer`, `share_app_id`, `share_link_id` |
| Spotify | `si`, `context`, `nd` |
| Twitter / X | `twclid` |
| Mailchimp | `mc_cid`, `mc_eid` |
| Generic | `ref`, `source`, `affiliate`, `partner`, `promo`, `tracking_id`, `trk`, ... |

## Setup

### 1. Create a Discord Application & Bot

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application** → give it a name
3. Go to **Bot** → click **Add Bot**
4. Under **Privileged Gateway Intents**, enable **Message Content Intent**
5. Copy your bot token

### 2. Invite the Bot

In the Developer Portal, go to **OAuth2 → URL Generator**:
- Scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Read Message History`, `Embed Links`

Open the generated URL and invite the bot to your server.

### 3. Install & Run

```bash
# Clone / download the project
cd discord-link-cleaner

# Create a virtual environment (recommended)
python -m venv .venv
source .venv/bin/activate      # Linux/macOS
# .venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt

# Set up environment
cp .env.example .env
# Edit .env and paste your bot token

# Run the bot
python bot.py
```

## Slash Commands

All commands require **Manage Server** permission.

| Command | Description |
|---|---|
| `/linkclean toggle` | Enable or disable the cleaner for this server |
| `/linkclean ignore` | Toggle ignoring the current channel |
| `/linkclean status` | Show current settings |
| `/linkclean test <url>` | Preview what a URL looks like after cleaning |

> **Note:** Slash commands sync automatically on first startup. If they don't appear, wait up to an hour for Discord to propagate them, or add a `await bot.tree.sync()` call in `on_ready` for instant global sync (can be rate-limited).

## Project Structure

```
discord-link-cleaner/
├── bot.py                  # Entry point, bot setup
├── cogs/
│   ├── __init__.py
│   └── link_cleaner.py     # All the URL cleaning logic + event listener + slash commands
├── requirements.txt
├── .env.example
└── README.md
```

## Persistence

By default, server settings (enabled/disabled, ignored channels) are stored **in memory** and will reset on bot restart. To make them persistent, replace the `_guild_settings` dict in `link_cleaner.py` with a SQLite/JSON backend — the `get_settings()` function is the only integration point you'd need to change.

## Running as a Service (Linux)

```ini
# /etc/systemd/system/link-cleaner-bot.service
[Unit]
Description=Discord Link Cleaner Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/path/to/discord-link-cleaner
ExecStart=/path/to/discord-link-cleaner/.venv/bin/python bot.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now link-cleaner-bot
```
