Here's a complete walkthrough of how `sentinel.html` works from the moment you open it in a browser.

---

## Structure overview

The file is a single self-contained HTML page with three sections — CSS styles, HTML markup, and a JavaScript application. There is no framework, no build step, no dependencies beyond two Google Fonts.

---

## 1. Auto-connection on load

The very last line of the script is `connect()`, which fires the moment the page finishes loading.

`connect()` calls `connectWS()`, which tries to open a WebSocket to `ws://localhost:8765/ws` (the address shown in the connection strip input). The connection goes through three possible states, reflected in the status pill:

- **CONNECTING…** (amber) — socket is being opened
- **LIVE · WS** (green) — socket is open and receiving pushes
- **RECONNECTING…** (amber) → retries with exponential backoff if it drops

If the WebSocket can't connect or drops, `startPoll()` is called automatically as a fallback. This hits `GET /alerts?limit=200` on the interval you select (3s / 5s / 10s / 30s). The moment the WebSocket reconnects, polling stops.

---

## 2. Fetching alert history

The instant the WebSocket opens, `fetchAll()` is called. This makes a single `GET /alerts?limit=200` request and calls `replaceAll()`, which populates the feed with everything the Python backend has seen so far. This means when you open the dashboard mid-session, you see all previous alerts immediately, not just new ones going forward.

---

## 3. Receiving new alerts in real time

When `security_monitor.py` finishes processing a clip it calls `_broadcast()`, which sends a JSON message over the WebSocket to every connected client. The message shape is:

```json
{ "type": "alert", "alert": { ...alert fields... } }
```

The `ws.onmessage` handler in the page receives this, extracts `msg.alert`, and calls `ingestOne(raw, true)`. The `true` flag means the card gets a blue flash animation and a toast notification showing the threat level.

---

## 4. The normalise function

Every raw alert object — whether it arrives over WebSocket, HTTP polling, or history fetch — is passed through `norm()` before being stored. This function does three things:

**Schema translation** — the Python backend uses fields like `camera`, `alert`, `details`, `yolo_summary`. The function maps these to consistent internal names (`camera_id`, `alert_line`, `details_line`, `detected_objects`) so the renderer never has to care which format came in.

**URL construction** — `clip_url` from the backend is a relative path like `/recordings/clip_20240427.mp4`. The function prepends `httpBase()` (e.g. `http://localhost:8765`) to make it an absolute URL the `<video>` tag can actually fetch. Same for `thumb_url`.

**Object tag parsing** — if `detected_objects` is empty but `yolo_summary` contains `"2x person, 1x backpack"`, it splits that string into individual tag strings so they render as individual badge elements.

---

## 5. Rendering the feed

`renderFeed()` is called any time the data or filter changes. It does the following in order:

**Snapshot expanded state** — before wiping `feed.innerHTML`, it records which card IDs were expanded so it can restore them after the re-render. Without this, every update would collapse all open cards.

**Filter** — if a filter button other than ALL is active, the alerts array is filtered to only that threat level before rendering.

**Sort** — the filtered list is sorted by the current sort mode: newest first (default), oldest first, or highest threat first. Threat ordering uses the `THREAT_ORDER` map: `CRITICAL=4, HIGH=3, MEDIUM=2, LOW=1, UNKNOWN=0`.

**Card generation** — for each alert a `<div class="alert-card">` is built with two parts:

- **Card header** — always visible: threat badge, camera name, one-line alert summary (truncated at 115 chars), timestamp, and a chevron arrow
- **Card body** — hidden until expanded: video panel on the left, detail panel on the right

The **video panel** has three states:
- If `clip_url` is present → renders a `<video controls>` element with the clip as `src` and the thumbnail as `poster` (shown before play is pressed)
- If only `thumb_url` → shows a static image
- Otherwise → shows a placeholder icon with the local file path as a hint

The **detail panel** contains the threat meter bar (a coloured progress bar whose width maps to threat level), the LLaVA alert sentence, the LLaVA details paragraph, YOLO object tags, a metadata grid (camera, date, model, frame count), and three action buttons.

---

## 6. Action buttons

Each card has three buttons wired up with `data-action` attributes:

**Dismiss** — adds the alert's `_id` to the `dismissed` Set, removes it from the `alerts` array, and re-renders. The dismissed set persists for the session, so if the HTTP poll replaces the alerts array the dismissed cards stay hidden. Refreshing the page resets dismissals.

**Copy JSON** — calls `navigator.clipboard.writeText()` with `JSON.stringify(obj._raw)` — the original unmodified object exactly as it came from the Python backend.

**Escalate** — currently shows a toast. The comment in the code shows where you'd wire in a `fetch('/api/escalate', ...)` call.

---

## 7. Stats and filter counts

`updateStats()` is called every time the alerts array changes. It counts the full `alerts` array (not just what's visible after filtering) and updates the five summary cards at the top and the two pills in the topbar.

---

## 8. The connection strip

The URL input defaults to `localhost:8765`. If your Python server runs on a different machine (e.g. a Raspberry Pi at `192.168.1.50:8765`), you type that address, click **Disconnect**, then **Connect** — the page will reconnect to the new address. While connected, the input is disabled to prevent accidental edits.

---

## Data flow summary

```
Python backend
      │
      │  WebSocket push  { type:'alert', alert:{...} }
      │  (or HTTP poll   GET /alerts → { alerts:[...] } )
      ▼
   norm()  ←── translates schema, builds absolute URLs
      │
      ▼
  alerts[]  ←── in-memory array, session-only
      │
      ▼
renderFeed()  ←── filter → sort → build DOM cards
      │
      ▼
   <video src="http://localhost:8765/recordings/clip_xyz.mp4">
                    ↑
         fetched directly from FastAPI StaticFiles
```

The page holds no persistent state — everything resets on refresh, which is why `fetchAll()` runs on every new connection.