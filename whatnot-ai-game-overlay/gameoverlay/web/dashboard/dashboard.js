// Price-O-Matic operator dashboard (private; requires the local admin session).
// All dynamic content is inserted with textContent / DOM nodes — never innerHTML.

const $ = (id) => document.getElementById(id);

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

// ------------------------------------------------------------------ API
function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)wgo_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : "";
}

async function api(path, { method = "GET", body } = {}) {
  const opts = { method, headers: {}, credentials: "same-origin" };
  if (method !== "GET") opts.headers["X-CSRF-Token"] = csrf();
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  if (res.status === 401) {
    location.href = "/login";
    throw new Error("signed out");
  }
  let data = null;
  try {
    data = await res.json();
  } catch {}
  if (!res.ok) {
    const d = data && data.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(-2).join(".")}: ${e.msg}`).join("; ") : `HTTP ${res.status}`;
    const err = new Error(msg);
    err.detail = d;
    throw err;
  }
  return data;
}

let toastTimer;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.toggle("err", isErr);
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), isErr ? 6000 : 2500);
}

async function act(fn, okMsg) {
  try {
    const r = await fn();
    if (okMsg) toast(okMsg);
    return r;
  } catch (e) {
    toast(e.message || String(e), true);
    return null;
  }
}

// ------------------------------------------------------------------ state
let status = null;
let settings = null;
let lastFrameGen = -1;
let lastHistorySeq = -1;
let suggestions = [];
let searchResults = [];
let lastEngineState = null;

const fmtTime = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");
const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");

// ------------------------------------------------------------------ status rendering
function pill(label, value, cls) {
  return h("span", { class: `pill ${cls}` }, h("span", { text: label }), h("b", { text: value }));
}

function renderPills(s) {
  const p = $("pills");
  const obsCls = s.obs.state === "connected" ? "ok" : s.obs.state === "auth_failed" ? "err" : "warn";
  const cam = s.obs.cameraSource || "not selected";
  const ai = s.anthropic.state;
  const aiCls = ["connected", "demo", "idle"].includes(ai) ? (ai === "idle" ? "" : "ok") : ai === "not_configured" || ai === "auth_failed" || ai === "limit_reached" ? "err" : "warn";
  const pc = s.pricecharting.state;
  const pcCls = pc === "connected" ? "ok" : pc === "idle" ? "" : pc === "error" ? "warn" : "err";
  const eng = s.engine;
  const detCls = eng.detectionEnabled ? "ok" : "warn";
  const ov = s.overlay;
  const usage = s.anthropic.usage || {};
  const spend = usage.limitUsd ? `$${(usage.estimatedCostUsd || 0).toFixed(2)} / $${usage.limitUsd.toFixed(2)}` : "—";
  p.replaceChildren(
    pill("OBS", s.obs.state.replace("_", " "), obsCls),
    pill("Camera", cam, !s.obs.cameraSource ? "warn" : cameraFailing(s) ? "err" : "ok"),
    pill("Claude", ai.replace("_", " "), aiCls),
    pill("PriceCharting", pc.replace("_", " "), pcCls),
    pill("Detection", eng.detectionEnabled ? "active" : "paused", detCls),
    pill("Overlay", `${ov.visible ? "game shown" : "idle"} · ${ov.clients} src`, ov.clients > 0 ? "ok" : "warn"),
    pill("Spend (est.)", spend, usage.limitReached ? "err" : ""),
  );
}

function cameraFailing(s) {
  return s.engine.state === "error" && /camera capture failed/i.test(s.engine.reason || "");
}

const NOTICE_TTL_S = 120;

function noticeDismissed(key) {
  try {
    return localStorage.getItem(`dismissed:${key}`) === "1";
  } catch {
    return false;
  }
}

function dismissNotice(key) {
  try {
    localStorage.setItem(`dismissed:${key}`, "1");
  } catch {}
  if (status) renderBanner(status);
}

function renderBanner(s) {
  const msgs = [];
  const notices = []; // informational, dismissible: [key, text]
  if (cameraFailing(s)) {
    msgs.push(`${s.engine.reason}. In OBS, check that “${s.obs.cameraSource}” shows live video, untick “Deactivate when not showing” in its Properties, and close any other app using the camera.`);
  }
  if (s.mode === "demo") msgs.push("DEMO MODE — mock camera, mock Claude and mock PriceCharting data. Everything shown is watermarked sample data.");
  const crashKey = `crash:${s.session.id}`;
  if (s.session.recoveredFromCrash && Date.now() / 1000 - s.session.startedAt < NOTICE_TTL_S && !noticeDismissed(crashKey)) {
    notices.push([crashKey, "The app wasn't closed cleanly last time (for example, the window was closed). Settings and caches were restored."]);
  }
  if (s.mode !== "demo") {
    if (!s.secrets.anthropic_api_key.configured) msgs.push("Anthropic API key missing — automatic recognition is disabled (manual search still works).");
    if (!s.secrets.pricecharting_token.configured) msgs.push("PriceCharting token missing — no prices can be retrieved.");
    if (s.secrets.backend === "file") msgs.push("No OS keyring available: credentials are stored in the app data folder file (owner-only).");
  }
  if (s.pricecharting.state === "auth_failed") msgs.push("PriceCharting rejected the API token. Update it in Settings → Credentials.");
  if (s.anthropic.state === "auth_failed") msgs.push("Anthropic rejected the API key. Update it in Settings → Credentials.");
  if (s.anthropic.usage && s.anthropic.usage.limitReached) msgs.push("Claude session spend limit reached — detection paused. Raise the limit in Settings → Recognition to continue.");
  const b = $("banner");
  b.hidden = msgs.length === 0 && notices.length === 0;
  b.classList.toggle("err", msgs.some((m) => /rejected|missing|limit|capture failed/i.test(m)));
  b.replaceChildren(
    ...msgs.map((m) => h("div", { text: m })),
    ...notices.map(([key, text]) =>
      h("div", { class: "notice" }, h("span", { text }), h("button", { class: "ghost small", title: "Dismiss", onclick: () => dismissNotice(key) }, "Dismiss")),
    ),
  );
}

function renderCurrent(s) {
  const eng = s.engine;
  const cur = eng.current;
  const chip = $("stateChip");
  chip.textContent = eng.state.replace("_", " ");
  chip.className = `chip ${eng.state}`;
  let st = cur.status || eng.reason || "";
  if (eng.hideInS !== null && eng.hideInS !== undefined && s.overlay.visible) st += ` · overlay hides in ${eng.hideInS}s unless identified`;
  if (eng.held) st += " · HELD";
  $("statusText").textContent = st;

  // frame
  if (cur.hasFrame && cur.generation !== lastFrameGen) {
    lastFrameGen = cur.generation;
    const img = $("frameImg");
    img.src = `/api/frame/current.jpg?g=${cur.generation}&t=${Date.now()}`;
    img.hidden = false;
  }
  const q = cur.quality;
  $("frameCaption").textContent = q ? `sharpness ${q.sharpness} · brightness ${q.brightness} · ${q.width}×${q.height}` : "No capture yet";

  // recognition
  const r = cur.recognition;
  const meta = cur.recognitionMeta;
  const kv = $("recognition");
  const rows = [];
  if (r) {
    rows.push(["Title", r.title || "—"], ["Platform", r.platform || "—"], ["Region", r.region], ["Edition", r.edition || "standard"], ["Confidence", `${r.confidence}${r.title_obscured ? " (obscured)" : ""}`]);
  }
  if (meta) rows.push(["Vision", `${meta.status} · ${meta.latencyMs} ms${meta.costUsd ? ` · $${meta.costUsd.toFixed(4)}` : ""}`]);
  if (cur.timingsMs && cur.timingsMs.total) rows.push(["Total", `${cur.timingsMs.total} ms`]);
  kv.replaceChildren(...rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: String(v) })]));

  // match
  const m = cur.match;
  const matched = $("matched");
  if (m && m.product) {
    matched.replaceChildren(
      h("div", {}, h("strong", { text: m.product.productName }), " · ", m.product.consoleName),
      h("small", {}, `via ${m.via}${m.score ? ` · score ${m.score}` : ""} · id `, h("a", { href: `https://www.pricecharting.com/game/${encodeURIComponent(m.product.productId)}`, target: "_blank", rel: "noopener noreferrer", text: m.product.productId })),
    );
  } else if (m) {
    matched.replaceChildren(h("div", { text: `${m.status.replace("_", " ")}: ${m.reason || ""}` }));
  } else {
    matched.replaceChildren(h("div", { class: "hint", text: "No product matched yet" }));
  }

  // prices: prefer the record actually on air
  const rec = s.overlay.record;
  const pr = cur.prices;
  const show = rec ? { loose: fmtCents(rec.looseCents), complete: fmtCents(rec.completeCents), new: fmtCents(rec.newCents) } : pr ? { loose: pr.loose, complete: pr.complete, new: pr.new } : null;
  $("pricesCaption").textContent = rec
    ? `ON AIR: ${rec.title} · ${rec.platform}`
    : pr
      ? `Matched (not on air): ${pr.productName} · ${pr.consoleName}`
      : "Nothing on air";
  $("pricesCaption").classList.toggle("onair", !!rec);
  $("pLoose").textContent = show ? show.loose : "—";
  $("pComplete").textContent = show ? show.complete : "—";
  $("pNew").textContent = show ? show.new : "—";
  const fr = $("freshness");
  const fresh = rec ? rec.freshness : cur.freshness;
  const when = rec ? rec.retrievedAt : pr && pr.retrievedAt;
  fr.replaceChildren();
  if (fresh) fr.append(h("span", { class: `fresh-badge ${fresh}`, text: fresh.toUpperCase() }), ` retrieved ${fmtDate(when)}`);
  if (cur.stalePrices) fr.append(h("div", { class: "hint", text: `Stale cached prices (NOT shown on air): ${cur.stalePrices.loose} / ${cur.stalePrices.complete} / ${cur.stalePrices.new} from ${fmtDate(cur.stalePrices.retrievedAt)}` }));
  if (cur.error) fr.append(h("div", { class: "hint", text: `Error: ${cur.error}` }));

  // corrections: pre-tick "teach" while the pipeline is asking for confirmation
  if (eng.state !== lastEngineState) {
    $("teachChk").checked = eng.state === "needs_confirmation";
    lastEngineState = eng.state;
  }

  // suggestions (needs confirmation)
  suggestions = (m && m.suggestions) || [];
  const wrap = $("suggestionsWrap");
  const showSugg = eng.state === "needs_confirmation" && suggestions.length > 0;
  wrap.hidden = !showSugg;
  if (showSugg) {
    $("suggestions").replaceChildren(
      ...suggestions.slice(0, 9).map((sg, i) =>
        h("li", { class: sg.platformMatch && sg.regionMatch ? "eligible" : "" },
          h("button", { onclick: () => selectProduct(sg.productId, true) },
            h("span", { class: "n", text: String(i + 1) }),
            h("span", {}, sg.productName, h("div", { class: "meta", text: sg.consoleName })),
            h("span", { class: "meta", text: `${sg.score}` }))),
      ),
    );
  }
  $("evidence").textContent = r ? JSON.stringify({ ...r, meta }, null, 2) : "";

  // buttons
  $("btnPause").firstChild.textContent = eng.detectionEnabled ? "Pause detection " : "Resume detection ";
  $("btnPause").classList.toggle("on", !eng.detectionEnabled);
  $("btnHold").firstChild.textContent = eng.held ? "Release hold " : "Hold game ";
  $("btnHold").classList.toggle("on", eng.held);
  $("btnPending").hidden = !eng.pendingWhileHeld;
  if (eng.pendingWhileHeld) $("btnPending").textContent = `Show new game: ${eng.pendingWhileHeld.title}`;

  const u = s.anthropic.usage || {};
  $("usage").textContent = `Claude: ${u.requests || 0} requests · ${u.errors || 0} errors · est. $${(u.estimatedCostUsd || 0).toFixed(3)} this session · PriceCharting: ${s.pricecharting.requestsTotal} calls (queue ${s.pricecharting.queueDepth})`;
}

