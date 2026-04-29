# 🎥 Sentinel — AI Security Camera System

A two-device security camera system that uses a **Raspberry Pi** for capture and a **Windows PC** for AI analysis. Motion-triggered video clips are recorded on the Pi, uploaded to your PC, analysed by LLaVA, and displayed in real time on the Sentinel web dashboard.

---

## Architecture

```
Raspberry Pi                          Windows PC
─────────────────────────────         ──────────────────────────────────
pi_client.py                          server.py
  │                                     │
  ├─ Pi Camera / USB webcam             ├─ Receives clip via HTTP POST
  ├─ YOLOv8 (person detection)          ├─ Runs LLaVA via Ollama
  ├─ Frame diff (motion detection)      ├─ Builds alert (threat level +
  ├─ Records MP4 clip (pre-roll +       │    description)
  │    6s post-trigger)                 ├─ Stores clip in /recordings
  └─ Uploads clip to PC ──────────────► └─ Pushes alert via WebSocket
                                                    │
                                          sentinel.html (browser dashboard)
```

---

## Files

| File | Runs on | Purpose |
|------|---------|---------|
| `pi_client.py` | Raspberry Pi | Capture, detect, record, upload |
| `server.py` | Windows PC | Receive clips, run LLaVA, push alerts |
| `sentinel.html` | Browser (any device) | Live alert dashboard |

---

## Requirements

### Raspberry Pi

- Raspberry Pi 3B+ or newer
- Pi Camera Module **or** any USB webcam
- Python 3.9+
- Raspberry Pi OS (Bullseye or Bookworm)

Install dependencies:
```bash
pip install ultralytics opencv-python requests --break-system-packages
```

### Windows PC

