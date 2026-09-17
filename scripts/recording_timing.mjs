function stableHash(value) {
  let hash = 2166136261;
  for (const character of String(value || "")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619) >>> 0;
  }
  return hash >>> 0;
}

export function automaticFinishDelayMs(recordingKey, elapsedMs, maximumSeconds) {
  const seed = stableHash(recordingKey);
  const naturalTargetMs = 30000 + (seed % 13000);
  const reviewPauseMs = 3200 + ((seed >>> 8) % 3500);
  const desiredFinishMs = Math.max(naturalTargetMs, Number(elapsedMs || 0) + reviewPauseMs);
  const latestFinishMs = Math.max(0, Number(maximumSeconds || 0) * 1000 - 1200);
  const finishAtMs = Math.min(desiredFinishMs, latestFinishMs);
  return Math.max(0, finishAtMs - Number(elapsedMs || 0));
}

export function finalizeInteractionEvidence(interactionMode, demonstration, metrics = {}, requestCount = 0) {
  if (demonstration?.required) return demonstration;
  const clicks = Number(metrics?.clicks || 0);
  const requests = Number(requestCount || 0);
  if (interactionMode === "manual") {
    return {
      required: false,
      ok: true,
      interactionMode: "manual",
      clicks,
      requests,
      review: "human",
    };
  }
  return {
    required: true,
    ok: clicks > 0 && requests > 0,
    clicks,
    requests,
    error: "没有检测到真实功能点击和成功接口请求",
  };
}