function fmtCents(c) {
  if (c === null || c === undefined || c <= 0) return "N/A";
  const d = Math.floor(c / 100);
  return "$" + String(d).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + String(c % 100).padStart(2, "0");
}

function renderStatus(s) {
  status = s;
  const mb = $("modeBadge");
  mb.textContent = s.mode === "demo" ? "DEMO" : "LIVE";
  mb.className = `badge ${s.mode}`;
  $("obsUrl").textContent = s.overlay.url;
  renderPills(s);
  renderBanner(s);
  renderCurrent(s);
  if (s.overlay.seq !== lastHistorySeq && s.overlay.visible) {
    lastHistorySeq = s.overlay.seq;
    loadHistory();
  }
  if (!s.setupCompleted && !wizardShownOnce) {
    wizardShownOnce = true;
    openWizard();
  }
}

// ------------------------------------------------------------------ actions
async function selectProduct(pid, correction = false) {
  const generation = status ? status.engine.current.generation : null;
  const r = await act(() => api("/api/select", { method: "POST", body: { productId: pid, correction, generation } }));
  if (r && r.record) toast(`On air: ${r.record.title}`);
  $("searchResults").replaceChildren();
}

async function doSearch(q) {
  q = q.trim();
  if (!q) return;
  const box = $("searchResults");
  box.replaceChildren(h("li", { class: "hint", text: "Searching… (PriceCharting requests are rate-limited to 1/second)" }));
  const r = await act(() => api(`/api/search?q=${encodeURIComponent(q)}`));
  if (!r) {
    box.replaceChildren();
    return;
  }
  const seen = new Set();
  searchResults = [...r.local, ...r.remote].filter((c) => (seen.has(c.productId) ? false : seen.add(c.productId)));
  const items = searchResults.slice(0, 20).map((c, i) =>
    h("li", {}, h("button", { onclick: () => selectProduct(c.productId, $("teachChk").checked) },
      h("span", { class: "n", text: i < 9 ? String(i + 1) : "" }),
      h("span", {}, c.productName, h("div", { class: "meta", text: c.consoleName })),
      h("span", { class: "meta", text: `#${c.productId}` }))));
  if (r.error) items.unshift(h("li", { class: "hint", text: r.error }));
  if (!items.length) items.push(h("li", { class: "hint", text: "No results" }));
  box.replaceChildren(...items);
}

