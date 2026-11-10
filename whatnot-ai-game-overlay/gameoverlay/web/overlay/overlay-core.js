// Pure overlay logic (no DOM) — unit-tested with `node --test tests/js`.
//
// Record contract (equivalent to the design brief's VerifiedGamePriceRecord):
//   { title, platform, productId, verified: true,
//     looseUsd|null, completeUsd|null, newUsd|null,       // numbers (brief API)
//     looseCents|null, completeCents|null, newCents|null,  // exact integers (backend)
//     retrievedAt: ISO string, freshness?: 'fresh'|'cached'|'stale', demo?: boolean }

export const KINDS = ["loose", "complete", "new"];
export const NA = "N/A";

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];

/** 4295 -> "$42.95"; 12345678 -> "$123,456.78"; null/0/invalid -> "N/A" (never "$0.00"). */
export function formatCents(cents) {
  if (cents === null || cents === undefined || !Number.isSafeInteger(cents) || cents <= 0) return NA;
  const dollars = Math.floor(cents / 100);
  const rem = cents % 100;
  return "$" + String(dollars).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + String(rem).padStart(2, "0");
}

function priceCents(raw, kind) {
  const c = raw[kind + "Cents"];
  const u = raw[kind + "Usd"];
  const hasC = c !== undefined && c !== null;
  const hasU = u !== undefined && u !== null;
  if (hasC && (!Number.isSafeInteger(c) || c < 0)) return { ok: false };
  if (hasU && (typeof u !== "number" || !Number.isFinite(u) || u < 0)) return { ok: false };
  if (hasC && hasU && Math.round(u * 100) !== c) return { ok: false }; // inconsistent record
  if (hasC) return { ok: true, value: c > 0 ? c : null };
  if (hasU) {
    const cents = Math.round(u * 100);
    return { ok: true, value: cents > 0 ? cents : null };
  }
  return { ok: true, value: null };
}

/**
 * Validate an incoming record. Anything malformed is rejected (the overlay then
 * shows nothing rather than guessing). Returns {ok, record?, error?}.
 */
export function validateRecord(raw, { allowMissingTime = false } = {}) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return { ok: false, error: "not an object" };
  if (raw.verified !== true) return { ok: false, error: "record is not verified" };
  const title = typeof raw.title === "string" ? raw.title.replace(/\s+/g, " ").trim() : "";
  const platform = typeof raw.platform === "string" ? raw.platform.replace(/\s+/g, " ").trim() : "";
  if (!title || title.length > 200) return { ok: false, error: "invalid title" };
  if (!platform || platform.length > 60) return { ok: false, error: "invalid platform" };
  const pid = raw.productId;
  if (!((typeof pid === "string" && /^\d{1,12}$/.test(pid)) || (Number.isSafeInteger(pid) && pid >= 0))) {
    return { ok: false, error: "invalid productId" };
  }
  const prices = {};
  for (const k of KINDS) {
    const r = priceCents(raw, k);
    if (!r.ok) return { ok: false, error: `invalid ${k} price` };
    prices[k] = r.value;
  }
  let retrievedAt = null;
  if (typeof raw.retrievedAt === "string" && raw.retrievedAt) {
    const d = new Date(raw.retrievedAt);
    if (Number.isNaN(d.getTime())) return { ok: false, error: "invalid retrievedAt" };
    retrievedAt = d;
  } else if (!allowMissingTime) {
    return { ok: false, error: "missing retrievedAt" };
  }
  const freshness = ["fresh", "cached", "stale"].includes(raw.freshness) ? raw.freshness : "fresh";
  return {
    ok: true,
    record: {
      title,
      platform: platform.toUpperCase(),
      productId: String(pid),
      loose: prices.loose,
      complete: prices.complete,
      new: prices.new,
      retrievedAt,
      freshness,
      demo: raw.demo === true,
    },
  };
}

export function sameRecord(a, b) {
  if (!a || !b) return false;
  return (
    a.productId === b.productId &&
    a.title === b.title &&
    a.platform === b.platform &&
    a.loose === b.loose &&
    a.complete === b.complete &&
    a.new === b.new &&
    a.freshness === b.freshness &&
    a.demo === b.demo
  );
}

/** What visual transition moves the overlay from `current` to `next`. */
export function planTransition(currentVisible, current, next) {
  if (!next) return currentVisible ? "exit" : "none";
  if (!currentVisible || !current) return "enter";
  if (sameRecord(current, next)) return "refresh"; // same game & prices: update footer only
  return "swap"; // complete-panel transition: title, console and all prices together
}

const pad = (n) => String(n).padStart(2, "0");

/** "AS OF 14:32"; other day -> "AS OF NOV 09 14:32"; stale -> "CACHED · AS OF ...". */
export function formatAsOf(date, now = new Date(), freshness = "fresh") {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return "";
  const sameDay =
    date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate();
  const day = sameDay ? "" : `${MONTHS[date.getMonth()]} ${pad(date.getDate())} `;
  return `${freshness === "stale" ? "CACHED · " : ""}AS OF ${day}${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const DESIGN_WIDTH = { portrait: 920, landscape: 1400 };
const WIDTH_FRACTION = {
  portrait: { compact: 0.7, balanced: 0.84, large: 0.9 },
  landscape: { compact: 0.46, balanced: 0.58, large: 0.7 },
};
const MAX_HEIGHT_FRACTION = { portrait: 0.38, landscape: 0.4 };

export function resolveOrientation(vw, vh, mode = "auto") {
  if (mode === "portrait" || mode === "landscape") return mode;
  return vw > vh * 1.05 ? "landscape" : "portrait";
}

/**
 * Where to draw the machine. Returns design->canvas scale and top-left position.
 * `designHeight` is the machine's unscaled height for the resolved orientation.
 */
export function computeLayout({ vw, vh, size = "balanced", position = "bottom-center", orientation = "auto", edgeOffset = 0.035, designHeight }) {
  const o = resolveOrientation(vw, vh, orientation);
  const dw = DESIGN_WIDTH[o];
  const frac = (WIDTH_FRACTION[o] || WIDTH_FRACTION.portrait)[size] ?? WIDTH_FRACTION[o].balanced;
  const side = position.startsWith("side-");
  let width = vw * frac;
  if (side) width = Math.min(width, vw * (o === "portrait" ? 0.46 : 0.34));
  let scale = width / dw;
  const maxH = vh * MAX_HEIGHT_FRACTION[o] * (side ? 1.4 : 1);
  if (designHeight && designHeight * scale > maxH) scale = maxH / designHeight;
  const w = dw * scale;
  const h = (designHeight || 0) * scale;
  const marginX = Math.max(vw * 0.04, 8);
  const marginY = vh * Math.max(0, edgeOffset);
  let x;
  if (position.endsWith("-left")) x = marginX;
  else if (position.endsWith("-right")) x = vw - w - marginX;
  else x = (vw - w) / 2;
  let y;
  if (position.startsWith("top-")) y = marginY;
  else if (side) y = (vh - h) / 2;
  else y = vh - h - marginY;
  return { orientation: o, scale, x: Math.round(x), y: Math.round(y), width: w, height: h, designWidth: dw };
}

/** Exponential reconnect delay with jitter (ms). */
export function reconnectDelay(attempt, rand = Math.random) {
  const base = Math.min(10000, 500 * 2 ** Math.max(0, attempt));
  return Math.round(base * (0.8 + 0.4 * rand()));
}
