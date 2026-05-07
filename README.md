# Blink Camera → Telegram Backup (No Subscription Required)

## Network Discovery

| IP | Device | MAC Vendor |
|----|--------|------------|
| 192.168.1.130 | **Blink Sync Module 2** | Amazon Technologies (ports 1080, 8888) |
| 192.168.1.252 | **Blink Camera** | Amazon Technologies |
| 192.168.1.185 | w7900 Desktop (ai-is-taking-over) | Cloud Network Technology |
| 192.168.1.188 | This laptop (HP EliteBook) | - |
| 192.168.1.197 | Raspberry Pi (simplypi) | Raspberry Pi Foundation |

## How It Works

```
┌─────────────┐    motion    ┌──────────────┐   USB    ┌──────────┐
│ Blink Camera├─────────────►│ Sync Module 2├─────────►│ USB Drive│
│ .252        │   wifi       │ .130         │  local   │ (clips)  │
└─────────────┘              └──────┬───────┘  storage └──────────┘
                                    │
                                    │ Blink Cloud API
                                    ▼
                             ┌──────────────┐
                             │ Blink Servers│  (free tier - clips available
                             │ (cloud)      │   via API even w/o subscription)
                             └──────┬───────┘
                                    │
         ┌──────────────────────────┤
         │  blinkpy polls every 60s │
         ▼                          ▼
┌─────────────────┐    ┌────────────────────┐
│ This Laptop     │    │ Local Storage API   │
│ or w7900 Desktop│◄───┤ (USB drive manifest)│
│ (daemon)        │    └────────────────────┘
└────────┬────────┘
         │ python-telegram-bot
         ▼
┌─────────────────┐
│ Telegram Channel│  ← Permanent cloud backup!
│ (unlimited free │    No subscription needed.
│  storage)       │    Telegram = ∞ storage.
└─────────────────┘
```

## The "Outside the Box" Trick

1. **Blink's free tier** still records motion clips and stores them temporarily in the cloud + on the Sync Module 2's USB drive
2. **blinkpy** (open source) talks to the same API the Blink app uses — no subscription check
3. **Local Storage API** lets us pull clips directly from the USB drive before they get overwritten
4. **Telegram channels** = unlimited free cloud storage for videos (up to 2GB per file)
5. The daemon runs 24/7 on your always-on machine, polling every 60s

Result: **Free permanent cloud backup of all Blink motion clips.**

## Quick Start

```bash
cd ~/Project/BLINK\ CAMERA
source venv/bin/activate

# 1. Edit config.json with your Blink email/password + Telegram bot token + chat ID
nano config.json

# 2. Run interactive setup (handles 2FA, tests Telegram)
python setup.py

# 3. Start the daemon
python blink2telegram.py

# 4. (Optional) Install as systemd service for auto-start
sudo cp blink2telegram.service /etc/systemd/system/
sudo systemctl enable --now blink2telegram
sudo journalctl -u blink2telegram -f  # view logs
```

## Telegram Bot Setup

1. Open Telegram → search **@BotFather** → `/newbot` → follow prompts → copy token
2. Create a **Channel** (or group) in Telegram
3. Add your bot as **admin** of the channel
4. Get channel ID: visit `https://api.telegram.org/bot<TOKEN>/getUpdates` after sending a message

## Files

- `config.json` — credentials & settings
- `blink2telegram.py` — main daemon
- `setup.py` — interactive first-time setup
- `blink2telegram.service` — systemd unit for auto-start
- `clips/` — local cache of downloaded clips (auto-rotated)
- `uploaded_clips.db` — SQLite tracker to avoid duplicate uploads
- `blink_creds.json` — saved auth tokens (auto-generated)