async function loadHistory() {
  const r = await act(() => api("/api/history?limit=100"));
  if (!r) return;
  $("historyBody").replaceChildren(
    ...r.items.map((it) =>
      h("tr", {},
        h("td", { text: fmtTime(it.shownAt) }),
        h("td", { class: "prod", text: it.productName }),
        h("td", { text: it.consoleName }),
        h("td", { class: "num", text: it.loose }),
        h("td", { class: "num", text: it.complete }),
        h("td", { class: "num", text: it.new }),
        h("td", { text: `${it.source}${it.freshness === "stale" ? " (stale)" : ""}` }),
        h("td", {}, h("a", { href: `https://www.pricecharting.com/game/${encodeURIComponent(it.productId)}`, target: "_blank", rel: "noopener noreferrer", text: it.productId })),
        h("td", {}, h("button", { class: "small", onclick: () => act(() => api(`/api/history/${it.id}/recall`, { method: "POST" }), `Recalled ${it.productName}`) }, "Recall")),
      ),
    ),
  );
}

// ------------------------------------------------------------------ preview
let designMode = false;
let canvas = { w: 1080, h: 1920 };

function sizePreview() {
  const box = $("previewBox");
  const landscape = canvas.w > canvas.h;
  box.classList.toggle("landscape", landscape);
  const bw = box.clientWidth;
  const scale = bw / canvas.w;
  for (const f of [$("liveFrame"), $("designFrame")]) {
    f.style.width = canvas.w + "px";
    f.style.height = canvas.h + "px";
    f.style.transform = `scale(${scale})`;
  }
}

