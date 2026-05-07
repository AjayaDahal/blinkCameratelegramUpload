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
from datetime import datetime

from aiohttp import web
from blinkpy.blinkpy import Blink
from blinkpy.auth import Auth
from blinkpy.helpers.util import json_load

logger = logging.getLogger(__name__)

# Shared state
_blink: Blink | None = None
_config: dict = {}
_snapshot_cache: dict[str, bytes] = {}
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

    _last_refresh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---- Routes ----

async def handle_index(request):
    cameras_html = ""
    for name in (_blink.cameras if _blink else {}):
        cameras_html += f"""
        <div class="camera-card">
            <h2>{name}</h2>
            <img src="/snapshot/{name}" alt="{name}" id="cam-{name.replace(' ','_')}" />
            <div class="cam-actions">
                <button onclick="refreshCam('{name}')">Snap New Photo</button>
            </div>
        </div>
        """

    # Recent clips
    clip_dir = _config.get("clip_download_path", "/app/clips")
    clips = sorted(glob.glob(os.path.join(clip_dir, "*.mp4")),
                   key=os.path.getmtime, reverse=True)[:12]
    clips_html = ""
    for clip_path in clips:
        fname = os.path.basename(clip_path)
        size = os.path.getsize(clip_path) / (1024 * 1024)
        mtime = datetime.fromtimestamp(os.path.getmtime(clip_path)).strftime("%Y-%m-%d %H:%M")
        clips_html += f"""
        <div class="clip-card">
            <video controls preload="metadata" width="320">
                <source src="/clips/{fname}" type="video/mp4" />
            </video>
            <p><strong>{fname}</strong><br/>{size:.1f} MB | {mtime}</p>
        </div>
        """

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Blink Camera Dashboard</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #0a0a0a; color: #e0e0e0; padding: 20px; }}
    h1 {{ text-align: center; margin-bottom: 8px; color: #00d4ff;
          font-size: 28px; letter-spacing: 1px; }}
    .subtitle {{ text-align: center; color: #666; margin-bottom: 30px; font-size: 14px; }}
    .status-bar {{ display: flex; justify-content: center; gap: 30px;
                   margin-bottom: 30px; padding: 12px; background: #111;
                   border-radius: 10px; border: 1px solid #222; flex-wrap: wrap; }}
    .status-item {{ text-align: center; }}
    .status-item .label {{ font-size: 11px; color: #888; text-transform: uppercase; }}
    .status-item .value {{ font-size: 18px; font-weight: bold; color: #00d4ff; }}
    .section-title {{ font-size: 20px; margin: 30px 0 15px; color: #fff;
                      border-bottom: 2px solid #00d4ff33; padding-bottom: 8px; }}
    .cameras {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(400px, 1fr));
                gap: 20px; margin-bottom: 30px; }}
    .camera-card {{ background: #111; border-radius: 12px; overflow: hidden;
                    border: 1px solid #222; transition: border-color 0.3s; }}
    .camera-card:hover {{ border-color: #00d4ff55; }}
    .camera-card h2 {{ padding: 12px 16px; font-size: 16px; background: #151515;
                       border-bottom: 1px solid #222; }}
    .camera-card img {{ width: 100%; display: block; min-height: 240px;
                        object-fit: contain; background: #000; }}
    .cam-actions {{ padding: 10px 16px; text-align: right; }}
    .cam-actions button, .refresh-all {{ cursor: pointer; padding: 8px 18px;
        border: 1px solid #00d4ff44; background: #00d4ff15; color: #00d4ff;
        border-radius: 6px; font-size: 13px; transition: all 0.2s; }}
    .cam-actions button:hover, .refresh-all:hover {{ background: #00d4ff30; }}
    .clips {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
              gap: 16px; }}
    .clip-card {{ background: #111; border-radius: 10px; overflow: hidden;
                  border: 1px solid #222; }}
    .clip-card video {{ width: 100%; display: block; background: #000; }}
    .clip-card p {{ padding: 10px; font-size: 12px; color: #aaa; }}
    .refresh-all {{ display: block; margin: 0 auto 20px; font-size: 15px; padding: 10px 30px; }}
    .spinner {{ display: none; width: 20px; height: 20px; border: 2px solid #00d4ff33;
                border-top-color: #00d4ff; border-radius: 50%; animation: spin 0.8s linear infinite;
                margin: 0 auto; }}
    @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
    .live-dot {{ display: inline-block; width: 8px; height: 8px; background: #0f0;
                 border-radius: 50%; margin-right: 6px; animation: blink-dot 2s infinite; }}
    @keyframes blink-dot {{ 50% {{ opacity: 0.3; }} }}
</style>
</head>
<body>
    <h1>Blink Camera Dashboard</h1>
    <p class="subtitle"><span class="live-dot"></span>Live View &mdash; No Subscription Required</p>

    <div class="status-bar">
        <div class="status-item">
            <div class="label">Cameras</div>
            <div class="value">{len(_blink.cameras) if _blink else 0}</div>
        </div>
        <div class="status-item">
            <div class="label">Last Refresh</div>
            <div class="value" id="last-refresh">{_last_refresh}</div>
        </div>
        <div class="status-item">
            <div class="label">Local Clips</div>
            <div class="value">{len(clips)}</div>
        </div>
    </div>

    <button class="refresh-all" onclick="refreshAll()">Refresh All Cameras</button>
    <div class="spinner" id="spinner"></div>

    <h3 class="section-title">Live Cameras</h3>
    <div class="cameras">{cameras_html if cameras_html else '<p style="color:#666;">No cameras found. Check Blink credentials.</p>'}</div>

    <h3 class="section-title">Recent Motion Clips</h3>
    <div class="clips">{clips_html if clips_html else '<p style="color:#666;">No clips yet. Motion events will appear here.</p>'}</div>

    <script>
        async function refreshAll() {{
            document.getElementById('spinner').style.display = 'block';
            try {{
                const r = await fetch('/api/refresh?snap=true', { method: 'POST' });
                const data = await r.json();
                document.getElementById('last-refresh').textContent = data.last_refresh;
                // Reload all images with cache bust
                document.querySelectorAll('.camera-card img').forEach(img => {{
                    img.src = img.src.split('?')[0] + '?t=' + Date.now();
                }});
            }} catch(e) {{ console.error(e); }}
            document.getElementById('spinner').style.display = 'none';
        }}

        async function refreshCam(name) {{
            const r = await fetch('/api/refresh', {{ method: 'POST' }});
            const img = document.getElementById('cam-' + name.replace(/ /g, '_'));
            if (img) img.src = '/snapshot/' + encodeURIComponent(name) + '?t=' + Date.now();
        }}

        // Auto-refresh every 30 seconds
        setInterval(() => {{
            document.querySelectorAll('.camera-card img').forEach(img => {{
                img.src = img.src.split('?')[0] + '?t=' + Date.now();
            }});
        }}, 30000);
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


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
            cameras[name] = {
                "armed": cam.arm,
                "temperature": getattr(cam, "temperature", None),
                "battery": getattr(cam, "battery", None),
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
    app.router.add_get("/snapshot/{name}", handle_snapshot)
    app.router.add_post("/api/refresh", handle_api_refresh)
    app.router.add_get("/api/status", handle_api_status)
    app.router.add_get("/clips/{filename}", handle_clips_file)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    app = create_app()
    port = int(os.environ.get("WEB_PORT", "8080"))
    print(f"Starting Blink Dashboard on http://0.0.0.0:{port}")
    web.run_app(app, host="0.0.0.0", port=port)
