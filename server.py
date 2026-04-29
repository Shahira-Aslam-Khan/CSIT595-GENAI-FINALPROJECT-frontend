"""
server.py — Runs on Windows PC
Receives clips from Pi, runs LLaVA, pushes alerts to Sentinel dashboard

Dependencies (Windows):
  pip install aiohttp aiofiles ollama websockets ultralytics opencv-python

Run:
  python server.py
Then open sentinel.html and connect to localhost:8765
"""

import asyncio
import base64
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

import aiofiles
import aiohttp
from aiohttp import web
import cv2
import ollama

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────

SERVER_HOST         = "0.0.0.0"
SERVER_PORT         = 8765

RECORDINGS_DIR      = Path("recordings")   # clips stored here; served at /recordings/
MAX_ALERTS_STORED   = 200

OLLAMA_MODEL        = "llava"
OLLAMA_HOST         = "http://localhost:11434"

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sentinel-server")

RECORDINGS_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
#  SHARED STATE
# ─────────────────────────────────────────────────────────────

alert_store: list[dict] = []        # newest first
ws_clients:  set        = set()

# ─────────────────────────────────────────────────────────────
#  LLAVA
# ─────────────────────────────────────────────────────────────

LLAVA_PROMPT = """You are a security camera AI. Analyse this frame and reply with EXACTLY three lines:
THREAT: <CRITICAL|HIGH|MEDIUM|LOW|CLEAR>
ALERT: <one-sentence summary of what you see>
DETAILS: <one-sentence additional context or recommended action>

Rules:
- CRITICAL = weapon/violence/forced entry
- HIGH = suspicious person or behaviour
- MEDIUM = unexpected person or unusual activity
- LOW = person present, routine activity
- CLEAR = no person or threat visible
Reply with only those three lines, nothing else."""


def frame_to_b64(frame) -> str:
    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return base64.b64encode(buf.tobytes()).decode()


def parse_llava(text: str) -> tuple[str, str, str]:
    threat, alert, details = "UNKNOWN", text.strip(), ""
    for line in text.splitlines():
        l = line.strip()
        if l.upper().startswith("THREAT:"):
            threat = l.split(":", 1)[1].strip().upper()
        elif l.upper().startswith("ALERT:"):
            alert = l.split(":", 1)[1].strip()
        elif l.upper().startswith("DETAILS:"):
            details = l.split(":", 1)[1].strip()
    return threat, alert, details


def analyse_clip(clip_path: Path) -> tuple[str, str, str]:
    """Extract a representative frame and run LLaVA on it."""
    cap = cv2.VideoCapture(str(clip_path))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    # Seek to ~25% through clip — usually shows the trigger moment
    target = max(0, total // 4)
    cap.set(cv2.CAP_PROP_POS_FRAMES, target)
    ok, frame = cap.read()
    cap.release()

    if not ok:
        log.warning(f"Could not read frame from {clip_path.name}")
        return "UNKNOWN", "Could not analyse clip", ""

    try:
        client = ollama.Client(host=OLLAMA_HOST)
        resp = client.chat(
            model=OLLAMA_MODEL,
            messages=[{
                "role":    "user",
                "content": LLAVA_PROMPT,
                "images":  [frame_to_b64(frame)],
            }],
        )
        return parse_llava(resp["message"]["content"])
    except Exception as e:
        log.error(f"LLaVA error: {e}")
        return "UNKNOWN", f"LLaVA error: {e}", ""

# ─────────────────────────────────────────────────────────────
#  WEBSOCKET BROADCAST
# ─────────────────────────────────────────────────────────────

async def broadcast(payload: dict):
    if not ws_clients:
        return
    msg = json.dumps({"type": "alert", "alert": payload})
    dead = set()
    for ws in list(ws_clients):
        try:
            await ws.send_str(msg)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)

# ─────────────────────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────────────────────

