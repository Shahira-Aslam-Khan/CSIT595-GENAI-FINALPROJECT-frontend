"""
pi_client.py — Runs on Raspberry Pi
Lightweight: capture → YOLO → motion → record clip → upload to server

Dependencies (Pi):
  pip install ultralytics opencv-python requests --break-system-packages

No Ollama needed on the Pi.
"""

import cv2
import time
import uuid
import json
import logging
import platform
import tempfile
import os
import threading
from datetime import datetime
from pathlib import Path

import requests
from ultralytics import YOLO

# ─────────────────────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────────────────────

SERVER_URL          = "http://192.168.1.XX:8765"   # ← change to your PC's IP
CAMERA_INDEX        = 0
CAMERA_NAME         = "Front Door"

FRAME_WIDTH         = 640
FRAME_HEIGHT        = 480
FRAME_FPS           = 15
WARMUP_FRAMES       = 10

YOLO_MODEL          = "yolov8n.pt"
CONFIDENCE_THRESHOLD = 0.45
TARGET_CLASSES      = ["person"]

MOVEMENT_THRESHOLD  = 3500      # pixel diff area
ALERT_COOLDOWN      = 20        # seconds between uploads

# Clip recording
PRE_ROLL_FRAMES     = 15        # frames to keep before trigger
CLIP_DURATION_SEC   = 6         # how many seconds to record after trigger
CLIP_DIR            = Path(tempfile.gettempdir()) / "sentinel_clips"

SHOW_PREVIEW        = False     # True if Pi has a display attached

IS_WINDOWS = platform.system() == "Windows"

# ─────────────────────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pi-client")

# ─────────────────────────────────────────────────────────────
#  CAMERA
# ─────────────────────────────────────────────────────────────

def open_camera() -> cv2.VideoCapture:
    backend = cv2.CAP_DSHOW if IS_WINDOWS else cv2.CAP_V4L2
    log.info(f"Opening camera {CAMERA_INDEX} (backend {backend})")
    cam = cv2.VideoCapture(CAMERA_INDEX, backend)
    cam.set(cv2.CAP_PROP_FRAME_WIDTH,  FRAME_WIDTH)
    cam.set(cv2.CAP_PROP_FRAME_HEIGHT, FRAME_HEIGHT)
    cam.set(cv2.CAP_PROP_FPS,          FRAME_FPS)
    if not cam.isOpened():
        raise RuntimeError(f"Cannot open camera {CAMERA_INDEX}")
    log.info(f"Warming up ({WARMUP_FRAMES} frames)…")
    for _ in range(WARMUP_FRAMES):
        cam.read()
    log.info("Camera ready.")
    return cam

# ─────────────────────────────────────────────────────────────
#  MOTION
# ─────────────────────────────────────────────────────────────

def detect_motion(prev_gray, curr_gray) -> bool:
    if prev_gray is None:
        return False
    diff = cv2.absdiff(prev_gray, curr_gray)
    _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
    return cv2.countNonZero(thresh) > MOVEMENT_THRESHOLD

# ─────────────────────────────────────────────────────────────
#  CLIP RECORDING
# ─────────────────────────────────────────────────────────────

CLIP_DIR.mkdir(parents=True, exist_ok=True)

def save_clip(pre_roll: list, camera: cv2.VideoCapture,
              clip_id: str, fps: float) -> Path:
    """Write pre-roll + live frames into an MP4 clip and return its path."""
    path = CLIP_DIR / f"{clip_id}.mp4"
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    writer = cv2.VideoWriter(str(path), fourcc, fps,
                             (FRAME_WIDTH, FRAME_HEIGHT))

    # Write pre-roll buffer
    for f in pre_roll:
        writer.write(f)

    # Record live frames for CLIP_DURATION_SEC seconds
    post_frames = int(fps * CLIP_DURATION_SEC)
    for _ in range(post_frames):
        ok, frame = camera.read()
        if ok:
            writer.write(frame)

    writer.release()
    log.info(f"Clip saved: {path} ({len(pre_roll) + post_frames} frames)")
    return path

# ─────────────────────────────────────────────────────────────
#  UPLOAD
# ─────────────────────────────────────────────────────────────

