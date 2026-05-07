#!/usr/bin/env python3
"""
Blink Camera Live View + Dashboard Web App

Simple aiohttp web server that:
  - Shows live snapshots from all Blink cameras (refreshable)
  - Displays recent motion clips
  - Shows daemon status
  - Runs alongside the Telegram backup daemon
"""

import asyncio
import base64
import json
import logging
import os
import glob
import re
import time
from datetime import datetime

from aiohttp import web
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load

import ai_processor

logger = logging.getLogger(__name__)

# Shared state
_blink: Blink | None = None
_config: dict = {}
_snapshot_cache: dict[str, bytes] = {}
_annotated_cache: dict[str, bytes] = {}
_heatmap_cache: dict[str, bytes] = {}
_detection_results: dict[str, dict] = {}
_last_refresh: str = "never"


def load_config(path=None):
    if path is None:
        path = os.path.join(os.path.dirname(__file__), "config.json")
    with open(path) as f:
        config = json.load(f)

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


async def init_blink(config):
    global _blink
    _blink = Blink()
    creds_file = config["credentials_file"]

    if os.path.exists(creds_file):
        auth = Auth(await json_load(creds_file), no_prompt=True)
    else:
        auth = Auth(
            {"username": config["blink_username"], "password": config["blink_password"]},
            no_prompt=False,
        )

    _blink.auth = auth

    try:
        await _blink.start()
    except Exception as e:
        if "2FA" in str(type(e).__name__):
            pin = input("Enter 2FA PIN: ").strip()
            await _blink.send_2fa_code(pin)
        else:
            raise

    await _blink.save(creds_file)
    logger.info(f"Blink ready: {len(_blink.cameras)} cameras")


async def refresh_snapshots(snap=False):
    """Refresh cached snapshots. Only wakes cameras if snap=True (battery drain!)."""
    global _last_refresh, _snapshot_cache
    if not _blink:
        return

    if snap:
        # WARNING: This wakes cameras and drains battery!
        for name, camera in _blink.cameras.items():
            try:
                await camera.snap_picture()
            except Exception as e:
                logger.warning(f"Snap failed for {name}: {e}")
        await asyncio.sleep(5)  # give cameras time to upload

    try:
        await _blink.refresh()
    except Exception as e:
        logger.warning(f"Refresh failed: {e}")
        return

    for name, camera in _blink.cameras.items():
        if camera.image_from_cache:
            _snapshot_cache[name] = camera.image_from_cache
            # Run AI detection
            try:
                result = ai_processor.detect_objects(camera.image_from_cache, name)
                _annotated_cache[name] = result["annotated_image"]
                _detection_results[name] = result
            except Exception as e:
                logger.warning(f"AI detection failed for {name}: {e}")
            # Generate motion heatmap
            try:
                heatmap = ai_processor.generate_motion_heatmap(camera.image_from_cache, name)
                if heatmap:
                    _heatmap_cache[name] = heatmap
            except Exception as e:
                logger.warning(f"Heatmap failed for {name}: {e}")

    _last_refresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_camera_health(camera) -> dict:
    """Detect if camera is likely offline/dead by checking thumbnail staleness."""
    thumb = getattr(camera, "thumbnail", "") or ""
    # Extract ts= param from thumbnail URL
    ts_match = re.search(r'[?&]ts=(\d+)', thumb)
    if ts_match:
        thumb_ts = int(ts_match.group(1))
        age_days = (time.time() - thumb_ts) / 86400
    else:
        thumb_ts = None
        age_days = None

    battery = getattr(camera, "battery", None)
    battery_voltage = None
    if hasattr(camera, "attributes"):
        battery_voltage = camera.attributes.get("battery_voltage")

    wifi = getattr(camera, "wifi_strength", None)

    # Determine status
    if age_days is not None and age_days > 7:
        status = "offline"
        reason = f"Last seen {int(age_days)} days ago"
    elif battery_voltage is not None and battery_voltage < 100:
        status = "low_battery"
        reason = f"Battery voltage: {battery_voltage}"
    else:
        status = "online"
        reason = None

    return {
        "status": status,
        "reason": reason,
        "battery": battery,
        "battery_voltage": battery_voltage,
        "thumbnail_age_days": round(age_days, 1) if age_days is not None else None,
        "wifi_strength": wifi,
    }


# ---- Routes ----

async def handle_index(request):
    """Serve the static dashboard HTML page."""
    html_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    return web.FileResponse(html_path)


async def handle_snapshot_ai(request):
    """Serve AI-annotated snapshot with bounding boxes."""
    name = request.match_info["name"]
    if name in _annotated_cache and _annotated_cache[name]:
        return web.Response(body=_annotated_cache[name], content_type="image/jpeg")
    # Fall back to raw snapshot
    if name in _snapshot_cache and _snapshot_cache[name]:
        return web.Response(body=_snapshot_cache[name], content_type="image/jpeg")
    return web.Response(text="No snapshot available", status=404)


