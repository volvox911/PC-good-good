# Troubleshooting

Logs: `%LOCALAPPDATA%\WhatnotGameOverlay\logs\overlay.log.jsonl` (JSON lines; secrets are
scrubbed). Quick self-check: `.venv\Scripts\python -m gameoverlay check --live`.

## Installation

| Symptom | Fix |
|---|---|
| "Python 3.12+ was not found" | Install from python.org (tick *Add python.exe to PATH*) or `winget install -e --id Python.Python.3.12`, then run `Install.cmd` again. |
| Typing `python` opens the Microsoft Store | Windows' Store alias. The installer prefers the `py` launcher; or disable the alias in *Settings → Apps → Advanced app settings → App execution aliases*. |
| "running scripts is disabled on this system" | Use the `.cmd` files (they pass `-ExecutionPolicy Bypass` for that one script only). |
| pip fails / times out | Check internet/proxy, then `Install.cmd -Recreate`. Antivirus can block `.venv` creation — allow the project folder. |

## Starting

| Symptom | Fix |
|---|---|
| "Port 8765 is already in use" | The app is already running (check other console windows), or another program uses the port: `Start.cmd -Port 8770` and update the OBS URL. |
| Lost the dashboard sign-in link | It is printed in the console at start, or run `.venv\Scripts\python -m gameoverlay login-url`. |
| "Not signed in" after a long time | Sessions last 30 days; open the sign-in link again. |
| App restarts by itself | `Start.cmd` restarts after unexpected exits; check the log for the cause. After 5 quick failures it stops and shows the exit code. |

## OBS

| Symptom | Fix |
|---|---|
| Top bar: **OBS disconnected** | OBS not running, WebSocket server disabled (*Tools → WebSocket Server Settings*), or wrong host/port in *Settings → OBS & Camera*. The app retries automatically (backoff up to 30 s); *Test OBS connection* forces a retry. |
| **OBS auth failed** | Password mismatch. Copy it again from *Show Connect Info* into *Settings → Credentials*. |
| Camera dropdown empty / "OBS not connected" | Connect OBS first; then reopen Settings. |
| "Camera capture failed" / black stills | In the camera source properties untick *Deactivate when not showing*; make sure the ZV-1 is on and not used by another app. |
| Overlay not visible in OBS | URL exactly `http://127.0.0.1:8765/overlay`; Browser Source above the camera; source visible (eye icon); width/height = canvas; *Reset Transform*. Dashboard top bar should say `· 1 src`. Remember the overlay is **empty until a game is recognized** — use *Design samples* in the dashboard preview to check the look. |
| Overlay never connects (`· 0 src`) although URL is right | OBS was started before the app, so the first page load failed. Right-click the Browser Source → **Refresh** (needed only once; later restarts reconnect automatically). |
| Overlay has a white/black box | Custom CSS was changed; restore the OBS default (transparent background). |
| Overlay in the wrong place / too small | Browser Source width/height must equal the canvas; set size/position in the dashboard (not in OBS). |
| Overlay shows old prices after restarting the app | It doesn't: on reconnect it follows the new backend state (empty until a verified record). If the backend is unreachable for longer than *Overlay hides after backend lost* (default 120 s) it hides by itself. |

## Recognition

| Symptom | Fix |
|---|---|
| Stays on "Waiting for a game" | Hold the game still ~1 s inside the presentation area. Raise *Motion sensitivity* (lower number) if your hand movements are subtle; lower *Calm samples* for faster triggering. *Scan now* (**S**) forces a recognition. |
| "Image quality too low (blurry / too dark)" | Improve light, reduce glare, enable ZV-1 Product Showcase focus, tighten the crop. *Minimum sharpness* can be lowered slightly. |
| Often **Needs confirmation** | Usually unreadable or obscured titles, missing console branding, or several close PriceCharting products. Show the label/spine clearly. Pick the right product (keys **1–9**) — the correction is remembered. Thresholds: *Auto-accept title score*, *Required lead*, *Minimum confidence*. |
| Wrong edition/region | Editions (Player's Choice, Greatest Hits…) are only used when printed on the item; regions need visible evidence (ratings, PAL text). Correct it once — it is remembered for that recognition. *Default region* sets the fallback (NTSC-U). |
| Same game recognized again after re-posing | Expected when its image changed a lot; it costs one Claude call but no PriceCharting calls (cached) and the overlay doesn't flicker. Raise *New-item threshold* to make re-poses count as "same item". |
| Old prices disappear while recognizing a new game | By design: a different, unidentified item hides the old game after *Hide old prices after unidentified item* (default 6 s). |
| Claude: **not configured / auth failed** | Add/replace the key in *Settings → Credentials*. |
| Claude: **limit reached**, detection paused | Session spend limit hit (estimate). Raise *Session spend limit* in *Settings → Recognition*, then *Resume detection*. Manual search keeps working. |

## PriceCharting

| Symptom | Fix |
|---|---|
| **PriceCharting auth failed** | Token wrong/expired or subscription inactive. Paste the 40-character token from PriceCharting → Subscription → API/Download. |
| "PriceCharting rate limit" | The app never exceeds 1 request/second; repeated 429s mean another program shares your token. The app backs off automatically. |
| "Price refresh failed — not displayed" | The cached price was older than the refresh interval and PriceCharting couldn't be reached. Stale prices are hidden by default; the dashboard shows them labeled. Choose *show_labeled* in *Settings → Pricing* if you prefer a `CACHED` label on air. |
| A price shows **N/A** | PriceCharting returned no value for that condition. The app never shows `$0.00` or substitutes another price type. |

## Resetting

- Settings: change them back in the dashboard (*Settings*); defaults are listed in
  `gameoverlay/config.py`.
- Everything (history, caches, settings, dashboard token): stop the app and delete
  `%LOCALAPPDATA%\WhatnotGameOverlay`. Credentials in Windows Credential Manager remain
  (entries named `whatnot-ai-game-overlay`).
- PriceCharting's terms require purging their data after your subscription ends: delete
  `%LOCALAPPDATA%\WhatnotGameOverlay\overlay.sqlite3`.