async function sendDesignSample() {
  const variant = $("sampleSel").value;
  const r = await act(() => api(`/api/preview/${variant}`));
  if (!r) return;
  $("designFrame").contentWindow.postMessage({ type: "preview", record: r.record, layout: r.layout }, location.origin);
}

function setPreviewTab(design) {
  designMode = design;
  $("tabLive").classList.toggle("on", !design);
  $("tabDesign").classList.toggle("on", design);
  $("liveFrame").hidden = design;
  $("designFrame").hidden = !design;
  $("designControls").hidden = !design;
  if (design && !$("designFrame").src) $("designFrame").src = "/overlay?preview=1";
  else if (design) sendDesignSample();
}

window.addEventListener("message", (e) => {
  if (e.origin !== location.origin) return;
  if (e.data && e.data.type === "preview-ready" && designMode) sendDesignSample();
});

// ------------------------------------------------------------------ settings
const SETTINGS_TABS = {
  Credentials: "credentials",
  "OBS & Camera": [
    { path: "obs.host", label: "OBS WebSocket host", type: "text", help: "Usually 127.0.0.1 (OBS on this PC). OBS → Tools → WebSocket Server Settings." },
    { path: "obs.port", label: "OBS WebSocket port", type: "number", min: 1, max: 65535 },
    { path: "obs.camera_source", label: "Camera source", type: "source", help: "Pick your Sony ZV-1 input (e.g. a Video Capture Device). Never pick the overlay browser source or a scene." },
    { path: "obs.rotation", label: "Rotation", type: "select", options: [0, 90, 180, 270], help: "Rotate a sideways-mounted camera so text reads upright." },
    { path: "obs.crop", label: "Presentation area (crop)", type: "crop", help: "Drag the rectangle around where you hold games. Recognition only looks inside it." },
    { path: "obs.still_max_width", label: "Still width (px, 0 = native)", type: "number", min: 0, max: 4096 },
    { type: "background" },
  ],
  Recognition: [
    { path: "recognition.model", label: "Claude model", type: "text", help: "Default claude-opus-5-5." },
    { path: "recognition.effort", label: "Effort", type: "select", options: ["low", "medium", "high", "xhigh", "max"], help: "Higher effort is slower and costs more." },
    { path: "detection.sample_interval_ms", label: "Sampling interval (ms)", type: "number", min: 100, max: 5000, step: 50 },
    { path: "detection.motion_threshold", label: "Motion sensitivity (lower = more sensitive)", type: "number", min: 0.001, max: 0.5, step: 0.005 },
    { path: "detection.stable_frames_required", label: "Calm samples before recognizing", type: "number", min: 1, max: 20 },
    { path: "detection.change_threshold", label: "New-item threshold", type: "number", min: 0.02, max: 0.9, step: 0.01, help: "How different a frame must be from the displayed game to count as a new item." },
    { path: "detection.same_item_threshold", label: "Visual-memory threshold", type: "number", min: 0, max: 0.5, step: 0.01 },
    { path: "detection.visual_memory_enabled", label: "Reuse identification of returning games", type: "checkbox" },
    { path: "detection.min_sharpness", label: "Minimum sharpness", type: "number", min: 0, max: 5000, step: 5 },
    { path: "detection.burst_frames", label: "Stills per capture", type: "number", min: 1, max: 5 },
    { path: "recognition.accept_score", label: "Auto-accept title score", type: "number", min: 50, max: 100, step: 1 },
    { path: "recognition.accept_margin", label: "Required lead over runner-up", type: "number", min: 0, max: 50, step: 1 },
    { path: "recognition.min_confidence", label: "Minimum confidence", type: "select", options: ["low", "medium", "high"] },
    { path: "recognition.require_corroboration", label: "Corroborate before replacing a game", type: "checkbox" },
    { path: "recognition.default_region", label: "Default region", type: "select", options: ["NTSC-U", "PAL", "NTSC-J"] },
    { path: "recognition.session_spend_limit_usd", label: "Session spend limit (USD)", type: "number", min: 0, max: 1000, step: 0.5 },
    { path: "recognition.max_requests_per_minute", label: "Max Claude requests / minute", type: "number", min: 1, max: 120 },
    { path: "recognition.use_server_fallbacks", label: "Refusal fallbacks (server-side)", type: "checkbox" },
  ],
  "Display & Overlay": [
    { path: "overlay.position", label: "Position", type: "select", options: ["top-center", "bottom-center"], help: "The price strip spans the width of the frame; top is the default." },
    { path: "overlay.edge_offset", label: "Extra distance from the edge (fraction of height)", type: "number", min: 0, max: 0.3, step: 0.005, help: "0 = exactly as designed. Raise it (e.g. 0.03) if Whatnot's own buttons cover the strip." },
    { path: "overlay.opacity", label: "Opacity", type: "number", min: 0.3, max: 1, step: 0.05 },
    { path: "overlay.idle_panel", label: "Keep the strip on screen between games", type: "checkbox", help: "When a game leaves, the strip stays up with its prices cleared. Untick to hide it completely between games." },
    { path: "overlay.idle_title", label: "Text shown between games", type: "text", help: "Shown in the title spot while no game is on screen (leave empty for none)." },
    { path: "overlay.round_whole_dollars", label: "Round prices to whole dollars on the overlay", type: "checkbox", help: "On: $42.50 shows as $43. Off: exact cents. Your dashboard and history always show exact cents." },
    { path: "overlay.canvas_width", label: "OBS canvas width (preview)", type: "number", min: 320, max: 7680 },
    { path: "overlay.canvas_height", label: "OBS canvas height (preview)", type: "number", min: 320, max: 7680 },
    { path: "display.unidentified_grace_s", label: "Hide old prices after unidentified item (s)", type: "number", min: 0, max: 120, step: 0.5 },
    { path: "display.empty_grace_s", label: "Hide after empty presentation area (s, 0 = never)", type: "number", min: 0, max: 600, step: 1 },
    { path: "display.display_timeout_s", label: "Display timeout (s, 0 = none)", type: "number", min: 0, max: 3600, step: 5 },
    { path: "display.min_display_s", label: "Minimum time on screen (s)", type: "number", min: 0, max: 30, step: 0.5 },
    { path: "display.overlay_disconnect_hide_s", label: "Overlay hides after backend lost (s)", type: "number", min: 5, max: 3600, step: 5 },
  ],
  Pricing: [
    { path: "pricing.price_refresh_s", label: "Price cache duration (s)", type: "number", min: 60, max: 604800, step: 60, help: "Cached prices older than this are refreshed before display (default 3600 = 1 hour)." },
    { path: "pricing.stale_policy", label: "If a refresh fails", type: "select", options: ["hide", "show_labeled"], help: "hide = never show stale prices; show_labeled = show with a CACHED label (up to the max age)." },
    { path: "pricing.max_stale_s", label: "Max age for labeled stale prices (s)", type: "number", min: 60, max: 2592000, step: 60 },
    { path: "pricing.min_request_interval_s", label: "Seconds between PriceCharting requests (min 1.05)", type: "number", min: 1.05, max: 60, step: 0.05 },
    { path: "pricing.max_retries", label: "Retries", type: "number", min: 0, max: 6 },
    { path: "pricing.search_cache_s", label: "Search cache duration (s)", type: "number", min: 0, max: 7776000, step: 3600 },
  ],
  Logging: [{ path: "log_level", label: "Log level", type: "select", options: ["DEBUG", "INFO", "WARNING", "ERROR"] }],
};

