// Run: node --test tests/js
import assert from "node:assert/strict";
import test from "node:test";

import {
  fitScale,
  formatCents,
  formatDisplayPrice,
  idleRecord,
  plateText,
  reconnectDelay,
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

test("formatDisplayPrice: whole dollars by default, half-up, never $0", () => {
  assert.equal(formatDisplayPrice(1250), "$13");
  assert.equal(formatDisplayPrice(13746), "$137");
  assert.equal(formatDisplayPrice(124599), "$1,246");
  assert.equal(formatDisplayPrice(12345678), "$123,457");
  assert.equal(formatDisplayPrice(100), "$1");
  assert.equal(formatDisplayPrice(149), "$1");
  assert.equal(formatDisplayPrice(150), "$2");
  assert.equal(formatDisplayPrice(49), "$0.49", "under $1 stays exact, never $0");
  for (const v of [null, undefined, 0, -5, 1.5]) assert.equal(formatDisplayPrice(v), "N/A");
});

test("formatDisplayPrice: exact cents when rounding is off", () => {
  assert.equal(formatDisplayPrice(124599, false), "$1,245.99");
  assert.equal(formatDisplayPrice(59900, false), "$599");
  assert.equal(formatDisplayPrice(null, false), "N/A");
});

test("plateText: idle dash, missing N/A, otherwise the price", () => {
  assert.equal(plateText(4295, { idle: true }), "—");
  assert.equal(plateText(null), "N/A");
  assert.equal(plateText(4295), "$43");
  assert.equal(plateText(4295, { round: false }), "$42.95");
});

test("fitScale: only narrows, never below the minimum", () => {
  assert.equal(fitScale(100, 200), 1);
  assert.equal(fitScale(200, 100), 0.5);
  assert.equal(fitScale(1000, 100), 0.2);
  assert.equal(fitScale(0, 100), 1);
  assert.equal(fitScale(100, 0), 1);
});

test("sameRecord: identical game and prices; idle differs from a game", () => {
  const a = validateRecord(good).record;
  assert.ok(sameRecord(a, validateRecord({ ...good }).record));
  assert.ok(!sameRecord(a, validateRecord({ ...good, looseCents: 4999, looseUsd: 49.99 }).record));
  const idle = idleRecord("Price Guide");
  assert.equal(idle.loose, null);
  assert.equal(idle.productId, "__idle__");
  assert.ok(sameRecord(idle, idleRecord("Price Guide")));
  assert.ok(!sameRecord(idle, idleRecord("Next up")));
  assert.ok(!sameRecord(idle, a));
});

test("reconnect backoff grows and is capped", () => {
  const fixed = () => 0.5;
  assert.equal(reconnectDelay(0, fixed), 500);
  assert.equal(reconnectDelay(1, fixed), 1000);
  assert.equal(reconnectDelay(10, fixed), 10000);
});
