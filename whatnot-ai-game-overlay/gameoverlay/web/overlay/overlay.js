// THE WASTELAND EXCHANGE — PRICE-O-MATIC · viewer overlay runtime
//
// * Connects to the local backend over a read-only WebSocket (/ws/overlay) and
//   reconnects automatically (no OBS page refresh needed).
// * Shows nothing until a verified record arrives. Every update is a full state
//   snapshot applied as ONE atomic DOM update — the title, console and the three
//   prices always change together.
// * Exposes window.setGameOverlayData(record) / window.hideGameOverlay() (design
//   prototype API). ?preview=1 is a watermarked design preview fed by the
//   dashboard via postMessage; it never opens the live socket.

import {
  computeLayout,
  formatAsOf,
  formatCents,
  planTransition,
  reconnectDelay,
  resolveOrientation,
  validateRecord,
} from "./overlay-core.js";

const $ = (id) => document.getElementById(id);
const els = {
  anchor: $("anchor"),
  machine: $("machine"),
  title: $("gameTitle"),
  console: $("consoleName"),
  loose: $("priceLoose"),
  complete: $("priceComplete"),
  new: $("priceNew"),
  asOf: $("asOf"),
  attribution: $("attribution"),
  machineName: $("machineName"),
  machineModel: $("machineModel"),
  demoMark: $("demoMark"),
};

const params = new URLSearchParams(location.search);
const PREVIEW = params.get("preview") === "1";
const SWAP_OUT_MS = 150;
const SWAP_IN_MS = 180;
const ENTER_MS = 380;
const PRICE_CONDENSE = 0.86; // must match .price { transform: scaleX(...) } in overlay.css
const EXIT_MS = 360;

let layout = {
  size: "balanced",
  position: "bottom-center",
  orientation: "auto",
  opacity: 1,
  fontScale: 1,
  edgeOffset: 0.035,
  attribution: "SOURCE: PRICECHARTING",
  machineName: "THE WASTELAND EXCHANGE",
  machineModel: "PRICE-O-MATIC",
  reducedMotion: false,
  disconnectHideMs: 120000,
};
let demoMode = false;
let shown = null; // normalized record currently rendered
let visible = false;
let busy = false;
let queued; // latest pending target (undefined = none); intermediate states are dropped
let orientation = null;