const getPath = (obj, path) => path.split(".").reduce((o, k) => (o == null ? o : o[k]), obj);
function patchFor(path, value) {
  const keys = path.split(".");
  const out = {};
  let cur = out;
  keys.forEach((k, i) => {
    if (i === keys.length - 1) cur[k] = value;
    else cur = cur[k] = {};
  });
  return out;
}

async function saveSetting(path, value, errEl) {
  try {
    settings = await api("/api/settings", { method: "PATCH", body: patchFor(path, value) });
    if (errEl) errEl.textContent = "";
    syncQuickLayout();
    toast("Saved");
    return true;
  } catch (e) {
    if (errEl) errEl.textContent = e.message;
    else toast(e.message, true);
    return false;
  }
}

function fieldEl(f) {
  if (f.type === "background") return backgroundField();
  const value = getPath(settings, f.path);
  const err = h("div", { class: "err" });
  let input;
  if (f.type === "select") {
    input = h("select", {}, ...f.options.map((o) => h("option", { value: String(o), selected: String(o) === String(value), text: String(o) })));
    input.addEventListener("change", async () => {
      const ok = await saveSetting(f.path, typeof f.options[0] === "number" ? Number(input.value) : input.value, err);
      if (ok && f.path === "obs.rotation") document.dispatchEvent(new Event("camera-changed"));
    });
  } else if (f.type === "checkbox") {
    input = h("input", { type: "checkbox" });
    input.checked = !!value;
    input.addEventListener("change", () => saveSetting(f.path, input.checked, err));
  } else if (f.type === "number") {
    input = h("input", { type: "number", min: f.min, max: f.max, step: f.step || 1, value });
    input.addEventListener("change", () => saveSetting(f.path, Number(input.value), err));
  } else if (f.type === "source") {
    input = h("select", {}, h("option", { value: "", text: value || "(choose…)" }));
    loadSources(input, value);
    input.addEventListener("change", async () => {
      if (input.value && (await saveSetting(f.path, input.value, err))) document.dispatchEvent(new Event("camera-changed"));
    });
  } else if (f.type === "crop") {
    return h("div", { class: "field wide" }, h("label", { text: f.label }), cropEditor(value, err), h("div", { class: "help", text: f.help || "" }), err);
  } else {
    input = h("input", { type: "text", value: value ?? "" });
    input.addEventListener("change", () => saveSetting(f.path, input.value.trim() === "" && f.path.endsWith("camera_source") ? null : input.value, err));
  }
  return h("div", { class: "field" }, h("label", { text: f.label }), input, f.help ? h("div", { class: "help", text: f.help }) : null, err);
}

async function loadSources(select, current) {
  try {
    const r = await api("/api/obs/sources");
    select.replaceChildren(
      h("option", { value: "", text: "(choose…)" }),
      ...r.inputs.map((i) =>
        h("option", { value: i.name, selected: i.name === current, disabled: !i.selectable, text: `${i.name} — ${i.kind}${i.isCamera ? " (camera)" : ""}${i.selectable ? "" : " (not allowed)"}` })),
    );
  } catch (e) {
    select.replaceChildren(h("option", { value: current || "", text: current ? `${current} (OBS offline)` : `OBS not connected: ${e.message}` }));
  }
}

