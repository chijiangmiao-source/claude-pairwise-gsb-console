import assert from "node:assert/strict";
import test from "node:test";

import { automaticFinishDelayMs, finalizeInteractionEvidence, isSafeFeatureControl } from "../scripts/recording_timing.mjs";

test("automatic recordings finish as soon as the real workflow completes", () => {
  assert.equal(automaticFinishDelayMs("recording", 9000, 88), 0);
  assert.equal(automaticFinishDelayMs("long-workflow", 41000, 50), 0);
});

test("feature traversal skips destructive controls", () => {
  assert.equal(isSafeFeatureControl("运行分析"), true);
  assert.equal(isSafeFeatureControl("结果详情"), true);
  assert.equal(isSafeFeatureControl("删除记录"), false);
  assert.equal(isSafeFeatureControl("Clear all"), false);
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
