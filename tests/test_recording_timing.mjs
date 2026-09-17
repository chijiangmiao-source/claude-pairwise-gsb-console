import assert from "node:assert/strict";
import test from "node:test";

import { automaticFinishDelayMs } from "../scripts/recording_timing.mjs";

test("automatic recordings vary instead of ending at one fixed second", () => {
  const keys = Array.from({ length: 12 }, (_, index) => `recording-${index}`);
  const totals = keys.map((key) => 9000 + automaticFinishDelayMs(key, 9000, 88));
  assert.ok(new Set(totals).size >= 8);
  assert.ok(totals.every((value) => value >= 30000 && value < 43000));
  assert.ok(totals.some((value) => Math.round(value / 1000) !== 38));
});

test("a longer real workflow keeps a review pause and respects the maximum", () => {
  const elapsed = 41000;
  const total = elapsed + automaticFinishDelayMs("long-workflow", elapsed, 50);
  assert.ok(total >= 44200);
  assert.ok(total <= 48800);
});
