# Architecture

Single local Python process (FastAPI + asyncio) — no Docker, no cloud hosting, no OBS plugin.

```
                 ┌──────────────── private (admin session) ───────────────┐
 Browser ──────► │ /dashboard  /api/*  /ws/dashboard   (operator controls) │
                 └─────────────────────────────────────────────────────────┘
                 ┌──────────────── public, read-only ──────────────────────┐
 OBS Browser ──► │ /overlay  /overlay/static/*  /ws/overlay  /health      │
 Source          └─────────────────────────────────────────────────────────┘

 OBS (obs-websocket v5) ◄── OBSClient (read-only allowlist, auto-reconnect)
        │ GetSourceScreenshot(camera input)
        ▼
 Engine (state machine) ──► ClaudeRecognizer (Anthropic SDK, structured output, spend limits)
        │                └► Matcher ──► PricingService ──► PriceChartingClient ──► pricecharting.com
        │                                     │ caches (SQLite)       └ SerialRateLimiter (≤1 req/s)
        └──► OverlayHub (one atomic VerifiedRecord) ──► /ws/overlay ──► overlay.js
```

## Modules (`gameoverlay/`)

| Module | Responsibility |
|---|---|
| `config.py` | `AppConfig` (env/bootstrap: env, host, port, data dir, demo) and `RuntimeSettings` (validated operator settings persisted in SQLite). |
| `secrets_store.py` | Credentials: env var → OS keyring (Windows Credential Manager) → owner-only file fallback. Dashboard admin token. |
| `logging_setup.py` | Console + rotating JSON-lines logs with secret redaction. |
| `db.py` | SQLite (WAL) with versioned forward-only migrations; settings, sessions (crash detection), caches, history, fingerprints, API usage. |
| `money.py` | Integer-pennies validation and exact USD formatting (`N/A` for missing). |
| `models.py` | `Candidate`, `ProductPrices`, `VerifiedRecord` (the only thing the overlay can display). |
| `pricing/` | `client.py` (documented endpoints, strict parsing, retries/backoff, token scrubbing), `ratelimit.py` (serialized queue, ≥1 s spacing), `service.py` (price/search caches, freshness policy, in-flight de-dup). |
| `obs/client.py` | obs-websocket v5 protocol (Hello/Identify/auth, requests), read-only allowlist, reconnect with backoff, fail-fast on disconnect. |
| `vision/` | `imaging.py` (crop/rotate, quality, localized motion, grid fingerprints), `change.py` (stability detector), `platforms.py` (PriceCharting console families/regions), `schema.py` + `prompts.py` + `recognizer.py` (Claude Vision), `usage.py` (cost estimates, spend/rate caps). |
| `pipeline/matcher.py` | Candidate validation: platform family + region, title similarity with significant numbers, edition adjustments, margins, match cache (manual corrections win). |
| `pipeline/engine.py` | Recognition state machine and operator actions. |
| `overlay/hub.py` | Overlay state + read-only WebSocket broadcaster. |
| `security.py` | Host check (DNS rebinding), one-time sign-in codes, signed session cookies, CSRF, Origin checks, CSP/security headers. |
| `server.py` | Routes. `services.py` wires live vs. demo components. `__main__.py` CLI. |
| `demo/` | Fake OBS server, mock PriceCharting transport, mock vision, scene renderer, demo world, preview samples. |
| `web/overlay/` | The operator's price-strip package: `index.html`, `style.css`, `header-skin.png` (text-free frame art), bundled OFL fonts; `overlay-core.js` (pure logic: validation, price text, fit) and `overlay.js` (DOM, text fitting, WebSocket, self-reload after updates). |
| `web/dashboard/` | Operator UI. |

## State machine

`IDLE → DETECTING → RECOGNIZING → MATCHING → PRICE_LOOKUP → DISPLAYING`, plus
`NEEDS_CONFIRMATION`, `ERROR`, `PAUSED`.

- **IDLE**: waiting for OBS / camera selection (recovers automatically).
- **DETECTING**: sampling small screenshots (default every 400 ms); waiting for a stable frame.
- **RECOGNIZING**: high-quality burst, quality gate, visual memory, Claude Vision (second image
  if the first is obscured).
- **MATCHING**: PriceCharting candidate validation (match cache → UPC → search).
- **PRICE_LOOKUP**: cache or refresh; anti-flicker minimum display time.
- **DISPLAYING**: record on air; same item re-posed → no action; different item → new job + grace timer.
- **NEEDS_CONFIRMATION**: ambiguous/low confidence/no match — operator picks from suggestions.
- **ERROR**: job errors stay visible until the next item/action; capture hiccups recover by themselves.
- **PAUSED**: operator pause, or Claude spend limit reached.

## Safety invariants (each covered by tests)

1. The overlay only ever receives a `VerifiedRecord` built from **one** PriceCharting product
   response; title, console and all three prices are replaced in one assignment and rendered in
   one synchronous DOM update.
2. Every job carries a generation number; results of superseded jobs are never committed.
3. No price is shown without a validated PriceCharting product; Claude output never becomes a price.
4. Missing values are `N/A`; zero/negative/malformed values are treated as missing.
5. Stale prices are never presented as current (hidden, or labeled `CACHED` by explicit policy).
6. Demo records are rejected by the hub in live mode; sample values exist only in the demo
   catalog and the authenticated preview endpoint.
7. ≤ 1 PriceCharting request per second, globally serialized.
8. Secrets never reach browser clients, URLs (except the token-in-query to pricecharting.com
   itself) or logs.
9. The OBS client cannot send state-changing requests.
10. When the item being identified leaves the camera (including when the on-air game returns),
    its job is cancelled before it can commit.
11. Only automatic identifications and explicit operator corrections of the current item teach
    the match cache / visual memory; buyer requests, UPC lookups and recalls never do.
12. Records shown after a hold, and records left on air past the price cache duration, are
    re-validated through the freshness policy first.
13. An overlay client that misses an update is disconnected so it reconnects and resyncs.

## Data flow of a record (wire format to the overlay)

```json
{"type": "state", "seq": 17, "visible": true, "demoMode": false,
 "record": {"title": "Zelda Ocarina of Time", "platform": "NINTENDO 64", "productId": "…",
            "verified": true, "looseUsd": 42.5, "completeUsd": 109.99, "newUsd": 599.0,
            "looseCents": 4250, "completeCents": 10999, "newCents": 59900,
            "retrievedAt": "2026-11-10T14:32:00Z", "freshness": "fresh", "demo": false},
 "layout": {"position": "top-center", "edgeOffset": 0, "idlePanel": true, "roundWholeDollars": true, "…": "…"},
 "build": "<overlay files fingerprint>"}
```

The overlay formats from the integer cents; it rejects records where cents and USD disagree.
`window.setGameOverlayData(record)` accepts the same record shape (the design brief's contract).
