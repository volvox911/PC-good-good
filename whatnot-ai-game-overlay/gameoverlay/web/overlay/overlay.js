// Joe Dirt price strip · live overlay runtime
//
// Rendering and text fitting come from the design package's overlay.js (sanitized text,
// horizontal-only fit so the frame never moves). The data path is this app's own:
//
// * Connects to the local backend over a read-only WebSocket (/ws/overlay) and reconnects
//   automatically (no OBS page refresh needed). Only verified PriceCharting records are
//   shown; every update is one full snapshot written in a single synchronous step, so the
//   title, system and the three prices always change together, with no transition.
// * Between games the strip stays on screen with the title spot showing the idle text and
//   the plates showing "—" (unless the operator turns the idle strip off). The page never
//   contains sample data. ?preview=1 is the dashboard's watermarked design preview, fed by
//   postMessage; it never opens the live socket.
// * "Title only" (operator setting): the plate-free version of the same frame with just the
//   game and its system. The backend then sends no prices to this page at all.

import { fitScale, formatCents, idleRecord, KINDS, plateText, reconnectDelay, sameRecord, validateRecord } from "./overlay-core.js";

const search = new URLSearchParams(location.search);
const PREVIEW = search.get("preview") === "1";
if (PREVIEW) document.body.classList.add("preview");
const PAGE_BUILD = document.querySelector('meta[name="overlay-build"]')?.content || null;

const els = {
  header: document.getElementById("header"),
  title: document.getElementById("game-title"),
  system: document.getElementById("game-system"),
  loose: document.getElementById("price-loose"),
  complete: document.getElementById("price-complete"),
  new: document.getElementById("price-new"),
  demoMark: document.getElementById("demoMark"),
};

let layout = {
  position: "top-center",
  opacity: 1,
  edgeOffset: 0,
  idlePanel: true,
  idleTitle: "Price Guide",
  roundWholeDollars: true,
  showPrices: true,
  disconnectHideMs: 120000,
};
let demoMode = false;
let shown = null; // record on screen (verified or idle); null = nothing drawn
let renderedWith = ""; // display options the current text was rendered with

/** What to show when no game is on air: the idle strip, or nothing at all. */
const emptyTarget = () => (layout.idlePanel === false ? null : idleRecord(layout.idleTitle ?? "Price Guide"));

// ------------------------------------------------------------------ text (design package)
function text(value, fallback = "") {
  if (value == null || String(value).trim() === "") return fallback;
  return String(value).trim().slice(0, 110);
}

function setText(el, value) {
  if (el.textContent !== value) el.textContent = value;
}

/** 1% of the strip's width in px — the design's `cqw` unit, for OBS builds without it. */
function syncUnits() {
  const w = els.header.getBoundingClientRect().width;
  if (w > 0) els.header.style.setProperty("--u", (w / 100).toFixed(4) + "px");
}

// Fit actual font metrics horizontally without changing the frame or vertical type size.
function fitProp(span) {
  if (span.classList.contains("game-title")) return "--title-fit";
  if (span.classList.contains("game-system")) return "--system-fit";
  return "--fit";
}

function fitSpan(span, maxWidth) {
  const prop = fitProp(span);
  span.style.setProperty(prop, "1");
  const width = span.getBoundingClientRect().width;
  const scale = fitScale(width, maxWidth);
  if (scale !== 1) span.style.setProperty(prop, scale.toFixed(4));
  return width * scale <= maxWidth + 0.5;
}

/**
 * Fits a line; if even the narrowest allowed squeeze is too wide (an exceptionally long
 * name), shortens it with an ellipsis rather than letting it run over the price plates.
 */
function fitLine(span, maxWidth) {
  if (fitSpan(span, maxWidth)) return;
  const full = span.textContent;
  let lo = 1;
  let hi = full.length - 1;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    span.textContent = full.slice(0, mid).trimEnd() + "…";
    if (fitSpan(span, maxWidth)) lo = mid;
    else hi = mid - 1;
  }
  span.textContent = full.slice(0, lo).trimEnd() + "…";
  fitSpan(span, maxWidth);
}

function adjustText() {
  syncUnits();
  fitLine(els.title, els.title.parentElement.clientWidth - 6);
  fitLine(els.system, els.system.parentElement.clientWidth - 6);
  if (els.header.classList.contains("title-only")) return; // plates are not drawn
  for (const key of KINDS) fitSpan(els[key], els[key].parentElement.clientWidth - 4);
}