function backgroundField() {
  const st = h("span", { class: "help", text: status && status.engine.hasBackground ? "Empty-background reference captured ✓" : "No empty-background reference" });
  return h("div", { class: "field" },
    h("label", { text: "Empty presentation area" }),
    h("div", { class: "row" },
      h("button", { class: "small", onclick: async () => { if (await act(() => api("/api/background/capture", { method: "POST" }), "Captured empty background")) st.textContent = "Empty-background reference captured ✓"; } }, "Capture now"),
      h("button", { class: "small ghost", onclick: async () => { await act(() => api("/api/background", { method: "DELETE" }), "Cleared"); st.textContent = "No empty-background reference"; } }, "Clear")),
    h("div", { class: "help", text: "With nothing in front of the camera, capture a reference. The app then knows the area is empty without asking Claude (saves cost)." }),
    st);
}

async function loadPreview(img, err) {
  // fetch() instead of <img src> so the server's explanation can be shown when it fails
  err.textContent = "Loading camera preview…";
  err.classList.remove("err-ok");
  try {
    const res = await fetch(`/api/camera/preview.jpg?t=${Date.now()}`, { credentials: "same-origin" });
    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        if (j && j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {}
      img.hidden = true;
      err.textContent = `Camera preview unavailable: ${msg}`;
      return;
    }
    const blob = await res.blob();
    if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
    img.dataset.url = URL.createObjectURL(blob);
    img.src = img.dataset.url;
    img.hidden = false;
    err.textContent = res.headers.get("X-Preview-Warning") || "";
  } catch (e) {
    img.hidden = true;
    err.textContent = `Camera preview unavailable: ${e.message}`;
  }
}

function cropEditor(crop, err) {
  const img = h("img", { alt: "Camera preview", hidden: true });
  const rect = h("div", { class: "crop-rect" }, h("div", { class: "handle" }));
  const box = h("div", { class: "crop-editor" }, img, rect);
  let c = { ...crop };
  const place = () => {
    rect.style.left = c.x * 100 + "%";
    rect.style.top = c.y * 100 + "%";
    rect.style.width = c.w * 100 + "%";
    rect.style.height = c.h * 100 + "%";
  };
  place();
  loadPreview(img, err);
  // reload automatically when the camera source / rotation is changed elsewhere on the page
  const onSourceChange = () => (document.body.contains(box) ? loadPreview(img, err) : document.removeEventListener("camera-changed", onSourceChange));
  document.addEventListener("camera-changed", onSourceChange);
  let drag = null;
  rect.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    rect.setPointerCapture(e.pointerId);
    drag = { mode: e.target.classList.contains("handle") ? "resize" : "move", sx: e.clientX, sy: e.clientY, start: { ...c } };
  });
  rect.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const b = box.getBoundingClientRect();
    const dx = (e.clientX - drag.sx) / b.width;
    const dy = (e.clientY - drag.sy) / b.height;
    const s = drag.start;
    if (drag.mode === "move") {
      c.x = Math.min(1 - s.w, Math.max(0, s.x + dx));
      c.y = Math.min(1 - s.h, Math.max(0, s.y + dy));
    } else {
      c.w = Math.min(1 - s.x, Math.max(0.05, s.w + dx));
      c.h = Math.min(1 - s.y, Math.max(0.05, s.h + dy));
    }
    place();
  });
  rect.addEventListener("pointerup", () => {
    if (!drag) return;
    drag = null;
    const round = (v) => Math.round(v * 1000) / 1000;
    c = { x: round(c.x), y: round(c.y), w: round(c.w), h: round(c.h) };
    saveSetting("obs.crop", c, err);
  });
  const reset = h("button", { class: "small ghost", onclick: () => { c = { x: 0, y: 0, w: 1, h: 1 }; place(); saveSetting("obs.crop", c, err); } }, "Use full frame");
  const refresh = h("button", { class: "small ghost", onclick: () => loadPreview(img, err) }, "Refresh image");
  return h("div", {}, box, h("div", { class: "row", style: "margin-top:6px" }, reset, refresh));
}

async function credentialsPane() {
  const st = await api("/api/secrets");
  const mk = (name, label, help) => {
    const s = st[name];
    const input = h("input", { type: "password", placeholder: s.configured ? "•••••••• (configured — enter to replace)" : "not set", autocomplete: "off" });
    const err = h("div", { class: "err" });
    const state = h("span", { class: `secret-state ${s.configured ? "ok" : "missing"}`, text: s.configured ? `configured (${s.source})` : "missing" });
    const save = h("button", { class: "small", onclick: async () => {
      if (!input.value.trim()) return;
      try {
        const r = await api("/api/secrets", { method: "PUT", body: { [name]: input.value.trim() } });
        input.value = "";
        state.textContent = r[name].configured ? `configured (${r[name].source})` : "missing";
        state.className = `secret-state ${r[name].configured ? "ok" : "missing"}`;
        toast("Saved securely");
      } catch (e) { err.textContent = e.message; }
    } }, "Save");
    return h("div", { class: "field" }, h("label", {}, label, " ", state), h("div", { class: "row" }, input, save), h("div", { class: "help", text: help }), err);
  };
  return h("div", {},
    h("p", { class: "hint", text: `Credentials are write-only here and stored in ${st.backend === "keyring" ? "the Windows Credential Manager (OS keyring)" : "an owner-only file in the app data folder"}. They are never sent to the overlay or shown again.` }),
    mk("anthropic_api_key", "Anthropic API key", "From console.anthropic.com → API Keys. Used only by this PC to call Claude Vision."),
    mk("pricecharting_token", "PriceCharting API token", "40-character token from PriceCharting → Subscription → API/Download."),
    mk("obs_password", "OBS WebSocket password", "OBS → Tools → WebSocket Server Settings → Show Connect Info."),
    h("div", { class: "row" }, h("button", { class: "small", onclick: async () => { const r = await act(() => api("/api/obs/reconnect", { method: "POST" })); if (r) toast(r.ok ? "OBS connected" : `OBS: ${r.state} ${r.lastError || ""}`, !r.ok); } }, "Test OBS connection")),
  );
}