async def handle_snapshot_heatmap(request):
    """Serve motion heatmap image."""
    name = request.match_info["name"]
    if name in _heatmap_cache and _heatmap_cache[name]:
        return web.Response(body=_heatmap_cache[name], content_type="image/jpeg")
    if name in _snapshot_cache and _snapshot_cache[name]:
        return web.Response(body=_snapshot_cache[name], content_type="image/jpeg")
    return web.Response(text="No heatmap available", status=404)


async def handle_api_dashboard(request):
    """Main JSON endpoint for the dashboard — cameras, clips, timeline, stats."""
    cameras = []
    if _blink:
        for name, cam in _blink.cameras.items():
            health = get_camera_health(cam)
            det = _detection_results.get(name, {})
            cameras.append({
                "name": name,
                "temperature": getattr(cam, "temperature", "?"),
                "battery": health.get("battery", "?"),
                "battery_voltage": health.get("battery_voltage", "?"),
                "wifi_strength": health.get("wifi_strength", "?"),
                "health_status": health["status"],
                "health_reason": health.get("reason", ""),
                "alert_level": det.get("alert_level", "none"),
                "ai_summary": det.get("summary", "No AI data yet"),
                "det_counts": dict(ai_processor.get_detection_stats().get(name, {})),
            })

    clip_dir = _config.get("clip_download_path", "/app/clips")
    clip_files = sorted(glob.glob(os.path.join(clip_dir, "*.mp4")),
                        key=os.path.getmtime, reverse=True)[:12]
    clips = []
    for cp in clip_files:
        clips.append({
            "filename": os.path.basename(cp),
            "size_mb": f"{os.path.getsize(cp) / (1024*1024):.1f}",
            "mtime": datetime.fromtimestamp(os.path.getmtime(cp)).strftime("%Y-%m-%d %H:%M"),
        })

    timeline = ai_processor.get_detection_history()[-50:]
    timeline.reverse()

    return web.json_response({
        "num_cameras": len(cameras),
        "last_refresh": _last_refresh,
        "num_clips": len(clip_files),
        "total_detections": len(ai_processor.get_detection_history()),
        "cameras": cameras,
        "clips": clips,
        "timeline": timeline,
    })


async def handle_snapshot(request):
    name = request.match_info["name"]
    if name in _snapshot_cache and _snapshot_cache[name]:
        return web.Response(body=_snapshot_cache[name], content_type="image/jpeg")
    # Return a placeholder
    return web.Response(text="No snapshot available", status=404)


async def handle_api_refresh(request):
    # snap=True only if user explicitly requests it (wakes camera, uses battery)
    snap = request.query.get("snap", "false").lower() == "true"
    await refresh_snapshots(snap=snap)
    return web.json_response({"status": "ok", "last_refresh": _last_refresh, "snapped": snap})


async def handle_api_status(request):
    cameras = {}
    if _blink:
        for name, cam in _blink.cameras.items():
            health = get_camera_health(cam)
            cameras[name] = {
                "armed": cam.arm,
                "temperature": getattr(cam, "temperature", None),
                "battery": getattr(cam, "battery", None),
                "battery_voltage": health.get("battery_voltage"),
                "wifi_strength": health.get("wifi_strength"),
                "status": health["status"],
                "status_reason": health.get("reason"),
                "thumbnail_age_days": health.get("thumbnail_age_days"),
                "last_motion": str(getattr(cam, "last_motion", "")),
            }

    clip_dir = _config.get("clip_download_path", "/app/clips")
    clips = glob.glob(os.path.join(clip_dir, "*.mp4"))

    return web.json_response({
        "cameras": cameras,
        "total_clips": len(clips),
        "last_refresh": _last_refresh,
    })


async def handle_clips_file(request):
    fname = request.match_info["filename"]
    # Sanitize filename
    safe = os.path.basename(fname)
    clip_dir = _config.get("clip_download_path", "/app/clips")
    path = os.path.join(clip_dir, safe)
    if os.path.exists(path):
        return web.FileResponse(path)
    return web.Response(text="Not found", status=404)


async def on_startup(app):
    global _config
    _config = load_config()
    await init_blink(_config)
    await refresh_snapshots()


def create_app():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.router.add_get("/", handle_index)
    app.router.add_get("/snapshot/ai/{name}", handle_snapshot_ai)
    app.router.add_get("/snapshot/heatmap/{name}", handle_snapshot_heatmap)
    app.router.add_get("/snapshot/{name}", handle_snapshot)
    app.router.add_post("/api/refresh", handle_api_refresh)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/api/dashboard", handle_api_dashboard)
    app.router.add_get("/clips/{filename}", handle_clips_file)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    app = create_app()
    port = int(os.environ.get("WEB_PORT", "8080"))
    print(f"Starting Blink Dashboard on http://0.0.0.0:{port}")
    web.run_app(app, host="0.0.0.0", port=port)
