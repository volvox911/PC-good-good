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
| Lost the dashboard sign-in link / "already used or has expired" | The one-time link works once within 15 minutes. Open `http://127.0.0.1:8765/login` and paste the access token printed by `.venv\Scripts\python -m gameoverlay login-url`. |
| "Not signed in" after a long time | Sessions last 30 days; sign in again as above. |
| App restarts by itself | `Start.cmd` restarts after unexpected exits; check the log for the cause. After 5 quick failures it stops and shows the exit code. |

## OBS

| Symptom | Fix |
|---|---|
| Top bar: **OBS disconnected** | OBS not running, WebSocket server disabled (*Tools → WebSocket Server Settings*), or wrong host/port in *Settings → OBS & Camera*. The app retries automatically (backoff up to 30 s); *Test OBS connection* forces a retry. |
| **OBS auth failed** | Password mismatch. Copy it again from *Show Connect Info* into *Settings → Credentials*. |
| Camera dropdown empty / "OBS not connected" | Connect OBS first; then reopen Settings. |
| "Camera capture failed" / black stills | In the camera source properties untick *Deactivate when not showing*; make sure the ZV-1 is on and not used by another app. |
| Overlay not visible in OBS | URL exactly `http://127.0.0.1:8765/overlay`; Browser Source above the camera; source visible (eye icon); width/height = canvas; *Reset Transform*. Dashboard top bar should say `· 1 src`. Between games the strip shows `PRICE GUIDE` with dashes (unless *Keep the strip on screen between games* is off) — use *Design samples* in the dashboard preview to check the look. |
| Overlay never connects (`· 0 src`) although URL is right | OBS was started before the app, so the first page load failed. Right-click the Browser Source → **Refresh** (needed only once; later restarts reconnect automatically). |
| Stream shows the game's name but no prices (dashboard shows them) | Title-only mode is on: the *Show stream prices* button is highlighted and the top bar says `title only`. Click it (or press **V**) to show prices again. |
| Overlay has a white/black box | Custom CSS was changed; restore the OBS default (transparent background). |
| Overlay in the wrong place / too small | Browser Source width/height must equal the canvas (the strip sizes itself to the canvas width); set top/bottom and the extra distance from the edge in the dashboard (not in OBS). |
| OBS still shows an older overlay look after an update | Pages opened by a version with the self-reload reload themselves once. Otherwise right-click the Browser Source → **Properties → Refresh cache of current page** once. |
| Lettering looks like a plain system font | The strip's fonts are served by the app; make sure the URL is the app's `http://127.0.0.1:8765/overlay` (not a local file) and refresh the page once. |
| Overlay shows old prices after restarting the app | It doesn't: on reconnect it follows the new backend state (cleared until a verified record). If the backend is unreachable for longer than *Overlay hides after backend lost* (default 120 s) it clears the prices by itself. |

## Recognition

| Symptom | Fix |
|---|---|
| Stays on "Waiting for a game" | Hold the game still ~1 s inside the presentation area. Raise *Motion sensitivity* (lower number) if your hand movements are subtle; lower *Calm samples* for faster triggering. *Scan now* (**S**) forces a recognition. |
| "Image quality too low (blurry / too dark)" | Quality is measured on the game itself, not the backdrop, so a dark or black backdrop is fine. Put more light on the game, reduce glare, enable ZV-1 Product Showcase focus, tighten the crop. The dashboard's capture caption shows the measured brightness/sharpness and how much of the frame was judged to be the game. *Minimum sharpness* and *Minimum brightness of the game* (Settings → Recognition) can be lowered slightly. |
| Often **Needs confirmation** | Usually unreadable or obscured titles, missing console branding, or several close PriceCharting products. Show the label/spine clearly. Pick the right product (keys **1–9**) — a suggestion pick is remembered as a correction. Thresholds: *Auto-accept title score*, *Required lead*, *Minimum confidence*. |
| Wrong edition/region | Editions (Player's Choice, Greatest Hits…) are only used when printed on the item; regions need visible evidence (ratings, PAL text). Search for the right product with *This corrects the item on camera* ticked — it is remembered for that recognition. *Default region* sets the fallback (NTSC-U). |
| Released the hold but the held game stayed up | If a new game was resolved while held, releasing shows it (after a fresh price check). Otherwise the next calm frame is re-checked automatically within a couple of seconds. |
| Prices disappeared after a long time on air | On-air prices older than the cache duration are re-validated; if PriceCharting can't be reached they are taken off air rather than shown as current. |
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