let frameRequested = false;
function scheduleFit() {
  if (frameRequested) return;
  frameRequested = true;
  requestAnimationFrame(() => {
    frameRequested = false;
    if (shown) render(shown); // re-fit from the full text (fonts or size changed)
  });
}

// ------------------------------------------------------------------ rendering
/** Display options that change what a record looks like (a change forces a re-render). */
const displayOptions = () => `${layout.roundWholeDollars !== false}|${layout.showPrices !== false}|${layout.idleTitle}|${demoMode}`;

/** Title-only mode: the operator hid the market values, or the record came without them. */
const pricesHidden = (rec) => layout.showPrices === false || rec.pricesHidden === true;

/** Single synchronous write of every field, fitted before the browser paints it. */
function render(rec) {
  const idle = rec.idle === true;
  const round = layout.roundWholeDollars !== false;
  const hidden = pricesHidden(rec);
  const stale = !idle && !hidden && rec.freshness === "stale";
  els.header.classList.toggle("title-only", hidden); // plate-free frame, wider title
  document.body.dataset.prices = hidden ? "hidden" : "shown";
  setText(els.title, text(rec.title));
  setText(els.system, idle ? "" : text(rec.platform) + (stale ? " · CACHED" : ""));
  for (const key of KINDS) setText(els[key], plateText(rec[key], { idle, round, hidden }));
  els.demoMark.hidden = !(PREVIEW || demoMode || rec.demo);
  document.body.dataset.content = idle ? "idle" : "record";
  els.header.setAttribute(
    "aria-label",
    idle
      ? "Waiting for the next game"
      : hidden
        ? `${rec.title}, ${rec.platform}.`
        : `${rec.title}, ${rec.platform}. Loose ${formatCents(rec.loose)}, complete ${formatCents(rec.complete)}, new ${formatCents(rec.new)}.`,
  );
  renderedWith = displayOptions();
  adjustText();
}

function apply(next) {
  if (!next) {
    shown = null;
    document.body.dataset.overlay = "hidden";
    document.body.dataset.content = "none";
    for (const el of [els.title, els.system, els.loose, els.complete, els.new]) setText(el, ""); // no stale game data left in the DOM
    return;
  }
  if (!(shown && sameRecord(shown, next) && displayOptions() === renderedWith)) render(next);
  shown = next;
  document.body.dataset.overlay = "visible";
}

function applyLayoutSettings() {
  const pos = String(layout.position || "top-center");
  els.header.classList.toggle("at-bottom", pos.startsWith("bottom-"));
  const offset = Math.max(0, Math.min(0.3, Number(layout.edgeOffset) || 0));
  els.header.style.setProperty("--edge-extra", (offset * window.innerHeight).toFixed(1) + "px");
  const opacity = Number(layout.opacity);
  els.header.style.setProperty("--strip-opacity", String(opacity >= 0.3 && opacity <= 1 ? opacity : 1));
}

// ------------------------------------------------------------------ public API
window.setGameOverlayData = (record) => {
  const v = validateRecord(record, { allowMissingTime: PREVIEW });
  if (!v.ok) {
    console.warn("overlay: rejected record:", v.error);
    return false;
  }
  apply(v.record);
  return true;
};
window.hideGameOverlay = () => apply(emptyTarget());
window.__overlayState = () => ({
  visible: document.body.dataset.overlay === "visible",
  shown,
  demoMode,
  preview: PREVIEW,
  layout,
  build: PAGE_BUILD,
});

/** Design package API ({title, system, prices: {loose, complete, new}} in dollars) — preview only. */
function previewUpdate(data) {
  if (!data || typeof data !== "object") return false;
  const prices = data.prices && typeof data.prices === "object" ? data.prices : data;
  const usd = (v) => {
    if (v == null || v === "") return null;
    const n = Number(String(v).replace(/[$,\s]/g, ""));
    return Number.isFinite(n) && n >= 0 ? n : undefined;
  };
  return window.setGameOverlayData({
    title: data.title ?? data.game,
    platform: data.system ?? data.platform,
    productId: "0",
    verified: true,
    demo: true,
    looseUsd: usd(prices.loose),
    completeUsd: usd(prices.complete),
    newUsd: usd(prices.new),
  });
}

// ------------------------------------------------------------------ live connection
/**
 * OBS keeps an open Browser Source page running even after the app is updated, so the
 * page would keep the old look while still receiving live data. When the backend reports a
 * newer overlay build, reload once to pick it up (guarded so it can never loop).
 */
