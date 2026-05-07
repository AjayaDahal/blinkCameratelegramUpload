#!/usr/bin/env python3
"""
Combined entrypoint: runs both the Telegram backup daemon and the web dashboard.
Shares a single Blink instance between them.
"""

import asyncio
import logging
import os
import sys
import time

from aiohttp import web
import web_dashboard
from blink2telegram import BlinkTelegramDaemon, setup_logging, load_config

# Track last battery alert time per camera (epoch)
_last_battery_alert: dict[str, float] = {}
BATTERY_ALERT_INTERVAL = 12 * 3600  # 12 hours


async def check_battery_alerts(uploader):
    """Send Telegram alert every 12h for cameras with low battery or offline status."""
    if not web_dashboard._blink:
        return
    now = time.time()
    for name, cam in web_dashboard._blink.cameras.items():
        health = web_dashboard.get_camera_health(cam)
        if health["status"] not in ("low_battery", "offline"):
            continue
        last_sent = _last_battery_alert.get(name, 0)
        if now - last_sent < BATTERY_ALERT_INTERVAL:
            continue
        # Build alert message
        voltage = health.get("battery_voltage", "?")
        status = health["status"].replace("_", " ").upper()
        reason = health.get("reason") or ""
        msg = (
            f"🔋 <b>Battery Alert: {name}</b>\n"
            f"Status: <b>{status}</b>\n"
            f"Battery: {health.get('battery', '?')} ({voltage}mV)\n"
        )
        if reason:
            msg += f"Reason: {reason}\n"
        msg += f"\nChecked at {time.strftime('%Y-%m-%d %H:%M:%S')}"
        try:
            await uploader.send_text(msg)
            _last_battery_alert[name] = now
            logging.getLogger(__name__).info(f"Battery alert sent for {name}: {status}")
        except Exception as e:
            logging.getLogger(__name__).error(f"Failed to send battery alert for {name}: {e}")


async def run_combined():
    config = load_config()
    setup_logging(config)

    logger = logging.getLogger(__name__)

    # Initialize Blink once (shared between web + daemon)
    await web_dashboard.init_blink(config)
    logger.info("Blink initialized")

    # Start web server first (don't block on snapshots)
    app = web.Application()
    app.router.add_get("/", web_dashboard.handle_index)
    app.router.add_get("/snapshot/ai/{name}", web_dashboard.handle_snapshot_ai)
    app.router.add_get("/snapshot/heatmap/{name}", web_dashboard.handle_snapshot_heatmap)
    app.router.add_get("/snapshot/{name}", web_dashboard.handle_snapshot)
    app.router.add_post("/api/refresh", web_dashboard.handle_api_refresh)
    app.router.add_get("/api/status", web_dashboard.handle_api_status)
    app.router.add_get("/api/dashboard", web_dashboard.handle_api_dashboard)
    app.router.add_get("/clips/{filename}", web_dashboard.handle_clips_file)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("WEB_PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Web dashboard running on http://0.0.0.0:{port}")

    # Take initial snapshots in background (non-blocking)
    asyncio.create_task(web_dashboard.refresh_snapshots())

    # Start Telegram daemon if credentials are configured
    has_telegram = config.get("telegram_bot_token") and config.get("telegram_chat_id")

    if has_telegram:
        logger.info("Telegram bot configured - starting backup daemon")
        daemon = BlinkTelegramDaemon(config)
        # Share the already-authenticated blink instance
        daemon.blink = web_dashboard._blink
        daemon.running = True

        try:
            # Run daemon polling loop (authenticate already done via shared blink)
            poll_interval = config["poll_interval_seconds"]
            cycle = 0
            while daemon.running:
                cycle += 1
                logger.info(f"--- Poll cycle {cycle} ---")
                try:
                    await daemon.fetch_cloud_clips()
                except Exception as e:
                    logger.error(f"Cloud sync error: {e}")
                try:
                    await daemon.fetch_local_storage_clips()
                except Exception as e:
                    logger.error(f"Local storage sync error: {e}")
                # Battery health check — alert every 12h
                try:
                    await check_battery_alerts(daemon.uploader)
                except Exception as e:
                    logger.error(f"Battery alert error: {e}")
                if cycle % 10 == 0:
                    try:
                        await daemon.blink.save(config["credentials_file"])
                    except Exception:
                        pass
                await asyncio.sleep(poll_interval)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await daemon.shutdown()
        finally:
            await runner.cleanup()
    else:
        logger.info("No Telegram config - running web dashboard only (live view mode)")
        logger.info("Set telegram_bot_token + telegram_chat_id in config.json to enable backups")
        try:
            while True:
                await asyncio.sleep(3600)
        except (KeyboardInterrupt, asyncio.CancelledError):
            await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(run_combined())
