#!/usr/bin/env python3
"""
Blink Camera → Telegram Channel Backup Daemon

Polls Blink cameras for new motion clips (cloud + local storage) and
uploads them to a Telegram channel. No Blink subscription required.

Network map:
  192.168.1.130 = Blink Sync Module 2 (Amazon, ports 1080/8888)
  192.168.1.252 = Blink Camera (Amazon)

Strategy:
  1. Authenticate via blinkpy (Blink cloud API - works without subscription)
  2. Poll for new motion clips every N seconds
  3. Also poll local storage manifest (USB on sync module)
  4. Download new clips to local disk
  5. Upload to Telegram channel/group via bot API
  6. Track uploaded clip IDs in SQLite to avoid duplicates
  7. Rotate local clips when storage exceeds limit
"""

import asyncio
import json
import logging
import os
import signal
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import aiohttp
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load
import telegram


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path: str = None) -> dict:
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        config = json.load(f)

    # Environment variables override config file values
    env_map = {
        "BLINK_USERNAME": "blink_username",
        "BLINK_PASSWORD": "blink_password",
        "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
        "TELEGRAM_CHAT_ID": "telegram_chat_id",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key)
        if val:
            config[cfg_key] = val

    return config


# ---------------------------------------------------------------------------
# SQLite tracker - remember what we already uploaded
# ---------------------------------------------------------------------------

