export function automaticFinishDelayMs() {
  // Keep the final result visible long enough to read after the last human-
  // paced scroll. This is a fixed review pause, not arbitrary duration padding.
  return 2000;
}

export function humanClickPauseMs(sequence, phase = "before") {
  const index = Math.max(0, Number(sequence) || 0);
  if (phase === "after") return 820 + ((index * 211 + 97) % 520);
  return 560 + ((index * 173 + 61) % 420);
}

export function isSafeFeatureControl(label) {
  const text = String(label || "").replace(/\s+/g, " ").trim();
  if (!text) return false;
  return !/(?:删除|移除|清空|重置|取消|关闭|退出|注销|下线|停止|终止|撤销|驳回|remove|delete|clear|reset|cancel|close|logout|stop|terminate|revoke|reject)/i.test(text);
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
