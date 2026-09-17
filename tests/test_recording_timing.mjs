import assert from "node:assert/strict";
import test from "node:test";

import { automaticFinishDelayMs, finalizeInteractionEvidence } from "../scripts/recording_timing.mjs";

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

test("manual recording is saved for human review without automatic request detection", () => {
  assert.deepEqual(
    finalizeInteractionEvidence("manual", { required: false, ok: true }, { clicks: 0 }, 0),
    {
      required: false,
      ok: true,
      interactionMode: "manual",
      clicks: 0,
      requests: 0,
      review: "human",
    },
  );
});

test("automatic recording still requires both a click and a successful request", () => {
  assert.equal(finalizeInteractionEvidence("auto", { required: false }, { clicks: 1 }, 0).ok, false);
  assert.equal(finalizeInteractionEvidence("auto", { required: false }, { clicks: 1 }, 1).ok, true);
});
