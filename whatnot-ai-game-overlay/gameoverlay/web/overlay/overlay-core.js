// Pure overlay logic (no DOM) — unit-tested with `node --test tests/js`.
//
// Record contract (equivalent to the design brief's VerifiedGamePriceRecord):
//   { title, platform, productId, verified: true,
//     looseUsd|null, completeUsd|null, newUsd|null,       // numbers (brief API)
//     looseCents|null, completeCents|null, newCents|null,  // exact integers (backend)
//     retrievedAt: ISO string, freshness?: 'fresh'|'cached'|'stale', demo?: boolean }

export const KINDS = ["loose", "complete", "new"];
export const NA = "N/A";

/** 4295 -> "$42.95"; 12345678 -> "$123,456.78"; null/0/invalid -> "N/A" (never "$0.00"). */
export function formatCents(cents) {
  if (cents === null || cents === undefined || !Number.isSafeInteger(cents) || cents <= 0) return NA;
  const dollars = Math.floor(cents / 100);
  const rem = cents % 100;
  return "$" + String(dollars).replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + String(rem).padStart(2, "0");
}

/**
 * Viewer-facing price text. With `round` (default), rounds half-up to whole dollars
 * ("$12.50" -> "$13", "$137.46" -> "$137"); values under $1 stay exact so a real price
 * is never shown as "$0". Without `round`, exact to the cent with ".00" dropped.
 */
export function formatDisplayPrice(cents, round = true) {
  const exact = formatCents(cents);
  if (exact === NA) return NA;
  if (round && cents >= 100) {
    const dollars = Math.floor((cents + 50) / 100);
    return "$" + String(dollars).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }
  return exact.endsWith(".00") ? exact.slice(0, -3) : exact;
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

/** The panel's resting state between games: no title from any game, no prices. */
export function idleRecord(title = "") {
  return {
    idle: true,
    title: typeof title === "string" ? title.slice(0, 60) : "",
    platform: "",
    productId: "__idle__",
    loose: null,
    complete: null,
    new: null,
    retrievedAt: null,
    freshness: "fresh",
    demo: false,
  };
}

export const PLACEHOLDER = "—";

/** The text a price plate shows: idle "—", missing "N/A" (never "$0.00"), else the price. */
export function plateText(cents, { idle = false, round = true } = {}) {
  if (idle) return PLACEHOLDER;
  return formatDisplayPrice(cents, round);
}

/**
 * Horizontal fit used by the price strip (from the design package's overlay.js): text keeps
 * its vertical size and is only narrowed with scaleX, never below `min`, so the frame and
 * the plates never move or resize. Returns the scaleX factor (1 = fits as is).
 */
export function fitScale(width, maxWidth, min = 0.2) {
  if (!(width > 0) || !(maxWidth > 0) || width <= maxWidth) return 1;
  return Math.max(min, Math.min(1, maxWidth / width));
}

export function sameRecord(a, b) {
  if (!a || !b) return false;
  return (
    a.idle === b.idle &&
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

/** Exponential reconnect delay with jitter (ms). */
export function reconnectDelay(attempt, rand = Math.random) {
  const base = Math.min(10000, 500 * 2 ** Math.max(0, attempt));
  return Math.round(base * (0.8 + 0.4 * rand()));
}