def upload_clip(clip_path: Path, clip_id: str,
                yolo_labels: list[str], trigger_frame: int):
    """POST the clip + metadata to the server. Runs in a background thread."""
    meta = {
        "id":               clip_id,
        "timestamp":        datetime.now().isoformat(),
        "camera":           CAMERA_NAME,
        "yolo_summary":     ", ".join(yolo_labels),
        "detected_objects": yolo_labels,
        "frame_count":      trigger_frame,
    }
    try:
        with open(clip_path, "rb") as f:
            resp = requests.post(
                f"{SERVER_URL}/upload",
                files={"clip": (clip_path.name, f, "video/mp4")},
                data={"meta": json.dumps(meta)},
                timeout=30,
            )
        if resp.status_code == 200:
            log.info(f"Upload OK: {clip_path.name}")
        else:
            log.warning(f"Upload failed: {resp.status_code} {resp.text[:120]}")
    except Exception as e:
        log.error(f"Upload error: {e}")
    finally:
        # Clean up local clip after upload
        try:
            clip_path.unlink()
        except Exception:
            pass

# ─────────────────────────────────────────────────────────────
#  MAIN LOOP
# ─────────────────────────────────────────────────────────────

def main():
    log.info(f"Pi client starting — server: {SERVER_URL}")

    # Verify server reachable
    try:
        r = requests.get(f"{SERVER_URL}/health", timeout=5)
        log.info(f"Server reachable: {r.json()}")
    except Exception as e:
        log.warning(f"Server not reachable yet ({e}) — will retry on each upload.")

    model  = YOLO(YOLO_MODEL)
    camera = open_camera()

    # Estimate actual FPS
    actual_fps = camera.get(cv2.CAP_PROP_FPS) or FRAME_FPS

    if SHOW_PREVIEW:
        cv2.namedWindow("Pi Sentinel", cv2.WINDOW_NORMAL)

    prev_gray        = None
    last_alert_time  = 0
    frame_number     = 0
    consecutive_fail = 0

    # Ring buffer for pre-roll
    pre_roll: list = []

    log.info("Monitoring… (Ctrl-C to stop)")

    while True:
        ok, frame = camera.read()
        if not ok:
            consecutive_fail += 1
            if consecutive_fail >= 30:
                log.error("Too many failures — restarting camera.")
                camera.release()
                time.sleep(2)
                camera = open_camera()
                consecutive_fail = 0
            time.sleep(0.05)
            continue

        consecutive_fail = 0
        frame_number += 1
        curr_gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

        # Maintain pre-roll ring buffer
        pre_roll.append(frame.copy())
        if len(pre_roll) > PRE_ROLL_FRAMES:
            pre_roll.pop(0)

        # ── YOLO ──────────────────────────────────────────────
        results = model(frame, verbose=False)[0]
        yolo_labels     = []
        detected_person = False

        for box in results.boxes:
            conf  = float(box.conf[0])
            cls   = int(box.cls[0])
            label = model.names[cls]
            if conf < CONFIDENCE_THRESHOLD:
                continue
            yolo_labels.append(f"{label}:{conf:.2f}")
            if label in TARGET_CLASSES:
                detected_person = True

            if SHOW_PREVIEW:
                x1, y1, x2, y2 = map(int, box.xyxy[0])
                cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 200, 50), 2)
                cv2.putText(frame, f"{label} {conf:.2f}",
                            (x1, y1 - 6), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (0, 200, 50), 1)

        # ── Motion ────────────────────────────────────────────
        motion = detect_motion(prev_gray, curr_gray)
        prev_gray = curr_gray

        # ── Trigger ───────────────────────────────────────────
        now = time.time()
        if (detected_person and motion
                and (now - last_alert_time) >= ALERT_COOLDOWN):
            last_alert_time = now
            clip_id = str(uuid.uuid4())
            log.info(f"Trigger! Recording clip {clip_id}")

            clip_path = save_clip(list(pre_roll), camera, clip_id, actual_fps)
            pre_roll.clear()

            threading.Thread(
                target=upload_clip,
                args=(clip_path, clip_id, yolo_labels, frame_number),
                daemon=True,
            ).start()

        # ── Preview ───────────────────────────────────────────
        if SHOW_PREVIEW:
            status = f"M:{'Y' if motion else 'N'}  P:{'Y' if detected_person else 'N'}"
            cv2.putText(frame, f"{CAMERA_NAME} | {status}",
                        (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 50), 2)
            cv2.imshow("Pi Sentinel", frame)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord('q'), 27):
                break

    camera.release()
    if SHOW_PREVIEW:
        cv2.destroyAllWindows()
    log.info("Pi client stopped.")


if __name__ == "__main__":
    main()