let currentTab = "Credentials";
async function renderSettings() {
  settings = await api("/api/settings");
  $("settingsTabs").replaceChildren(
    ...Object.keys(SETTINGS_TABS).map((t) => h("button", { class: t === currentTab ? "on" : "", onclick: () => { currentTab = t; renderSettings(); } }, t)),
  );
  const body = $("settingsBody");
  const def = SETTINGS_TABS[currentTab];
  if (def === "credentials") body.replaceChildren(await credentialsPane());
  else body.replaceChildren(...def.map(fieldEl));
}

function syncQuickLayout() {
  if (!settings) return;
  for (const sel of document.querySelectorAll("[data-setting]")) sel.value = getPath(settings, sel.dataset.setting);
  const key = `${settings.overlay.canvas_width}x${settings.overlay.canvas_height}`;
  const cs = $("canvasSel");
  if (![...cs.options].some((o) => o.value === key)) cs.append(h("option", { value: key, text: key.replace("x", "×") }));
  cs.value = key;
  canvas = { w: settings.overlay.canvas_width, h: settings.overlay.canvas_height };
  sizePreview();
  if (designMode) sendDesignSample();
}

// ------------------------------------------------------------------ setup wizard
let wizardShownOnce = false;
let wizStep = 0;
const WIZ = [
  { title: "1 · Credentials", render: async () => credentialsPane() },
  {
    title: "2 · Connect to OBS",
    render: async () => {
      settings = await api("/api/settings");
      return h("div", {},
        h("ol", { class: "steps-list" },
          h("li", { text: "Open OBS Studio 28 or newer." }),
          h("li", { text: "Tools → WebSocket Server Settings → tick “Enable WebSocket server”. Port 4455 is the default." }),
          h("li", { text: "Click “Show Connect Info”, copy the server password and paste it in step 1 (OBS WebSocket password)." })),
        fieldEl({ path: "obs.host", label: "OBS host", type: "text" }),
        fieldEl({ path: "obs.port", label: "OBS port", type: "number", min: 1, max: 65535 }),
        h("p", { class: "hint", text: `Current OBS status: ${status ? status.obs.state : "?"}` }));
    },
  },
  {
    title: "3 · Camera source & presentation area",
    render: async () => {
      settings = await api("/api/settings");
      return h("div", {},
        h("p", { class: "hint", text: "Select the Sony ZV-1 input. The app screenshots that input only — never your program output — so the overlay is never fed back into recognition." }),
        fieldEl({ path: "obs.camera_source", label: "Camera source", type: "source" }),
        fieldEl({ path: "obs.rotation", label: "Rotation", type: "select", options: [0, 90, 180, 270] }),
        fieldEl({ path: "obs.crop", label: "Presentation area", type: "crop", help: "Drag around where you hold games up." }),
        backgroundField());
    },
  },
  {
    title: "4 · Overlay layout & OBS Browser Source",
    render: async () => {
      settings = await api("/api/settings");
      const url = status ? status.overlay.url : `${location.origin}/overlay`;
      return h("div", {},
        fieldEl({ path: "overlay.position", label: "Position", type: "select", options: ["top-center", "bottom-center"] }),
        fieldEl({ path: "overlay.canvas_width", label: "OBS canvas width", type: "number", min: 320, max: 7680 }),
        fieldEl({ path: "overlay.canvas_height", label: "OBS canvas height", type: "number", min: 320, max: 7680 }),
        h("h3", { text: "Add the overlay to OBS" }),
        h("ol", { class: "steps-list" },
          h("li", {}, "In your Whatnot scene: Sources → + → Browser."),
          h("li", {}, "URL: ", h("code", { text: url })),
          h("li", { text: "Width/Height = your canvas (1080 × 1920 for vertical, 1920 × 1080 for horizontal)." }),
          h("li", { text: "Leave Custom CSS as OBS default (transparent background). Untick “Shutdown source when not visible” and “Refresh browser when scene becomes active”." }),
          h("li", { text: "Put the Browser source ABOVE the camera in the source list. Do not crop or resize it — position and size are controlled here." })));
    },
  },
  {
    title: "5 · Ready",
    render: async () =>
      h("div", {},
        h("p", { text: "Hold a game steady inside the presentation area. Within a few seconds the dashboard shows the recognized title and PriceCharting match, and the overlay appears on stream." }),
        h("p", { class: "hint", text: "Keyboard: P pause/resume · S scan now · H hold · C clear · R refresh prices · / search · U UPC · 1–9 pick a suggestion." })),
  },
];

async function renderWizard() {
  $("wizSteps").replaceChildren(...WIZ.map((w, i) => h("span", { class: i === wizStep ? "on" : "", text: w.title.split(" · ")[0] })));
  $("wizTitle").textContent = WIZ[wizStep].title;
  $("wizBody").replaceChildren(h("p", { class: "hint", text: "Loading…" }));
  try {
    $("wizBody").replaceChildren(await WIZ[wizStep].render());
  } catch (e) {
    $("wizBody").replaceChildren(h("p", { class: "err", text: e.message }));
  }
  $("wizBack").disabled = wizStep === 0;
  $("wizNext").textContent = wizStep === WIZ.length - 1 ? "Finish" : "Next";
}

