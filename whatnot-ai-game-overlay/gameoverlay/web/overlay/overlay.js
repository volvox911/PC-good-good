// Price guide band · viewer overlay runtime
//
// * Connects to the local backend over a read-only WebSocket (/ws/overlay) and
//   reconnects automatically (no OBS page refresh needed).
// * Between games the band stays on screen in an idle state (no game title, no
//   prices) unless the operator turns that off; it never shows sample data. Every
//   update is a full state snapshot applied as ONE atomic DOM update — the title,
//   console and the three prices always change together.
// * Exposes window.setGameOverlayData(record) / window.hideGameOverlay() (design
//   prototype API). ?preview=1 is a watermarked design preview fed by the
//   dashboard via postMessage; it never opens the live socket.

import {
  computeLayout,
  formatAsOf,
  formatCents,
  idleRecord,
  PLACEHOLDER,
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
  metaLine: document.querySelector(".meta-line"),
  demoMark: $("demoMark"),
};

const params = new URLSearchParams(location.search);
const PREVIEW = params.get("preview") === "1";
const SWAP_OUT_MS = 150;
const SWAP_IN_MS = 180;
const ENTER_MS = 360;
const EXIT_MS = 340;

let layout = {
  size: "balanced",
  position: "top-center",
  orientation: "auto",
  opacity: 1,
  fontScale: 1,
  edgeOffset: 0.008,
  attribution: "SOURCE: PRICECHARTING",
  showAttribution: false,
  showRetrievalTime: false,
  idlePanel: true,
  idleTitle: "Price Guide",
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

/** What to show when no game is on air: the idle panel, or nothing at all. */
const emptyTarget = () => (layout.idlePanel === false ? null : idleRecord(layout.idleTitle ?? "Price Guide"));

// ------------------------------------------------------------------ rendering
function span(cls, text) {
  const s = document.createElement("span");
  s.className = cls;
  s.textContent = text;
  return s;
}

/**
 * Price-tag style, exact to the cent: "$12.34" -> "$12" + smaller ".34";
 * whole-dollar values drop ".00" ("$12"), which is the same exact value.
 */
function setPrice(el, cents, idle) {
  let text = idle ? PLACEHOLDER : formatCents(cents);
  if (text.endsWith(".00")) text = text.slice(0, -3);
  const m = /^(\$[\d,]+)(\.\d\d)$/.exec(text);
  el.replaceChildren(...(m ? [span("p-main", m[1]), span("p-cents", m[2])] : [span("p-main", text)]));
  el.classList.toggle("na", text === "N/A");
  el.classList.toggle("placeholder", idle);
}

/** Single atomic write of every game field. */
function render(rec) {
  const idle = rec.idle === true;
  els.title.textContent = rec.title;
  els.console.textContent = idle ? "" : rec.platform;
  setPrice(els.loose, rec.loose, idle);
  setPrice(els.complete, rec.complete, idle);
  setPrice(els.new, rec.new, idle);
  renderFooter(rec);
  els.machine.classList.toggle("is-idle", idle);
  els.machine.classList.toggle("is-stale", !idle && rec.freshness === "stale");
  els.demoMark.hidden = !(PREVIEW || rec.demo || demoMode);
  document.body.dataset.content = idle ? "idle" : "record";
  els.machine.setAttribute(
    "aria-label",
    idle
      ? "Price guide — waiting for the next game"
      : `${rec.title}, ${rec.platform}. Loose ${formatCents(rec.loose)}, complete ${formatCents(rec.complete)}, new ${formatCents(rec.new)}.`,
  );
  fitText();
  position();
}

function renderFooter(rec) {
  // The retrieval time is optional (off by default). Stale prices — only ever shown when
  // the operator allows it — are always labeled so they never pass as current.
  const stale = !rec.idle && rec.freshness === "stale";
  let text = "";
  if (!rec.idle && rec.retrievedAt && layout.showRetrievalTime) text = formatAsOf(rec.retrievedAt, new Date(), rec.freshness);
  else if (stale) text = "CACHED";
  els.asOf.textContent = text;
  els.asOf.classList.toggle("stale", stale);
  els.metaLine.hidden = !text && els.attribution.hidden;
}

function clearText() {
  for (const el of [els.title, els.console, els.loose, els.complete, els.new, els.asOf]) el.textContent = "";
}

function fitText() {
  const fs = Number(layout.fontScale) || 1;
  // Title: up to 2 lines at full size, shrinking gracefully, then 3 lines (clamped).
  // Title: one line at full size if possible (shrinking a little), otherwise two
  // balanced lines at a smaller size — the sign never grows taller.
  const t = els.title;
  t.classList.remove("wrap");
  let size = 62 * fs;
  t.style.fontSize = size + "px";
  while (t.scrollWidth > t.clientWidth + 1 && size > 40) {
    size -= 2;
    t.style.fontSize = size + "px";
  }
  if (t.scrollWidth > t.clientWidth + 1) {
    t.classList.add("wrap");
    size = 38 * fs;
    t.style.fontSize = size + "px";
    const lines = (n, s) => n * s * 1.14 + s * 0.1 + 2;
    while (t.scrollHeight > lines(2, size) && size > 20) {
      size -= 1;
      t.style.fontSize = size + "px";
    }
  }
  // Prices: each as large as its tag allows (a six-figure value shrinks only itself).
  const base = 54;
  for (const el of [els.loose, els.complete, els.new]) {
    el.style.fontSize = (el.classList.contains("na") ? 40 : base) * fs + "px";
    if (el.classList.contains("na")) continue;
    const avail = el.parentElement.clientWidth - 16; // breathing room inside the tag
    const need = el.scrollWidth;
    let eff = base * fs;
    if (need > avail && need > 0) eff = (base * fs * avail) / need;
    el.style.fontSize = Math.max(22, Math.floor(eff)) + "px";
  }
}

function applyLayoutSettings() {
  document.documentElement.style.setProperty("--font-scale", String(Number(layout.fontScale) || 1));
  els.machine.style.setProperty("--panel-opacity", String(Number(layout.opacity) || 1));
  // Source attribution is off by default (operator choice); the retrieval time stays on.
  els.attribution.textContent = layout.attribution || "";
  els.attribution.hidden = !(layout.showAttribution && layout.attribution);
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
  const pos = layout.position || "top-center";
  els.anchor.dataset.edge = pos.startsWith("bottom-") ? "bottom" : pos.startsWith("side-") ? "side" : "top";
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
      document.body.dataset.content = "none";
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
window.hideGameOverlay = () => apply(emptyTarget());
window.__overlayState = () => ({ visible, shown, busy, demoMode, preview: PREVIEW, layout, orientation });

// ------------------------------------------------------------------ live connection
let ws = null;
let attempt = 0;
let lastMsgAt = 0;
let disconnectedAt = null;
let reconnectTimer = null;
let lastStateSeq = null;
let seqMismatch = 0;

function onState(msg) {
  if (msg.layout && typeof msg.layout === "object") layout = { ...layout, ...msg.layout };
  demoMode = msg.demoMode === true;
  applyLayoutSettings();
  if (!msg.visible || !msg.record) {
    apply(emptyTarget()); // game left: clear to the idle panel (or hide, if configured)
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
  // Freshness/hide policy during long outages: never leave possibly-outdated data up forever.
  if (disconnectedAt !== null && visible && shown && !shown.idle && now - disconnectedAt > (layout.disconnectHideMs || 120000)) {
    apply(emptyTarget());
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
      apply(emptyTarget());
    }
  });
  if (window.parent !== window) window.parent.postMessage({ type: "preview-ready" }, location.origin);
} else {
  connect();
  setInterval(watchdog, 1000);
}