function reloadIfOutdated(serverBuild) {
  if (!serverBuild || !PAGE_BUILD || serverBuild === PAGE_BUILD || PREVIEW) return false;
  try {
    if (sessionStorage.getItem("overlayReloadedFor") === serverBuild) return false;
    sessionStorage.setItem("overlayReloadedFor", serverBuild);
  } catch {
    return false; // without storage we can't guarantee a single reload — keep running
  }
  location.reload();
  return true;
}

let ws = null;
let attempt = 0;
let lastMsgAt = 0;
let disconnectedAt = null;
let reconnectTimer = null;
let lastStateSeq = null;
let seqMismatch = 0;

function onState(msg) {
  if (reloadIfOutdated(msg.build)) return;
  if (msg.layout && typeof msg.layout === "object") layout = { ...layout, ...msg.layout };
  demoMode = msg.demoMode === true;
  applyLayoutSettings();
  if (!msg.visible || !msg.record) {
    apply(emptyTarget()); // game left: clear to the idle strip (or hide, if configured)
    return;
  }
  const v = validateRecord(msg.record);
  apply(v.ok ? v.record : emptyTarget()); // malformed data is never displayed
}

function connect() {
  clearTimeout(reconnectTimer);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  try {
    ws = new WebSocket(`${proto}://${location.host}/ws/overlay`);
  } catch {
    scheduleReconnect();
    return;
  }
  ws.onopen = () => {
    attempt = 0;
    lastMsgAt = Date.now();
    disconnectedAt = null;
    document.body.dataset.connection = "open";
  };
  ws.onmessage = (ev) => {
    lastMsgAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    if (!msg) return;
    if (msg.type === "state") {
      lastStateSeq = msg.seq;
      seqMismatch = 0;
      onState(msg);
    } else if (msg.type === "ping" && typeof msg.seq === "number") {
      // Heartbeats carry the server's current state number. If we keep seeing a different
      // one, we missed an update: reconnect to receive the full current state.
      seqMismatch = msg.seq === lastStateSeq ? 0 : seqMismatch + 1;
      if (seqMismatch >= 2) {
        seqMismatch = 0;
        try {
          ws.close();
        } catch {}
      }
    }
  };
  ws.onclose = () => {
    document.body.dataset.connection = "closed";
    if (disconnectedAt === null) disconnectedAt = Date.now();
    scheduleReconnect();
  };
  ws.onerror = () => {
    try {
      ws.close();
    } catch {}
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, reconnectDelay(attempt++));
}

function watchdog() {
  const now = Date.now();
  if (ws && ws.readyState === WebSocket.OPEN && now - lastMsgAt > 25000) {
    try {
      ws.close(); // heartbeat missed — force a reconnect
    } catch {}
  }
  // Never leave possibly-outdated prices up forever during a long outage.
  if (disconnectedAt !== null && shown && !shown.idle && now - disconnectedAt > (layout.disconnectHideMs || 120000)) {
    apply(emptyTarget());
  }
}

// ------------------------------------------------------------------ boot
syncUnits();
applyLayoutSettings();
window.addEventListener(
  "resize",
  () => {
    applyLayoutSettings();
    scheduleFit();
  },
  { passive: true },
);
if (window.ResizeObserver) new ResizeObserver(scheduleFit).observe(els.header);
if (document.fonts) {
  document.fonts.ready.then(scheduleFit).catch(() => {});
  document.fonts.addEventListener?.("loadingdone", scheduleFit);
}

if (PREVIEW) {
  apply(emptyTarget());
  window.updateOverlay = previewUpdate;
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data || typeof e.data !== "object") return;
    if (e.data.type === "preview-layout" && e.data.layout) {
      layout = { ...layout, ...e.data.layout };
      applyLayoutSettings();
      if (shown) render(shown);
    } else if (e.data.type === "preview" && e.data.record) {
      if (e.data.layout) layout = { ...layout, ...e.data.layout };
      applyLayoutSettings();
      window.setGameOverlayData({ ...e.data.record, demo: true });
    } else if (e.data.type === "OVERLAY_UPDATE") {
      previewUpdate(e.data.payload);
    } else if (e.data.type === "preview-hide") {
      apply(emptyTarget());
    }
  });
  if (window.parent !== window) window.parent.postMessage({ type: "preview-ready" }, location.origin);
} else {
  connect();
  setInterval(watchdog, 1000);
}
