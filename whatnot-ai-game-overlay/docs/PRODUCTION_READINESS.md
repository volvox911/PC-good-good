# Production readiness audit

Date of audit: 2026-11-10. Environment where this was built and tested: Linux container,
Python 3.13, Node 22, headless Chromium (Playwright). **No Windows machine, no OBS, no Sony
ZV-1, no Anthropic API key and no PriceCharting token were available.**

## 1. What was implemented

| Area | Implementation |
|---|---|
| Backend | Python 3.12+, FastAPI + uvicorn, asyncio; single local process; `python -m gameoverlay serve`. |
| Configuration | `.env`/env vars for bootstrap (dev/test/production + demo), validated runtime settings persisted in SQLite and editable in the dashboard. |
| Credentials | Windows Credential Manager via `keyring` (env vars override; owner-only file fallback when no keyring exists). Write-only in the UI. |
| Database | SQLite (WAL) with versioned migrations; price cache, match cache (auto + manual corrections), search cache, history, visual fingerprints, API usage, sessions (crash detection). |
| PriceCharting | `/api/products` search, `/api/product` by id and UPC; exact field mapping; integer pennies → USD; global serialized queue ≥ 1.05 s (default 1.2 s); retries/backoff for 429/5xx/timeouts; auth errors not retried; strict response validation; token scrubbed from logs. |
| OBS | Own obs-websocket v5 client (Hello/Identify/auth, requests), read-only allowlist, source discovery, `GetSourceScreenshot` of the camera input only, auto-reconnect with backoff. |
| Detection | Localized motion + stability detector, periodic drift check, grid fingerprints (same item / new item / empty background), visual memory of earlier identifications, blur/brightness/contrast quality gate, burst capture, crop + rotation. |
| Recognition | Anthropic SDK `beta.messages.parse` with a Pydantic schema (title, platform from PriceCharting's console vocabulary, region, edition, evidence, confidence, obscured flag, more-images flag), `claude-opus-5-5` at effort `low` (configurable), server-side refusal fallbacks, refusal → "not identified", prompt caching, per-session spend limit + requests/minute cap + cost estimates. |
| Matching | Console family + region must match; title similarity with significant numbers (sequels, "3D"); edition adjustments; accept score + margin over runner-up + minimum confidence; UPC when legible; match cache; manual corrections override automatic matches. |
| State machine | Idle / Detecting / Recognizing / Matching / Price lookup / Displaying / Needs confirmation / Error / Paused; generation-based supersession; corroboration before replacing a game; minimum display time; unidentified/empty grace periods; display timeout; hold with "show new game"; rescan. |
| Overlay | The operator's Joe Dirt price strip (supplied `index.html` / `style.css` / `overlay.js` / text-free `header-skin.png`, used directly): ≈170 px at the top of a 1080×1920 frame, transparent elsewhere; exact font families from the design's font guide (Knewave, Barlow Condensed, Anton) bundled under the OFL; whole-dollar prices (configurable), `N/A`; horizontal-only text fitting so long names and six-figure prices fit; atomic updates with no transition and no frame movement; stays on screen between games with prices cleared (configurable); container-unit fallback for older OBS; read-only auto-reconnecting WebSocket; self-reload once after an app update; hides prices after a long backend outage; `window.setGameOverlayData(record)` API; watermarked demo/preview. |
| Dashboard | Status, current item + evidence + suggestions, controls with keyboard shortcuts, manual search, UPC/barcode scanner, on-air and design previews, history + recall, settings (all thresholds, crop editor, background capture), first-run wizard. |
| Security | 127.0.0.1-only bind by default, one-time sign-in links (the permanent admin token is only ever POSTed, never put in URLs), signed HttpOnly SameSite=Strict session cookie, CSRF tokens, Host/Origin checks (DNS rebinding; overlay sockets too), strict CSP, public overlay limited to read-only data, admin token redacted from logs. |
| Reliability | Engine loop never dies (errors are reported), structured JSON logs, graceful shutdown, crash detection on next start, auto-restart wrapper in `Start.cmd`, bounded memory structures, DB pruning. |
| Windows | `Install.cmd` / `Start.cmd` / `Start-Demo.cmd` / `Run-Tests.cmd` (+ PowerShell scripts), desktop shortcuts, setup wizard. |
| Demo mode | Fake OBS server (driven through the real OBS client), mock vision, mock PriceCharting (driven through the real rate-limited client), separate demo database, port 8766. |

## 2. Tests that passed (this environment)

Run with `Run-Tests.cmd` (Windows) or `.venv/bin/python -m pytest -q && node --test "tests/js/*.test.mjs"`.

- **Python: 209 passed**, 6 skipped — the skips are the 5 opt-in live tests (no credentials here)
  and the opt-in docs-screenshot export. Includes 20 Playwright browser tests of the price strip
  (1080×1920, 720×1280, 1920×1080 and 360 px phone; long names and `$1,245.99` fit; whole-dollar
  rounding; no frame movement or transition on update; exact fonts; older-OBS unit fallback;
  self-reload after an app update).
- **JavaScript: 12 passed** (`node --test`).
- **Real-time demo soak:** the demo server ran the full pipeline continuously for ~33 minutes
  (fake OBS camera through the real WebSocket client, mock vision, mock PriceCharting through
  the real rate-limited client) at ~2.6 % CPU and ~146–152 MB RSS. See section 8 for the
  memory sampling after the review fixes.

Coverage of the required test list:

| # | Requirement | Where |
|---|---|---|
| 1 | Title/platform recognition with fixture images | `test_engine.py` (rendered fixture scenes through the full pipeline with a vision double), `test_recognizer.py` (real request construction with fixture images), `tests/live` (**real Claude on fixture labels — not run here**) |
| 2 | Ambiguous titles | `test_matcher.py::test_ambiguous_close_candidates`, `test_unknown_platform_never_auto_matches` |
| 3 | Alternate console releases | `test_matcher.py::test_same_title_different_consoles` (Mortal Kombat ×4), Resident Evil PS1/GC, Super Mario Bros vs 3 |
| 4 | Edition & regional variants | Player's Choice, Collector's, Greatest Hits, PAL N64 tests in `test_matcher.py` |
| 5 | Low-confidence recognition | `test_low_confidence_never_auto_matches`, obscured-title test, engine needs-confirmation tests |
| 6 | Replacement during a lookup | `test_engine.py::test_replacement_while_lookup_in_progress` |
| 7 | Out-of-order API responses | `test_engine.py::test_out_of_order_price_responses_discarded` |
| 8 | PriceCharting auth failure | `test_pricecharting_client.py` (401, error-message, missing token) |
| 9 | PriceCharting rate limiting | 1 req/s spacing under concurrency, 429 backoff/give-up |
| 10 | Missing values | `test_money.py`, client/engine/browser N/A tests |
| 11 | Cents → dollars | `test_money.py`, `overlay-core.test.mjs` |
| 12 | Cache expiration | `test_pricing_service.py` |
| 13 | OBS disconnect/reconnect | `test_obs_client.py` (restart on same port, auth failure, pending request fail-fast) |
| 14 | Overlay rendering | `tests/browser` at 1080×1920, 720×1280, 1920×1080: bounds, transparency, overflow, phone legibility |
| 15 | Manual corrections | matcher + engine correction-learning tests |
| 16 | Long-running operation | `test_engine.py::test_long_session_200_games_bounded_resources` (200 games, memory growth, task count, API call counts) |
| 17 | Missing credentials | recognizer + server startup tests |
| 18 | Stale pricing | service + engine stale tests (hide and labeled policies) |
| 19 | Title/price mismatch prevention | engine consistency tests, browser MutationObserver atomicity test |
| 20 | Unauthorized dashboard access | `test_server_security.py` (auth, CSRF, Origin, Host, forged cookies, WS) |

Design-brief additions: demo-data prevention, malformed records, hide/show, WebSocket reconnection
without refresh, hide after long disconnect, reduced motion, watermark.

## 3. What was verified against live services

**Nothing end-to-end.** Specifically:

- **PriceCharting:** the official API documentation was fetched and followed (endpoints, field
  names, pennies, 1 call/second limit, attribution guidance). No authenticated call was made.
- **Anthropic:** SDK 2.7.0 signatures for `beta.messages.parse` (`output_format`, `output_config`,
  `fallbacks`, `betas`) were inspected locally and the generated JSON schema was checked. No real
  API call was made — model behaviour, latency and cost per identification are **unmeasured**.
- **OBS:** the obs-websocket v5 protocol was implemented from its specification and tested against
  an in-process server implementing that protocol. Not tested against a real OBS.
- **Windows:** PowerShell/CMD scripts were written but **not executed** (no Windows here).

## 4. Hardware/integration steps still required (in this order)

1. `Install.cmd -WithTests` on the streaming PC; then `Run-Tests.cmd` — expect all green.
2. `Start-Demo.cmd` — confirm the dashboard and the watermarked demo overlay work on Windows.
3. Enter credentials (wizard). Run
   `set WGO_RUN_LIVE=1` and `Run-Tests.cmd -Live` (needs `ANTHROPIC_API_KEY`,
   `PRICECHARTING_TOKEN`, and for OBS `WGO_LIVE_OBS_SOURCE=Sony ZV-1` set in that console).
   This is the first proof that the real APIs work with this code.
4. `.venv\Scripts\python -m gameoverlay check --live`.
5. OBS: enable WebSocket, select the ZV-1 source, crop, capture background, add the Browser
   Source (docs/OBS_SETUP.md). Confirm `· 1 src` in the dashboard.
6. Rehearsal (not live on Whatnot): present 20–30 games, including same-title/different-console
   pairs, Player's Choice/Greatest Hits copies, a PAL copy, loose carts, a game with a sticker or
   glare. Note recognition time per game (dashboard *Total*), misidentifications and how often
   *Needs confirmation* appears. Tune thresholds if needed.
7. Check the vertical stream on an actual phone in the Whatnot app (legibility, and whether
   Whatnot's own UI covers the strip — adjust position/edge offset).
8. Run a 2–3 hour soak with OBS streaming to a private/test destination; watch CPU, memory and
   the spend estimate.

## 5. Unresolved limitations / risks

- **Lettering vs. the package's preview PNGs.** The package's `preview-*.png` files were rendered
  with fallback fonts (DejaVu); the live strip uses the exact families named in its font guide
  (Knewave, Barlow Condensed, Anton), so the lettering differs slightly from those PNGs while the
  frame, positions and sizes match.
- **Real-world recognition accuracy is unmeasured.** Thresholds (motion, stability, fingerprint
  distances, sharpness, accept score/margin) were tuned on synthetic images and need on-site tuning.
- **Latency target (2–5 s) is unverified.** It depends on Claude response time at effort `low`; the
  corroboration step (only when replacing a game with a non-high-confidence result) adds a second call.
- **Cost figures are estimates** from a built-in price table; the Anthropic invoice is authoritative.
- **Editions/regions** are only distinguished when visible on the item; PriceCharting variants
  that look identical to the standard release cannot be told apart from a photo.
- **UPC lookup** helps only when a barcode is legible (rare on retro carts); a USB scanner works
  via the dashboard.
- **Very long names** are narrowed horizontally (down to 20% width, the design's limit); a name
  that still does not fit is shortened with `…` rather than running over the price plates.
- **Attribution:** the strip has no source line or PriceCharting heading, at your request.
  PriceCharting's API terms say they *prefer* attribution when sharing their data under a
  commercial agreement; if your agreement requires on-screen attribution, it would need adding.
- **Whatnot's own top UI** (host name, viewer count) may overlap the strip on phones; adjust
  *Extra distance from the edge* after checking a real stream.
- **No HTTPS** — by design the app only listens on 127.0.0.1.
- **Docker** is not provided (not needed for normal use).

## 5a. Independent code review (done before release) and fixes

A separate reviewer read the critical paths without seeing this audit. All 12 findings were
fixed; each high-severity one has a regression test that fails on the previous code:

| # | Finding | Fix |
|---|---|---|
| 1 | A superseded job could still be shown if the on-air game returned before the other item's lookup finished. | The in-flight job is cancelled whenever its item leaves the camera; drift check also re-examines scenes that no longer match what's on air. |
| 2 | Any manual pick (e.g. showing a product for a buyer) was learned as a correction. | Only suggestion picks / search picks with *This corrects the item on camera*, for the same item generation, are learned. |
| 3 | Manual/recall selections taught visual memory. | Visual memory learns only from automatic identifications and explicit corrections. |
| 4 | An overlay client that missed an update could keep an old record while heartbeats continued. | Evicted sockets are closed (client reconnects and resyncs); clients also resync when heartbeats report a newer state. |
| 5 | Held/pending records skipped the freshness policy; releasing a hold left the held game up; on-air prices were never re-validated. | Pending records are re-priced on show; releasing a hold shows the pending game; on-air prices older than the cache duration are refreshed or taken off air. |
| 6 | Cancelling one caller of a shared price fetch failed the others. | Shared fetches are service-owned tasks awaited through `asyncio.shield`. |
| 7 | A legible UPC was accepted without a title cross-check. | UPC matches must agree with the printed title and console. |
| 8 | The admin token appeared in URLs/browser history and wasn't redacted from logs. | One-time sign-in codes, POST token login, token added to log redaction. |
| 9 | Rate limiter could start ~15 ms early on coarse timers. | Re-checks the clock after sleeping; hard floor 1.05 s. |
| 10 | Cancelled Claude calls weren't counted toward the spend limit. | Recorded at the session's average (or projected) cost. |
| 11 | Other web pages could exhaust overlay socket slots. | Overlay WebSocket rejects foreign Origins. |
| 12 | Double-click on a manual pick returned HTTP 500. | Returns 409 "superseded". |

## 6. Exact start instructions

1. Double-click `Install.cmd` (first time only).
2. Double-click `Start.cmd` (live) — or `Start-Demo.cmd` (demo, port 8766).
3. The dashboard opens automatically (one-time link, valid 15 minutes). Later, open
   `http://127.0.0.1:8765/login` and paste the token printed by
   `.venv\Scripts\python -m gameoverlay login-url`.
4. Keep the console window open during the show; Ctrl+C stops the app.

## 7. Exact OBS instructions (summary — full version in docs/OBS_SETUP.md)

1. OBS → Tools → WebSocket Server Settings → Enable, port 4455, copy the password into the
   dashboard (Settings → Credentials).
2. Camera source: untick *Deactivate when not showing*; select it in the dashboard; draw the crop.
3. Whatnot scene → Sources → + → **Browser**: URL `http://127.0.0.1:8765/overlay`, Width 1080,
   Height 1920 (= canvas), default Custom CSS, untick *Shutdown source when not visible* and
   *Refresh browser when scene becomes active*.
4. Put it above the camera; Transform → Reset Transform; size/position via the dashboard only.

## 8. Memory sampling (demo server, after the review fixes)

The demo server (full pipeline, ~2.5 samples/s, 104 recognition jobs, 64 overlay updates) was
sampled every 30 s for 12 minutes: RSS stayed between 144.5 and 157 MB. The resting baseline
moved from 144.5 MB to 144.9 MB (+0.4 MB); the short peaks are high-quality capture buffers that
are released. CPU averaged ~2.6 %. Visual memory held 10 entries (duplicates are not stored).
This is evidence that memory stays flat, not a substitute for the 2–3 hour soak with real OBS
in section 4.