const prefersReduced = () =>
  layout.reducedMotion || (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// ------------------------------------------------------------------ rendering
function setPrice(el, cents) {
  const text = formatCents(cents);
  el.textContent = text;
  el.classList.toggle("na", text === "N/A");
}

/** Single atomic write of every game field. */
function render(rec) {
  els.title.textContent = rec.title;
  els.console.textContent = rec.platform;
  setPrice(els.loose, rec.loose);
  setPrice(els.complete, rec.complete);
  setPrice(els.new, rec.new);
  renderFooter(rec);
  els.machine.classList.toggle("is-stale", rec.freshness === "stale");
  els.demoMark.hidden = !(PREVIEW || rec.demo || demoMode);
  els.machine.setAttribute(
    "aria-label",
    `${rec.title}, ${rec.platform}. Loose ${formatCents(rec.loose)}, complete ${formatCents(rec.complete)}, new ${formatCents(rec.new)}.`,
  );
  fitText();
  position();
}

function renderFooter(rec) {
  els.asOf.textContent = rec.retrievedAt ? formatAsOf(rec.retrievedAt, new Date(), rec.freshness) : "";
  els.asOf.classList.toggle("stale", rec.freshness === "stale");
}

function clearText() {
  for (const el of [els.title, els.console, els.loose, els.complete, els.new, els.asOf]) el.textContent = "";
}

function fitText() {
  const fs = Number(layout.fontScale) || 1;
  // Title: up to 2 lines at full size, shrinking gracefully, then 3 lines (clamped).
  const t = els.title;
  let size = 48 * fs;
  const min = 30;
  t.style.fontSize = size + "px";
  const lines = (n, s) => n * s * 1.12 + s * 0.06 + 2;
  while (t.scrollHeight > lines(2, size) && size > min) {
    size -= 2;
    t.style.fontSize = size + "px";
  }
  // Prices: each readout as large as its display allows (huge by default, a
  // six-figure value shrinks only itself) — never overflowing the CRT glass.
  // Digits are drawn with scaleX(PRICE_CONDENSE) in CSS, so measure accordingly.
  const base = 70;
  for (const el of [els.loose, els.complete, els.new]) {
    el.style.fontSize = base * fs + "px";
    const avail = el.parentElement.clientWidth - 12 - 14; // padding + breathing room from the bezel
    const need = el.scrollWidth * PRICE_CONDENSE;
    let eff = base * fs;
    if (need > avail && need > 0) eff = (base * fs * avail) / need;
    el.style.fontSize = Math.max(22, Math.floor(eff)) + "px";
  }
}

function applyLayoutSettings() {
  document.documentElement.style.setProperty("--font-scale", String(Number(layout.fontScale) || 1));
  els.machine.style.setProperty("--panel-opacity", String(Number(layout.opacity) || 1));
  els.attribution.textContent = layout.attribution || "SOURCE: PRICECHARTING";
  els.machineName.textContent = layout.machineName || "THE WASTELAND EXCHANGE";
  els.machineModel.textContent = layout.machineModel || "PRICE-O-MATIC";
  document.documentElement.classList.toggle("reduced-motion", !!layout.reducedMotion);
  const o = resolveOrientation(window.innerWidth, window.innerHeight, layout.orientation);
  if (o !== orientation) {
    orientation = o;
    els.machine.classList.toggle("layout-landscape", o === "landscape");
    els.machine.classList.toggle("layout-portrait", o === "portrait");
    if (shown) fitText();
  }
  position();
}

function position() {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const L = computeLayout({
    vw,
    vh,
    size: layout.size,
    position: layout.position,
    orientation: layout.orientation,
    edgeOffset: layout.edgeOffset,
    designHeight: els.machine.offsetHeight,
  });
  els.anchor.style.transform = `translate(${L.x}px, ${L.y}px) scale(${L.scale})`;
  els.anchor.dataset.orientation = L.orientation;
}

function setVisible(on) {
  visible = on;
  els.machine.dataset.state = on ? "visible" : "hidden";
  document.body.dataset.overlay = on ? "visible" : "hidden";
}

// ------------------------------------------------------------------ transitions
async function apply(next) {
  if (busy) {
    queued = next; // only the latest target matters
    return;
  }
  busy = true;
  try {
    const plan = planTransition(visible, shown, next);
    const reduced = prefersReduced();
    if (plan === "enter") {
      render(next);
      shown = next;
      void els.machine.offsetWidth; // commit start state so the entry transition runs
      setVisible(true);
      if (!reduced) await wait(ENTER_MS);
    } else if (plan === "exit") {
      setVisible(false);
      shown = null;
      if (!reduced) await wait(EXIT_MS);
      if (!visible) clearText(); // no stale game data left in the DOM
    } else if (plan === "swap") {
      if (reduced) {
        render(next);
        shown = next;
      } else {
        els.machine.classList.add("swap-out");
        await wait(SWAP_OUT_MS);
        render(next); // all fields change in the same frame, while hidden
        shown = next;
        els.machine.classList.remove("swap-out");
        els.machine.classList.add("swap-in");
        await wait(SWAP_IN_MS);
        els.machine.classList.remove("swap-in");
      }
    } else if (plan === "refresh") {
      shown = next;
      renderFooter(next);
    }
  } finally {
    busy = false;
    document.body.dataset.settled = queued === undefined ? "1" : "0";
    if (queued !== undefined) {
      const q = queued;
      queued = undefined;
      apply(q);
    }
  }
}

// ------------------------------------------------------------------ public API (prototype contract)
window.setGameOverlayData = (record) => {
  const v = validateRecord(record, { allowMissingTime: PREVIEW });
  if (!v.ok) {
    console.warn("overlay: rejected record:", v.error);
    return false;
  }
  apply(v.record);
  return true;
};
window.hideGameOverlay = () => apply(null);
window.__overlayState = () => ({ visible, shown, busy, demoMode, preview: PREVIEW, layout, orientation });

// ------------------------------------------------------------------ live connection
let ws = null;
let attempt = 0;
let lastMsgAt = 0;
let disconnectedAt = null;
let reconnectTimer = null;

function onState(msg) {
  if (msg.layout && typeof msg.layout === "object") layout = { ...layout, ...msg.layout };
  demoMode = msg.demoMode === true;
  applyLayoutSettings();
  if (!msg.visible || !msg.record) {
    apply(null);
    return;
  }
  const v = validateRecord(msg.record);
  apply(v.ok ? v.record : null); // malformed data is never displayed
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
    if (msg && msg.type === "state") onState(msg);
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
  // Freshness/hide policy during long outages: never leave possibly-outdated data up forever.
  if (disconnectedAt !== null && visible && now - disconnectedAt > (layout.disconnectHideMs || 120000)) {
    apply(null);
  }
  // Keep the "AS OF" line correct across midnight etc.
  if (shown && visible && !busy) renderFooter(shown);
}

// ------------------------------------------------------------------ boot
window.addEventListener("resize", () => {
  applyLayoutSettings();
});
setVisible(false);
applyLayoutSettings();

if (PREVIEW) {
  document.documentElement.classList.add("is-preview");
  window.addEventListener("message", (e) => {
    if (e.origin !== location.origin || !e.data || typeof e.data !== "object") return;
    if (e.data.type === "preview-layout" && e.data.layout) {
      layout = { ...layout, ...e.data.layout };
      applyLayoutSettings();
    } else if (e.data.type === "preview" && e.data.record) {
      if (e.data.layout) layout = { ...layout, ...e.data.layout };
      applyLayoutSettings();
      window.setGameOverlayData({ ...e.data.record, demo: true });
    } else if (e.data.type === "preview-hide") {
      apply(null);
    }
  });
  if (window.parent !== window) window.parent.postMessage({ type: "preview-ready" }, location.origin);
} else {
  connect();
  setInterval(watchdog, 1000);
}