class ClipTracker:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS uploaded_clips (
                clip_id   TEXT PRIMARY KEY,
                camera    TEXT,
                timestamp TEXT,
                filepath  TEXT,
                tg_msg_id TEXT,
                uploaded_at TEXT DEFAULT (datetime('now'))
            )
        """)
        self.conn.commit()

    def is_uploaded(self, clip_id: str) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM uploaded_clips WHERE clip_id = ?", (clip_id,)
        )
        return cur.fetchone() is not None

    def mark_uploaded(self, clip_id: str, camera: str, timestamp: str,
                      filepath: str, tg_msg_id: str):
        self.conn.execute(
            "INSERT OR IGNORE INTO uploaded_clips "
            "(clip_id, camera, timestamp, filepath, tg_msg_id) VALUES (?,?,?,?,?)",
            (clip_id, camera, timestamp, filepath, tg_msg_id),
        )
        self.conn.commit()

    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM uploaded_clips").fetchone()[0]

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Telegram uploader
# ---------------------------------------------------------------------------

class TelegramUploader:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot = telegram.Bot(token=bot_token)
        self.chat_id = chat_id

    async def send_video(self, filepath: str, caption: str) -> str:
        """Upload video clip to Telegram. Returns message ID."""
        with open(filepath, "rb") as f:
            msg = await self.bot.send_video(
                chat_id=self.chat_id,
                video=f,
                caption=caption[:1024],  # Telegram caption limit
                read_timeout=120,
                write_timeout=120,
                connect_timeout=30,
            )
        return str(msg.message_id)

    async def send_photo(self, filepath: str, caption: str) -> str:
        with open(filepath, "rb") as f:
            msg = await self.bot.send_photo(
                chat_id=self.chat_id,
                photo=f,
                caption=caption[:1024],
            )
        return str(msg.message_id)

    async def send_text(self, text: str):
        await self.bot.send_message(
            chat_id=self.chat_id,
            text=text[:4096],
            parse_mode="HTML",
        )


# ---------------------------------------------------------------------------
# Local storage cleanup
# ---------------------------------------------------------------------------

def cleanup_local_clips(clip_dir: str, max_mb: int):
    """Delete oldest clips when local storage exceeds limit."""
    clips = sorted(Path(clip_dir).glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in clips)
    limit = max_mb * 1024 * 1024

    while total > limit and clips:
        oldest = clips.pop(0)
        total -= oldest.stat().st_size
        oldest.unlink()
        logging.info(f"Deleted old clip: {oldest.name}")


# ---------------------------------------------------------------------------
# Main daemon
# ---------------------------------------------------------------------------

class BlinkTelegramDaemon:
    def __init__(self, config: dict):
        self.config = config
        self.blink: Blink | None = None
        self.tracker = ClipTracker(config["db_file"])
        self.uploader = TelegramUploader(
            config["telegram_bot_token"], config["telegram_chat_id"]
        )
        self.clip_dir = config["clip_download_path"]
        self.running = True
        os.makedirs(self.clip_dir, exist_ok=True)

    async def authenticate(self):
        """Authenticate with Blink API. Reuses saved credentials if available."""
        creds_file = self.config["credentials_file"]

        self.blink = Blink()

        if os.path.exists(creds_file):
            logging.info("Loading saved credentials...")
            auth = Auth(await json_load(creds_file), no_prompt=True)
        else:
            logging.info("First-time login - will prompt for credentials")
            auth = Auth(
                {
                    "username": self.config["blink_username"],
                    "password": self.config["blink_password"],
                },
                no_prompt=False,
            )

        self.blink.auth = auth

        try:
            await self.blink.start()
        except Exception as e:
            if "2FA" in str(type(e).__name__) or "2fa" in str(e).lower():
                logging.info("2FA required - check your email for the PIN")
                pin = input("Enter 2FA PIN from email: ").strip()
                result = await self.blink.send_2fa_code(pin)
                if not result:
                    raise RuntimeError("2FA verification failed")
            else:
                raise

        # Save credentials for future sessions (tokens, no password stored)
        await self.blink.save(creds_file)
        logging.info(f"Authenticated. Found {len(self.blink.cameras)} camera(s)")

        for name, cam in self.blink.cameras.items():
            logging.info(f"  Camera: {name} | Armed: {cam.arm}")

    async def fetch_cloud_clips(self):
        """Download new motion clips from Blink cloud (free tier)."""
        if not self.config.get("enable_cloud_sync", True):
            return

        try:
            await self.blink.refresh()
        except Exception as e:
            logging.warning(f"Cloud refresh failed: {e}")
            return

        for name, camera in self.blink.cameras.items():
            # Check for new motion clip
            clip_url = camera.clip
            if not clip_url:
                continue

            # Use URL as clip ID for dedup
            clip_id = f"cloud_{clip_url}"
            if self.tracker.is_uploaded(clip_id):
                continue

            logging.info(f"New cloud clip from {name}: {clip_url}")

            # Download video
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_name = name.replace(" ", "_").replace("/", "_")
            filename = f"{safe_name}_{ts}.mp4"
            filepath = os.path.join(self.clip_dir, filename)

            try:
                await camera.video_to_file(filepath)
            except Exception as e:
                logging.error(f"Failed to download clip from {name}: {e}")
                continue

            if not os.path.exists(filepath) or os.path.getsize(filepath) < 1000:
                logging.warning(f"Clip file too small or missing: {filepath}")
                continue

            # Upload to Telegram
            size_mb = os.path.getsize(filepath) / (1024 * 1024)
            caption = (
                f"📹 <b>{name}</b>\n"
                f"🕐 {ts}\n"
                f"📦 {size_mb:.1f} MB | Cloud clip"
            )
            try:
                msg_id = await self.uploader.send_video(filepath, caption)
                self.tracker.mark_uploaded(clip_id, name, ts, filepath, msg_id)
                logging.info(f"Uploaded to Telegram: {filename} (msg {msg_id})")
            except Exception as e:
                logging.error(f"Telegram upload failed: {e}")

            # Also send thumbnail
            if self.config.get("send_thumbnail", True):
                try:
                    thumb_path = filepath.replace(".mp4", ".jpg")
                    await camera.image_to_file(thumb_path)
                    if os.path.exists(thumb_path):
                        await self.uploader.send_photo(
                            thumb_path, f"📷 Thumbnail: {name}"
                        )
                except Exception:
                    pass

    async def fetch_local_storage_clips(self):
        """
        Download clips from Blink Sync Module 2 local storage (USB drive).
        This is the key "outside the box" move - accesses clips stored on the
        USB drive plugged into the sync module, which would otherwise be
        overwritten when full.
        """
        if not self.config.get("enable_local_storage_sync", True):
            return

        for sync_name, sync_module in self.blink.sync.items():
            try:
                # Request the local storage manifest
                manifest = None
                if hasattr(sync_module, "local_storage") and sync_module.local_storage:
                    ls = sync_module.local_storage
                    if hasattr(ls, "manifest") and ls.manifest:
                        manifest = ls.manifest
                    else:
                        # Try to refresh local storage
                        try:
                            await ls.refresh()
                            manifest = ls.manifest if hasattr(ls, "manifest") else None
                        except Exception as e:
                            logging.debug(f"Local storage refresh failed for {sync_name}: {e}")
                            continue
                else:
                    logging.debug(f"No local storage on sync module: {sync_name}")
                    continue

                if not manifest:
                    logging.debug(f"No manifest available for {sync_name}")
                    continue

                # Process clips from manifest
                clips = []
                if isinstance(manifest, dict) and "clips" in manifest:
                    clips = manifest["clips"]
                elif isinstance(manifest, list):
                    clips = manifest

                for clip_info in clips:
                    clip_id = f"local_{clip_info.get('id', clip_info.get('clip_id', ''))}"
                    if not clip_id or clip_id == "local_":
                        continue
                    if self.tracker.is_uploaded(clip_id):
                        continue

                    camera_name = clip_info.get("camera_name", "unknown")
                    created = clip_info.get("created_at", "")
                    logging.info(f"New local clip: {clip_id} from {camera_name}")

                    # Download via blinkpy local storage API
                    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                    safe_name = camera_name.replace(" ", "_")
                    filename = f"local_{safe_name}_{ts}.mp4"
                    filepath = os.path.join(self.clip_dir, filename)

                    try:
                        raw_clip_id = clip_info.get("id", clip_info.get("clip_id"))
                        manifest_id = manifest.get("manifest_id") if isinstance(manifest, dict) else None

                        if hasattr(ls, "download_clip"):
                            await ls.download_clip(filepath, raw_clip_id, manifest_id)
                        elif hasattr(ls, "get_clip"):
                            data = await ls.get_clip(raw_clip_id)
                            if data:
                                with open(filepath, "wb") as f:
                                    f.write(data)
                        else:
                            logging.warning("No download method found on local storage")
                            continue
                    except Exception as e:
                        logging.error(f"Failed to download local clip {clip_id}: {e}")
                        continue

                    if not os.path.exists(filepath) or os.path.getsize(filepath) < 1000:
                        continue

                    size_mb = os.path.getsize(filepath) / (1024 * 1024)
                    caption = (
                        f"📹 <b>{camera_name}</b>\n"
                        f"🕐 {created or ts}\n"
                        f"💾 {size_mb:.1f} MB | Local storage clip\n"
                        f"🔗 Sync: {sync_name}"
                    )
                    try:
                        msg_id = await self.uploader.send_video(filepath, caption)
                        self.tracker.mark_uploaded(
                            clip_id, camera_name, created or ts, filepath, msg_id
                        )
                        logging.info(f"Local clip uploaded: {filename}")
                    except Exception as e:
                        logging.error(f"Telegram upload failed for local clip: {e}")

            except Exception as e:
                logging.error(f"Local storage error for {sync_name}: {e}")

    async def run(self):
        """Main polling loop."""
        await self.authenticate()

        startup_msg = (
            f"🟢 <b>Blink→Telegram daemon started</b>\n"
            f"📷 Cameras: {', '.join(self.blink.cameras.keys())}\n"
            f"⏱ Poll interval: {self.config['poll_interval_seconds']}s\n"
            f"☁️ Cloud sync: {self.config.get('enable_cloud_sync', True)}\n"
            f"💾 Local storage sync: {self.config.get('enable_local_storage_sync', True)}\n"
            f"📊 Previously uploaded: {self.tracker.count()} clips"
        )
        try:
            await self.uploader.send_text(startup_msg)
        except Exception as e:
            logging.warning(f"Could not send startup message: {e}")

        poll_interval = self.config["poll_interval_seconds"]
        cycle = 0

        while self.running:
            cycle += 1
            logging.info(f"--- Poll cycle {cycle} ---")

            try:
                await self.fetch_cloud_clips()
            except Exception as e:
                logging.error(f"Cloud sync error: {e}")

            try:
                await self.fetch_local_storage_clips()
            except Exception as e:
                logging.error(f"Local storage sync error: {e}")

            # Cleanup old local files
            try:
                cleanup_local_clips(
                    self.clip_dir, self.config.get("max_local_clips_mb", 500)
                )
            except Exception as e:
                logging.warning(f"Cleanup error: {e}")

            # Re-save credentials (tokens may have refreshed)
            if cycle % 10 == 0:
                try:
                    await self.blink.save(self.config["credentials_file"])
                except Exception:
                    pass

            await asyncio.sleep(poll_interval)

    async def shutdown(self):
        self.running = False
        self.tracker.close()
        try:
            await self.uploader.send_text("🔴 <b>Blink→Telegram daemon stopped</b>")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def setup_logging(config: dict):
    log_file = config.get("log_file")
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )


async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else None
    config = load_config(config_path)
    setup_logging(config)

    if not config.get("blink_username") or not config.get("telegram_bot_token"):
        creds_file = config.get("credentials_file", "")
        if not os.path.exists(creds_file) and not config.get("blink_username"):
            logging.error(
                "Fill in blink_username, blink_password, telegram_bot_token, "
                "and telegram_chat_id in config.json"
            )
            sys.exit(1)

    daemon = BlinkTelegramDaemon(config)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: asyncio.create_task(daemon.shutdown()))

    try:
        await daemon.run()
    except KeyboardInterrupt:
        await daemon.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