async def handle_ws(request):
    """WebSocket endpoint — used by Sentinel dashboard."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    ws_clients.add(ws)
    log.info(f"WS client connected: {request.remote}  (total: {len(ws_clients)})")

    # Send history immediately on connect
    for alert in reversed(alert_store):
        try:
            await ws.send_str(json.dumps({"type": "history", "alert": alert}))
        except Exception:
            break

    async for msg in ws:
        pass   # we don't expect messages from the dashboard

    ws_clients.discard(ws)
    log.info(f"WS client disconnected (remaining: {len(ws_clients)})")
    return ws


async def handle_upload(request):
    """
    POST /upload
    multipart: clip (video/mp4) + meta (JSON string)
    """
    reader = await request.multipart()
    clip_path = None
    meta      = {}

    async for part in reader:
        if part.name == "clip":
            filename = part.filename or f"{uuid.uuid4()}.mp4"
            clip_path = RECORDINGS_DIR / filename
            async with aiofiles.open(clip_path, "wb") as f:
                while True:
                    chunk = await part.read_chunk(65536)
                    if not chunk:
                        break
                    await f.write(chunk)
            log.info(f"Clip received: {clip_path.name} ({clip_path.stat().st_size // 1024} KB)")

        elif part.name == "meta":
            raw = await part.read()
            try:
                meta = json.loads(raw)
            except Exception:
                pass

    if clip_path is None:
        return web.json_response({"error": "no clip received"}, status=400)

    # Run LLaVA in thread pool (blocking)
    loop = asyncio.get_event_loop()
    threat, alert_line, details = await loop.run_in_executor(
        None, analyse_clip, clip_path
    )

    payload = {
        "id":               meta.get("id", str(uuid.uuid4())),
        "timestamp":        meta.get("timestamp", datetime.now().isoformat()),
        "threat_level":     threat,
        "camera":           meta.get("camera", "Unknown"),
        "alert":            alert_line,
        "details":          details,
        "yolo_summary":     meta.get("yolo_summary", ""),
        "detected_objects": meta.get("detected_objects", []),
        "frame_count":      meta.get("frame_count", 0),
        "model":            OLLAMA_MODEL,
        "clip_url":         f"/recordings/{clip_path.name}",
        "playable":         True,
    }

    # Store
    alert_store.insert(0, payload)
    if len(alert_store) > MAX_ALERTS_STORED:
        alert_store.pop()

    # Broadcast to all dashboard clients
    await broadcast(payload)

    log.info(f"[ALERT] {threat} — {alert_line[:80]}")
    return web.json_response({"status": "ok", "alert_id": payload["id"]})


async def handle_alerts(request):
    """GET /alerts?limit=200 — HTTP fallback for dashboard polling."""
    limit = int(request.rel_url.query.get("limit", 200))
    return web.json_response({"alerts": alert_store[:limit], "count": len(alert_store)})


async def handle_health(request):
    return web.json_response({
        "status":  "ok",
        "alerts":  len(alert_store),
        "clients": len(ws_clients),
    })


# ─────────────────────────────────────────────────────────────
#  APP SETUP
# ─────────────────────────────────────────────────────────────

def make_app() -> web.Application:
    app = web.Application(client_max_size=500 * 1024 * 1024)  # 500 MB upload limit
    app.router.add_get( "/ws",           handle_ws)
    app.router.add_post("/upload",        handle_upload)
    app.router.add_get( "/alerts",        handle_alerts)
    app.router.add_get( "/health",        handle_health)
    # Serve saved clips so the dashboard can play them
    app.router.add_static("/recordings/", path=str(RECORDINGS_DIR), name="recordings")
    return app


if __name__ == "__main__":
    log.info(f"Sentinel server starting on {SERVER_HOST}:{SERVER_PORT}")
    log.info(f"  WS   → ws://localhost:{SERVER_PORT}/ws")
    log.info(f"  HTTP → http://localhost:{SERVER_PORT}/alerts")
    log.info(f"  Clips stored in: {RECORDINGS_DIR.resolve()}")
    web.run_app(make_app(), host=SERVER_HOST, port=SERVER_PORT, print=None)
