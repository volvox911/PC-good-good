// Run: node --test tests/js
import assert from "node:assert/strict";
import test from "node:test";

import {
  computeLayout,
  formatAsOf,
  formatCents,
  planTransition,
  reconnectDelay,
  resolveOrientation,
  sameRecord,
  validateRecord,
} from "../../gameoverlay/web/overlay/overlay-core.js";

const good = {
  title: "Zelda Ocarina of Time",
  platform: "Nintendo 64",
  productId: "900101",
  verified: true,
  looseUsd: 42.95,
  completeUsd: 109.99,
  newUsd: null,
  looseCents: 4295,
  completeCents: 10999,
  newCents: null,
  retrievedAt: "2026-11-10T14:32:00Z",
};

test("formatCents: exact USD from integer pennies", () => {
  assert.equal(formatCents(4295), "$42.95");
  assert.equal(formatCents(5), "$0.05");
  assert.equal(formatCents(100), "$1.00");
  assert.equal(formatCents(123456), "$1,234.56");
  assert.equal(formatCents(12345678), "$123,456.78");
  assert.notEqual(formatCents(4295), "$4,295");
});

test("formatCents: missing values are N/A, never $0.00", () => {
  for (const v of [null, undefined, 0, -1, 1.5, NaN, "4295"]) assert.equal(formatCents(v), "N/A");
});

test("validateRecord accepts the contract and normalizes", () => {
  const r = validateRecord(good);
  assert.ok(r.ok, r.error);
  assert.equal(r.record.loose, 4295);
  assert.equal(r.record.complete, 10999);
  assert.equal(r.record.new, null);
  assert.equal(r.record.platform, "NINTENDO 64");
  assert.ok(r.record.retrievedAt instanceof Date);
});

test("validateRecord accepts USD-only records (prototype API)", () => {
  const r = validateRecord({ title: "X", platform: "NES", productId: 1, verified: true, looseUsd: 42.95, completeUsd: null, newUsd: 599, retrievedAt: good.retrievedAt });
  assert.ok(r.ok);
  assert.equal(r.record.loose, 4295);
  assert.equal(r.record.new, 59900);
});

test("validateRecord rejects malformed input", () => {
  const bad = [
    null,
    [],
    "string",
    { ...good, verified: false },
    { ...good, verified: "true" },
    { ...good, title: "" },
    { ...good, title: 5 },
    { ...good, platform: undefined },
    { ...good, productId: "abc" },
    { ...good, productId: null },
    { ...good, looseCents: -5 },
    { ...good, looseCents: 12.5 },
    { ...good, looseCents: "4295" },
    { ...good, looseUsd: "42.95", looseCents: undefined },
    { ...good, looseUsd: Infinity, looseCents: undefined },
    { ...good, looseUsd: 41.0 }, // inconsistent with looseCents 4295
    { ...good, retrievedAt: "not a date" },
    { ...good, retrievedAt: undefined },
    { ...good, title: "x".repeat(201) },
  ];
  for (const b of bad) assert.equal(validateRecord(b).ok, false, JSON.stringify(b));
});

test("zero price is treated as no data", () => {
  const r = validateRecord({ ...good, looseCents: 0, looseUsd: 0 });
  assert.ok(r.ok);
  assert.equal(r.record.loose, null);
});

test("planTransition: enter / swap / refresh / exit", () => {
  const a = validateRecord(good).record;
  const b = validateRecord({ ...good, productId: "900302", title: "Mortal Kombat" }).record;
  const a2 = validateRecord({ ...good }).record;
  assert.equal(planTransition(false, null, a), "enter");
  assert.equal(planTransition(true, a, b), "swap");
  assert.equal(planTransition(true, a, a2), "refresh");
  assert.ok(sameRecord(a, a2));
  const aRefreshed = validateRecord({ ...good, looseCents: 4999, looseUsd: 49.99 }).record;
  assert.equal(planTransition(true, a, aRefreshed), "swap", "changed prices animate as a whole panel");
  assert.equal(planTransition(true, a, null), "exit");
  assert.equal(planTransition(false, null, null), "none");
});

test("formatAsOf", () => {
  const now = new Date(2026, 10, 10, 15, 0);
  assert.equal(formatAsOf(new Date(2026, 10, 10, 14, 32), now), "AS OF 14:32");
  assert.equal(formatAsOf(new Date(2026, 10, 9, 9, 5), now), "AS OF NOV 09 09:05");
  assert.equal(formatAsOf(new Date(2026, 10, 10, 14, 32), now, "stale"), "CACHED · AS OF 14:32");
  assert.equal(formatAsOf(new Date("bad"), now), "");
});

test("layout presets: portrait default is balanced bottom-center with 8% margins", () => {
  const L = computeLayout({ vw: 1080, vh: 1920, designHeight: 640 });
  assert.equal(L.orientation, "portrait");
  const margin = (1080 - L.width) / 2;
  assert.ok(margin >= 1080 * 0.07 && margin <= 1080 * 0.12, `margin ${margin}`);
  assert.ok(L.y + L.height <= 1920 && L.y > 1920 * 0.55, "in the lower part of the frame");
  assert.ok(L.height <= 1920 * 0.38 + 1);
});

test("layout presets: sizes and positions", () => {
  const c = computeLayout({ vw: 1080, vh: 1920, size: "compact", designHeight: 640 });
  const l = computeLayout({ vw: 1080, vh: 1920, size: "large", designHeight: 640 });
  assert.ok(c.width < l.width);
  const left = computeLayout({ vw: 1080, vh: 1920, position: "bottom-left", designHeight: 640 });
  const right = computeLayout({ vw: 1080, vh: 1920, position: "bottom-right", designHeight: 640 });
  assert.ok(left.x < right.x && left.x >= 0 && right.x + right.width <= 1080);
  const top = computeLayout({ vw: 1080, vh: 1920, position: "top-center", designHeight: 640 });
  assert.ok(top.y < 200);
  const side = computeLayout({ vw: 1080, vh: 1920, position: "side-right", designHeight: 640 });
  assert.ok(side.width <= 1080 * 0.46 + 1);
});

test("layout: landscape stays inside the canvas and auto-detects orientation", () => {
  assert.equal(resolveOrientation(1920, 1080), "landscape");
  assert.equal(resolveOrientation(1080, 1920), "portrait");
  assert.equal(resolveOrientation(1920, 1080, "portrait"), "portrait");
  for (const [vw, vh] of [[1920, 1080], [1280, 720], [1080, 1920], [720, 1280]]) {
    for (const size of ["compact", "balanced", "large"]) {
      const L = computeLayout({ vw, vh, size, designHeight: vw > vh ? 480 : 640 });
      assert.ok(L.x >= 0 && L.y >= 0 && L.x + L.width <= vw + 0.5 && L.y + L.height <= vh + 0.5, `${vw}x${vh} ${size}`);
    }
  }
});

test("reconnect backoff grows and is capped", () => {
  const fixed = () => 0.5;
  assert.equal(reconnectDelay(0, fixed), 500);
  assert.equal(reconnectDelay(1, fixed), 1000);
  assert.equal(reconnectDelay(10, fixed), 10000);
});