function openWizard() {
  wizStep = 0;
  $("wizard").hidden = false;
  renderWizard();
}

// ------------------------------------------------------------------ live updates
let ws;
let wsAttempt = 0;
function connectWs() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/dashboard`);
  ws.onopen = () => (wsAttempt = 0);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "status") renderStatus(msg.status);
    } catch {}
  };
  ws.onclose = () => setTimeout(connectWs, Math.min(8000, 500 * 2 ** wsAttempt++));
}

// ------------------------------------------------------------------ keyboard & barcode
let scanBuf = "";
let scanLast = 0;
document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName);
  if (e.key === "Escape") {
    document.activeElement?.blur();
    $("settings").hidden = true;
    return;
  }
  if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
  // USB barcode scanners "type" digits very fast followed by Enter.
  const now = performance.now();
  if (/^\d$/.test(e.key)) {
    scanBuf = now - scanLast < 60 ? scanBuf + e.key : e.key;
    scanLast = now;
  } else if (e.key === "Enter" && scanBuf.length >= 8 && now - scanLast < 120) {
    const upc = scanBuf;
    scanBuf = "";
    act(() => api("/api/upc", { method: "POST", body: { upc } }), `UPC ${upc} looked up`);
    return;
  }
  const k = e.key.toLowerCase();
  if (k === "p") $("btnPause").click();
  else if (k === "s") $("btnScan").click();
  else if (k === "h") $("btnHold").click();
  else if (k === "c") $("btnClear").click();
  else if (k === "r") $("btnRefresh").click();
  else if (k === "u") { e.preventDefault(); $("upcInput").focus(); }
  else if (e.key === "/") { e.preventDefault(); $("searchInput").focus(); }
  else if (/^[1-9]$/.test(e.key)) {
    setTimeout(() => {
      if (now - scanLast < 60 && scanBuf.length > 1) return; // part of a barcode scan
      const i = Number(e.key) - 1;
      const fromSuggestions = !$("suggestionsWrap").hidden;
      const list = fromSuggestions ? suggestions : searchResults;
      if (list[i]) selectProduct(list[i].productId, fromSuggestions || $("teachChk").checked);
    }, 80);
  }
});

// ------------------------------------------------------------------ wiring
$("btnPause").addEventListener("click", () => act(() => api(status && status.engine.detectionEnabled ? "/api/detection/pause" : "/api/detection/resume", { method: "POST" })));
$("btnScan").addEventListener("click", () => act(() => api("/api/detection/rescan", { method: "POST" }), "Scanning…"));
$("btnHold").addEventListener("click", () => act(() => api("/api/overlay/hold", { method: "POST", body: { on: !(status && status.engine.held) } })));
$("btnClear").addEventListener("click", () => act(() => api("/api/overlay/clear", { method: "POST" }), "Overlay cleared"));
$("btnRefresh").addEventListener("click", () => act(() => api("/api/prices/refresh", { method: "POST" }), "Prices refreshed"));
$("btnPending").addEventListener("click", () => act(() => api("/api/overlay/show-pending", { method: "POST" })));
$("searchForm").addEventListener("submit", (e) => { e.preventDefault(); doSearch($("searchInput").value); });
$("upcForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const upc = $("upcInput").value.replace(/\D/g, "");
  if (upc) act(() => api("/api/upc", { method: "POST", body: { upc } }), `UPC ${upc} looked up`).then(() => ($("upcInput").value = ""));
});
$("btnReloadHistory").addEventListener("click", loadHistory);
$("btnSettings").addEventListener("click", () => { $("settings").hidden = false; renderSettings(); });
$("btnCloseSettings").addEventListener("click", () => ($("settings").hidden = true));
$("btnWizard").addEventListener("click", openWizard);
$("tabLive").addEventListener("click", () => setPreviewTab(false));
$("tabDesign").addEventListener("click", () => setPreviewTab(true));
$("sampleSel").addEventListener("change", sendDesignSample);
$("btnCopyUrl").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText($("obsUrl").textContent); toast("Copied"); } catch { toast("Copy failed — select the URL manually", true); }
});
for (const sel of document.querySelectorAll("[data-setting]")) {
  sel.addEventListener("change", () => saveSetting(sel.dataset.setting, sel.value));
}
$("canvasSel").addEventListener("change", async () => {
  const [w, hh] = $("canvasSel").value.split("x").map(Number);
  await saveSetting("overlay.canvas_width", w);
  await saveSetting("overlay.canvas_height", hh);
});
$("wizBack").addEventListener("click", () => { if (wizStep > 0) { wizStep--; renderWizard(); } });
$("wizSkip").addEventListener("click", () => ($("wizard").hidden = true));
$("wizNext").addEventListener("click", async () => {
  if (wizStep < WIZ.length - 1) { wizStep++; renderWizard(); return; }
  await saveSetting("setup_completed", true);
  $("wizard").hidden = true;
});
window.addEventListener("resize", sizePreview);

(async function boot() {
  try {
    settings = await api("/api/settings");
    syncQuickLayout();
    renderStatus(await api("/api/status"));
  } catch (e) {
    toast(e.message, true);
  }
  loadHistory();
  connectWs();
})();