- Python 3.10+
- [Ollama](https://ollama.com) installed and running
- LLaVA model pulled

Install dependencies:
```powershell
pip install aiohttp aiofiles ollama websockets opencv-python ultralytics
```

---

## Setup — Step by Step

### Step 1 — Set up Ollama on your PC

1. Download and install Ollama from [https://ollama.com](https://ollama.com)
2. Open a terminal and pull the LLaVA model:
   ```powershell
   ollama pull llava
   ```
3. Verify it's running:
   ```powershell
   ollama list
   # should show: llava
   ```

---

### Step 2 — Configure the Pi client

Open `pi_client.py` and edit the config block at the top:

```python
SERVER_URL   = "http://192.168.1.XX:8765"  # ← your PC's local IP address
CAMERA_INDEX = 0                            # 0 = first camera, try 1 if needed
CAMERA_NAME  = "Front Door"                # label shown in the dashboard
```

**Finding your PC's local IP:**
```powershell
ipconfig
# look for "IPv4 Address" under your Wi-Fi or Ethernet adapter
# e.g. 192.168.1.45
```

Other settings you may want to adjust:

| Setting | Default | Description |
|---------|---------|-------------|
| `ALERT_COOLDOWN` | `20` | Seconds between uploads (prevent spam) |
| `CLIP_DURATION_SEC` | `6` | How long to record after motion triggers |
| `PRE_ROLL_FRAMES` | `15` | Frames to include before the trigger moment |
| `CONFIDENCE_THRESHOLD` | `0.45` | YOLO detection confidence (0–1) |
| `TARGET_CLASSES` | `["person"]` | What to detect — add `"car"`, `"dog"` etc. |
| `SHOW_PREVIEW` | `False` | Set `True` if your Pi has a monitor |

---

### Step 3 — Set up the Pi camera

**If using the Pi Camera Module:**
```bash
# Enable camera in raspi-config
sudo raspi-config
# → Interface Options → Camera → Enable → Reboot

# Verify camera is detected
libcamera-hello
ls /dev/video*   # should show /dev/video0
```

**If using a USB webcam:**
No extra setup needed — plug it in and it appears as `/dev/video0` automatically.

---

### Step 4 — Start the server (Windows PC)

```powershell
python server.py
```

You should see:
```
08:00:00 [INFO] Sentinel server starting on 0.0.0.0:8765
08:00:00 [INFO]   WS   → ws://localhost:8765/ws
08:00:00 [INFO]   HTTP → http://localhost:8765/alerts
08:00:00 [INFO]   Clips stored in: C:\...\recordings
```

Leave this terminal open — the server must be running before starting the Pi client.

---

### Step 5 — Start the Pi client

```bash
python pi_client.py
```

You should see:
```
08:00:01 [INFO] Pi client starting — server: http://192.168.1.XX:8765
08:00:01 [INFO] Server reachable: {'status': 'ok', ...}
08:00:01 [INFO] Opening camera 0 (backend ...)
08:00:02 [INFO] Camera ready.
08:00:02 [INFO] Monitoring… (Ctrl-C to stop)
```

When someone walks in front of the camera:
```
08:01:15 [INFO] Trigger! Recording clip abc123...
08:01:22 [INFO] Clip saved: /tmp/sentinel_clips/abc123.mp4
08:01:22 [INFO] Upload OK: abc123.mp4
```

---

### Step 6 — Open the dashboard

Open `sentinel.html` in your browser (double-click the file or drag it into Chrome/Edge).

In the **SOURCE** field at the top, enter:
```
localhost:8765
```

Click **Connect**. The pill should turn green showing `● LIVE · WS`.

When the Pi detects and uploads a clip, an alert card will appear within a few seconds. Click a card to expand it and see the video clip, threat level, and LLaVA's analysis.

---

## How alerts are triggered

An upload only happens when **both** conditions are true simultaneously:

1. **YOLOv8 detects a person** (or whatever `TARGET_CLASSES` is set to) with confidence above `CONFIDENCE_THRESHOLD`
2. **Motion is detected** via frame differencing (pixel change area exceeds `MOVEMENT_THRESHOLD`)

This dual-check prevents false alerts from:
- A person appearing in a still image or TV screen (YOLO would fire, motion wouldn't)
- Wind moving a plant or shadow (motion would fire, YOLO wouldn't)

Once both fire, a cooldown of `ALERT_COOLDOWN` seconds is enforced before the next upload.

---

## Threat levels

LLaVA analyses a frame from the clip and assigns one of these levels:

| Level | Meaning |
|-------|---------|
| 🔴 **CRITICAL** | Weapon, violence, or forced entry detected |
| 🟠 **HIGH** | Suspicious person or behaviour |
| 🔵 **MEDIUM** | Unexpected person or unusual activity |
| 🟢 **LOW** | Person present, routine activity |
| ⚪ **CLEAR** | No person or threat visible |

---

## Troubleshooting

**Pi can't connect to server**
- Check `SERVER_URL` in `pi_client.py` has the correct IP
- Make sure `server.py` is running on the PC
- Check Windows Firewall: allow inbound connections on port `8765`
  ```powershell
  netsh advfirewall firewall add rule name="Sentinel" dir=in action=allow protocol=TCP localport=8765
  ```

**Camera not found on Pi**
- Run `ls /dev/video*` — if nothing shows, the camera isn't detected
- For Pi Camera Module, run `sudo raspi-config` and enable the camera interface, then reboot
- Try `CAMERA_INDEX = 1` if you have multiple devices

**LLaVA is slow or times out**
- LLaVA runs on CPU if you don't have a GPU — expect 10–30s per analysis
- This is fine: the Pi has already saved and uploaded the clip; analysis happens in the background
- Check Ollama is running: `ollama list` should show `llava`

**Dashboard shows "OFFLINE"**
- Make sure `server.py` is running
- Check the SOURCE field says `localhost:8765` (not `http://localhost:8765`)
- Try clicking Disconnect then Connect again

**Video clips won't play in dashboard**
- The clips are saved in the `recordings/` folder next to `server.py`
- They are served at `http://localhost:8765/recordings/`
- Make sure the clip is an MP4 (H.264) — the Pi records `mp4v` which should be compatible

---

## Network diagram

```
┌─────────────────────┐         ┌──────────────────────────┐
│   Raspberry Pi      │         │      Windows PC           │
│                     │         │                           │
│  pi_client.py       │  Wi-Fi  │  server.py                │
│  ─────────────      │────────►│  ──────────               │
│  Camera capture     │ HTTP    │  /upload endpoint         │
│  YOLO detection     │ POST    │  LLaVA analysis           │
│  Motion detect      │ clip+   │  Alert storage            │
│  Clip recording     │ meta    │  WebSocket broadcast      │
└─────────────────────┘         └──────────┬───────────────┘
                                           │ WebSocket
                                           ▼
                                 ┌─────────────────────┐
                                 │   Browser           │
                                 │   sentinel.html     │
                                 │   Alert dashboard   │
                                 └─────────────────────┘
```

---

## Quick reference

| Command | Where |
|---------|-------|
| `python server.py` | Windows PC — start server |
| `python pi_client.py` | Raspberry Pi — start capture |
| Open `sentinel.html`, connect to `localhost:8765` | Browser — view dashboard |
| `ollama pull llava` | Windows PC — download LLaVA model |
| `ollama serve` | Windows PC — start Ollama if not auto-started |
| `Ctrl-C` | Stop either script |
