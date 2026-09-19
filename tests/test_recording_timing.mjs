import assert from "node:assert/strict";
import test from "node:test";

import {
  automaticFinishDelayMs, finalizeInteractionEvidence, humanClickPauseMs, isSafeFeatureControl,
} from "../scripts/recording_timing.mjs";

test("automatic recordings keep the final result visible for two seconds", () => {
  assert.equal(automaticFinishDelayMs("recording", 9000, 88), 2000);
  assert.equal(automaticFinishDelayMs("long-workflow", 41000, 50), 2000);
});

test("feature traversal skips destructive controls", () => {
  assert.equal(isSafeFeatureControl("运行分析"), true);
  assert.equal(isSafeFeatureControl("结果详情"), true);
  assert.equal(isSafeFeatureControl("删除记录"), false);
  assert.equal(isSafeFeatureControl("Clear all"), false);
});

test("automatic clicks use varied human-paced pauses", () => {
  const before = Array.from({ length: 8 }, (_, index) => humanClickPauseMs(index, "before"));
  const after = Array.from({ length: 8 }, (_, index) => humanClickPauseMs(index, "after"));
  assert.ok(before.every((value) => value >= 560 && value < 980));
  assert.ok(after.every((value) => value >= 820 && value < 1340));
  assert.ok(new Set(before).size > 4);
  assert.ok(new Set(after).size > 4);
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
