"use strict";

const LOCAL_ORIGIN = "http://127.0.0.1:8865";
const LOCAL_API = `${LOCAL_ORIGIN}/api/solo-qa`;
const SOLO_ORIGIN = "https://solo2.jzxhnh.com";
const PAIR_RE = /^pair-[a-f0-9]{16}$/;
const RETRYABLE = new Set([0, 408, 425, 429, 500, 502, 503, 504]);
const RETRY_DELAYS = [500, 1500, 3500];
const STATUS_MAP = {
  SUBMITTED: "qc_pending",
  QC_PASSED: "qc_passed",
  PENDING_FIX: "needs_fix",
  DISCARDED: "discarded",
};
const activeSubmissions = new Map();

function errorMessage(body, fallback) {
  if (typeof body === "string" && body.trim()) return body;
  if (body && typeof body === "object") {
    const messages = [];
    const add = (value, field = "") => {
      if (Array.isArray(value)) return value.forEach((item) => add(item, field));
      if (value && typeof value === "object") {
        return add(value.message || value.msg || value.detail || value.error, value.field || field);
      }
      const text = String(value || "").trim();
      if (text) messages.push(field ? `${field}：${text}` : text);
    };
    if (Array.isArray(body.errors)) body.errors.forEach((item) => add(item));
    else if (body.errors && typeof body.errors === "object") {
      Object.entries(body.errors).forEach(([field, value]) => add(value, field));
    }
    add(body.detail); add(body.error); add(body.message);
    if (messages.length) return [...new Set(messages)].join("；");
  }
  return fallback;
}

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, { credentials: "omit", cache: "no-store", ...options });
  } catch (error) {
    throw new Error(`本地 Pairwise 系统连接失败：${error instanceof Error ? error.message : String(error)}`);
  }
  const text = await response.text();
  let body = {};
  if (text) { try { body = JSON.parse(text); } catch { body = text; } }
  if (!response.ok) throw new Error(errorMessage(body, `本地接口请求失败 (${response.status})`));
  return body;
}
function localJson(path, options = {}) { return requestJson(`${LOCAL_API}${path}`, options); }
function jsonOptions(body, method = "POST") {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}
function wait(ms) { return new Promise((resolve) => setTimeout(resolve, ms)); }

async function soloTab() {
  const tabs = await chrome.tabs.query({ url: `${SOLO_ORIGIN}/*` });
  const tab = tabs.find((item) => item.active && Number.isInteger(item.id))
    || tabs.find((item) => Number.isInteger(item.id));
  if (!tab) throw new Error("请先在 Chrome 打开并登录 SOLO-QA，再保持该页面打开");
  return tab;
}

function bytesToBase64(bytes) {
  const chunks = [];
  for (let offset = 0; offset < bytes.length; offset += 32 * 1024) {
    chunks.push(String.fromCharCode(...bytes.subarray(offset, offset + 32 * 1024)));
  }
  return btoa(chunks.join(""));
}

async function requestRemoteOnce(path, options = {}, pageBody = null) {
  const tab = await soloTab();
  const method = String(options.method || "GET").toUpperCase();
  const headers = {};
  new Headers(options.headers || {}).forEach((value, key) => { headers[key] = value; });
  const body = pageBody || (typeof options.body === "string"
    ? { kind: "text", value: options.body } : { kind: "none" });
  let injected;
  try {
    injected = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, world: "MAIN",
      func: async (request) => {
        const cookieValue = (name) => {
          const prefix = `${name}=`;
          const entry = document.cookie.split("; ").find((item) => item.startsWith(prefix));
          return entry ? decodeURIComponent(entry.slice(prefix.length)) : "";
        };
        const requestHeaders = { ...request.headers };
        if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method)) {
          const csrf = cookieValue("solo_qa_csrf");
          if (!csrf) return { ok: false, status: 403, body: { detail: "SOLO-QA 页面缺少 CSRF 凭据，请刷新登录页面" } };
          requestHeaders["X-CSRF-Token"] = csrf;
        }
        let requestBody;
        if (request.body.kind === "text") requestBody = request.body.value;
        if (request.body.kind === "file") {
          const binary = atob(request.body.base64);
          const bytes = new Uint8Array(binary.length);
          for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
          const form = new FormData();
          form.append("file", new Blob([bytes], { type: request.body.contentType }), request.body.filename);
          if (request.body.uploadKind) form.append("kind", request.body.uploadKind);
          requestBody = form;
          delete requestHeaders["content-type"];
          delete requestHeaders["Content-Type"];
        }
        let response;
        try {
          response = await fetch(`/api/v1${request.path}`, {
            method: request.method, headers: requestHeaders, body: requestBody,
            credentials: "include", cache: "no-store",
          });
        } catch (error) {
          return { ok: false, status: 0, body: { detail: `SOLO-QA 连接失败：${error instanceof Error ? error.message : String(error)}` } };
        }
        const text = await response.text();
        let responseBody = {};
        if (text) { try { responseBody = JSON.parse(text); } catch { responseBody = text; } }
        return { ok: response.ok, status: response.status, body: responseBody };
      },
      args: [{ path, method, headers, body }],
    });
  } catch (error) {
    const failure = new Error(`无法调用已登录的 SOLO-QA 页面：${error instanceof Error ? error.message : String(error)}`);
    failure.remoteStatus = 0; throw failure;
  }
  const result = injected?.[0]?.result;
  if (!result || typeof result.status !== "number") {
    const failure = new Error("SOLO-QA 页面没有返回有效结果，请刷新后重试"); failure.remoteStatus = 0; throw failure;
  }
  if (!result.ok) {
    const failure = new Error(errorMessage(result.body, `SOLO-QA 请求失败 (${result.status || "网络错误"})`));
    failure.remoteStatus = result.status; throw failure;
  }
  return result.body;
}

async function remote(path, options = {}, pageBody = null, attempts = 1) {
  let last;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try { return await requestRemoteOnce(path, options, pageBody); }
    catch (error) {
      last = error;
      if (attempt >= attempts || !RETRYABLE.has(Number(error?.remoteStatus))) throw error;
      await wait(RETRY_DELAYS[Math.min(attempt - 1, RETRY_DELAYS.length - 1)]);
    }
  }
  throw last;
}
function remoteJson(path, options = {}) {
  return remote(path, options, null, String(options.method || "GET").toUpperCase() === "GET" ? 4 : 1);
}

async function sha256Hex(blob) {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
async function uploadFile(meta, uploadKind = "") {
  const response = await fetch(`${LOCAL_ORIGIN}${meta.url}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`读取本地文件失败 (${response.status})：${meta.name}`);
  const blob = await response.blob();
  if (blob.size !== Number(meta.size)) throw new Error(`本地文件大小已变化：${meta.name}`);
  if (await sha256Hex(blob) !== meta.sha256) throw new Error(`本地文件摘要已变化：${meta.name}`);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  return remote("/submissions/upload", { method: "POST" }, {
    kind: "file", base64: bytesToBase64(bytes), filename: meta.name,
    contentType: meta.content_type || blob.type || "application/octet-stream", uploadKind,
  }, 1);
}

function normalizeLabel(value) {
  return String(value || "")
    .replace(/[＊*：:\s_\-—–/\\（）()【】\[\]]/g, "")
    .toLowerCase();
}
const FIELD_SOURCE_BY_LABEL = new Map([
  ["a运行录屏", "a_video"],
  ["a运行录像", "a_video"],
  ["a录屏", "a_video"],
  ["a录像", "a_video"],
  ["b运行录屏", "b_video"],
  ["b运行录像", "b_video"],
  ["b录屏", "b_video"],
  ["b录像", "b_video"],
]);
function fieldSourceKeys(field) {
  const schemaKey = String(field.field_key || "");
  const keys = [schemaKey];
  for (const label of [field.label, field.name, field.title]) {
    const alias = FIELD_SOURCE_BY_LABEL.get(normalizeLabel(label));
    if (alias && !keys.includes(alias)) keys.push(alias);
  }
  return keys;
}
function choices(field) {
  return [field.options, field.choices, field.validation?.options].find(Array.isArray) || [];
}
function normalizeChoice(field, value) {
  const options = choices(field);
  if (!options.length) return value;
  const wanted = normalizeLabel(value);
  const match = options.find((option) => {
    const optionValue = typeof option === "object" ? option.value : option;
    const optionLabel = typeof option === "object" ? option.label : option;
    return normalizeLabel(optionValue) === wanted || normalizeLabel(optionLabel) === wanted;
  });
  if (!match) {
    const allowed = options.map((option) => typeof option === "object" ? option.label ?? option.value : option);
    throw new Error(`${field.label || field.field_key}的值“${value}”不被本期表单接受；当前可选：${allowed.join("、")}`);
  }
  return typeof match === "object" ? match.value : match;
}
function buildData(schema, bundle, uploaded) {
  const result = {}, missing = [];
  for (const field of schema.fields || []) {
    if (field.is_enabled === false) continue;
    const key = String(field.field_key || "");
    if (!key) continue;
    const sourceKeys = fieldSourceKeys(field);
    const uploadKey = sourceKeys.find((candidate) => Object.prototype.hasOwnProperty.call(uploaded, candidate));
    const valueKey = sourceKeys.find((candidate) => Object.prototype.hasOwnProperty.call(bundle.values || {}, candidate));
    if (uploadKey) result[key] = uploaded[uploadKey];
    else if (valueKey) result[key] = normalizeChoice(field, bundle.values[valueKey]);
    else if (field.is_required) missing.push(field.label || key);
  }
  // The current SOLO-QA form schema no longer exposes `validity`, while the
  // submission validator still requires it. Keep sending the local value until
  // the platform schema and validator are consistent again.
  if (!Object.prototype.hasOwnProperty.call(result, "validity")
      && Object.prototype.hasOwnProperty.call(bundle.values || {}, "validity")) {
    result.validity = bundle.values.validity;
  }
  if (missing.length) throw new Error(`SOLO-QA 本期新增了无法映射的必填项：${missing.join("、")}`);
  return result;
}

async function loadBundle(pairId) {
  if (!PAIR_RE.test(pairId)) throw new Error("Pair ID 格式不正确");
  return localJson(`/pairs/${pairId}/payload`);
}
async function recordState(bundle, values) {
  return localJson("/state", jsonOptions({ pair_id: bundle.pair_id, payload_sha256: bundle.payload_sha256, ...values }));
}
function detailSources(item) {
  return [item, item?.data, item?.values, item?.form_data, item?.payload].filter((value) => value && typeof value === "object");
}
function firstValue(item, keys) {
  for (const source of detailSources(item)) for (const key of keys) if (source[key] != null && String(source[key]).trim()) return String(source[key]);
  return "";
}
function compactRemote(item) {
  return {
    id: String(item?.id || ""), status: String(item?.status || "SUBMITTED"),
    a_session_id: firstValue(item, ["a_session_id", "A-SessionID"]),
    b_session_id: firstValue(item, ["b_session_id", "B-SessionID"]),
    a_prompt_id: firstValue(item, ["a_prompt_id", "A-PromptID"]),
    b_prompt_id: firstValue(item, ["b_prompt_id", "B-PromptID"]),
    user_prompt: firstValue(item, ["user_prompt", "User Prompt"]),
    qc_summary: String(item?.qc_summary || item?.message || "").slice(0, 2000),
    submitted_at: String(item?.submitted_at || item?.created_at || "").slice(0, 128),
    updated_at: String(item?.updated_at || item?.qc_finished_at || "").slice(0, 128),
  };
}
async function findRemote(bundle) {
  const values = bundle.values || {};
  const prompt = String(values.user_prompt || "").trim();
  const queries = [...new Set([
    values.a_session_id, values.b_session_id, values.a_prompt_id, values.b_prompt_id,
    prompt.slice(0, 48),
  ].map((value) => String(value || "").trim()).filter(Boolean))];
  const seen = new Set();
  for (const query of queries) {
    const response = await remoteJson(`/gsb/submissions?page=1&page_size=50&keyword=${encodeURIComponent(query)}`);
    const items = Array.isArray(response.items) ? response.items : [];
    for (const item of items) {
      const id = String(item?.id || "");
      if (!id || seen.has(id)) continue;
      seen.add(id);
      let detail = compactRemote(item);
      if (!detail.a_session_id || !detail.b_session_id || !detail.user_prompt) {
        detail = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(id)}`));
      }
      const sameSessions = Boolean(values.a_session_id && values.b_session_id
        && detail.a_session_id === values.a_session_id && detail.b_session_id === values.b_session_id);
      const samePrompts = Boolean(values.a_prompt_id && values.b_prompt_id
        && detail.a_prompt_id === values.a_prompt_id && detail.b_prompt_id === values.b_prompt_id);
      if (sameSessions || samePrompts) return detail;
    }
  }
  return null;
}
function stateForRemote(status) { return STATUS_MAP[String(status || "")] || "qc_pending"; }
function stateValues(detail, bundle = null) {
  const values = {
    status: stateForRemote(detail.status), remote_id: detail.id,
    remote_url: detail.id ? `${SOLO_ORIGIN}/app/gsb/submissions/${detail.id}` : "",
    remote_status: detail.status, qc_summary: detail.qc_summary,
    submitted_at: detail.submitted_at, remote_updated_at: detail.updated_at,
    error: "",
  };
  if (bundle?.payload_sha256) values.payload_sha256 = bundle.payload_sha256;
  return values;
}

async function uploadBundle(bundle, schema) {
  const traceLimit = Number(schema.attachmentMaxMb ?? schema.attachment_max_mb ?? 27) * 1024 * 1024;
  const videoLimit = Number(schema.videoMaxMb ?? schema.video_max_mb ?? 500) * 1024 * 1024;
  for (const key of ["a_trace_file", "b_trace_file"]) if (Number(bundle.files[key]?.size || 0) > traceLimit) throw new Error(`${key} 超过平台轨迹上限`);
  for (const key of ["a_video", "b_video"]) if (Number(bundle.files[key]?.size || 0) > videoLimit) throw new Error(`${key} 超过平台录像上限`);
  const aTrace = await uploadFile(bundle.files.a_trace_file);
  const aVideo = await uploadFile(bundle.files.a_video, "video");
  const bTrace = await uploadFile(bundle.files.b_trace_file);
  const bVideo = await uploadFile(bundle.files.b_video, "video");
  return {
    a_trace_file: [aTrace], a_video: aVideo.url || "",
    b_trace_file: [bTrace], b_video: bVideo.url || "",
  };
}

async function submitOneUnlocked(pairId) {
  const bundle = await loadBundle(pairId);
  if (!bundle.ready) throw new Error(`提交前检查未通过：${(bundle.issues || []).join("；")}`);
  if (bundle.solo_qa?.remote_id) {
    return { pair_id: pairId, outcome: "skipped", remote_id: String(bundle.solo_qa.remote_id), reason: "该 Pair 已绑定远端记录，只能同步状态或提交返修" };
  }
  if (bundle.solo_qa?.status === "submitting") {
    const recovered = await findRemote(bundle);
    if (recovered) {
      await recordState(bundle, stateValues(recovered, bundle));
      return { pair_id: pairId, outcome: "recovered", remote_id: recovered.id };
    }
    throw new Error("该 Pair 上一次提交仍在确认中，已拦截重复上传；请稍后同步状态");
  }
  const recoveredBefore = await findRemote(bundle);
  if (recoveredBefore) {
    await recordState(bundle, stateValues(recoveredBefore, bundle));
    return { pair_id: pairId, outcome: "recovered", remote_id: recoveredBefore.id };
  }
  await recordState(bundle, { status: "submitting", error: "" });
  try {
    const schema = await remoteJson("/gsb/form-schema");
    buildData(schema, bundle, { a_trace_file: [{}], a_video: "pending", b_trace_file: [{}], b_video: "pending" });
    const uploaded = await uploadBundle(bundle, schema);
    const data = buildData(schema, bundle, uploaded);
    const created = await remoteJson("/gsb/submissions", jsonOptions({ data, schema_fingerprint: schema.fingerprint || "" }));
    const detail = compactRemote({ ...created, status: created.status || "SUBMITTED" });
    if (!detail.id) throw new Error("SOLO-QA 已响应，但没有返回提交 ID");
    await recordState(bundle, stateValues(detail, bundle));
    return { pair_id: pairId, outcome: "submitted", remote_id: detail.id, status: detail.status };
  } catch (error) {
    let recovered = null;
    try { recovered = await findRemote(bundle); } catch { recovered = null; }
    if (recovered) {
      await recordState(bundle, stateValues(recovered, bundle));
      return { pair_id: pairId, outcome: "recovered", remote_id: recovered.id };
    }
    const message = error instanceof Error ? error.message : String(error);
    await recordState(bundle, { status: "failed", error: message });
    throw new Error(message);
  }
}

async function submitOne(pairId) {
  if (activeSubmissions.has(pairId)) return activeSubmissions.get(pairId);
  const pending = submitOneUnlocked(pairId);
  activeSubmissions.set(pairId, pending);
  try { return await pending; }
  finally { activeSubmissions.delete(pairId); }
}

async function repairOne(pairId) {
  const bundle = await loadBundle(pairId);
  if (!bundle.ready) throw new Error(`返修前检查未通过：${(bundle.issues || []).join("；")}`);
  const remoteId = String(bundle.solo_qa?.remote_id || "");
  if (!remoteId || bundle.solo_qa?.remote_status !== "PENDING_FIX") throw new Error("只有远端待返修的 Pair 可以返修，请先同步质检状态");
  const before = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(remoteId)}`));
  if (before.status !== "PENDING_FIX") throw new Error("远端记录已不是待返修状态，请先同步");
  await recordState(bundle, { status: "submitting", remote_id: remoteId, remote_status: before.status, error: "" });
  try {
    const schema = await remoteJson("/gsb/form-schema");
    const uploaded = await uploadBundle(bundle, schema);
    const data = buildData(schema, bundle, uploaded);
    await remoteJson(`/gsb/submissions/${encodeURIComponent(remoteId)}`, jsonOptions({
      data, schema_fingerprint: schema.fingerprint || "", comment: "",
    }, "PUT"));
    const detail = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(remoteId)}`));
    await recordState(bundle, stateValues(detail, bundle));
    return { pair_id: pairId, outcome: "repaired", remote_id: remoteId, status: detail.status };
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    await recordState(bundle, { status: "failed", remote_id: remoteId, remote_status: "PENDING_FIX", error: message });
    throw new Error(message);
  }
}

async function batch(payload, action) {
  const ids = Array.isArray(payload?.pair_ids) ? [...new Set(payload.pair_ids.map(String))] : [];
  if (!ids.length || ids.length > 100 || ids.some((id) => !PAIR_RE.test(id))) throw new Error("请选择有效的 Pair");
  const results = [];
  for (const pairId of ids) {
    try { results.push(await action(pairId)); }
    catch (error) { results.push({ pair_id: pairId, outcome: "failed", error: error instanceof Error ? error.message : String(error) }); }
  }
  return { results, failed: results.filter((item) => item.outcome === "failed").length };
}

function selectSyncItems(items, payload = {}) {
  const requested = Array.isArray(payload?.pair_ids)
    ? [...new Set(payload.pair_ids.map(String).filter(Boolean))] : [];
  if (!requested.length) return items;
  if (requested.length > 100 || requested.some((id) => !PAIR_RE.test(id))) throw new Error("请选择有效的 Pair");
  const wanted = new Set(requested);
  return items.filter((item) => wanted.has(String(item.pair_id || "")));
}

async function syncRemote(payload = {}) {
  const local = await localJson("/submissions");
  const results = [];
  for (const item of selectSyncItems(local.items || [], payload)) {
    try {
      const detail = compactRemote(await remoteJson(`/gsb/submissions/${encodeURIComponent(item.remote_id)}`));
      await localJson("/state", jsonOptions({ pair_id: item.pair_id, payload_sha256: item.payload_sha256 || "", ...stateValues(detail) }));
      results.push({ pair_id: item.pair_id, outcome: "synced", status: detail.status });
    } catch (error) {
      results.push({ pair_id: item.pair_id, outcome: "failed", error: error instanceof Error ? error.message : String(error) });
    }
  }
  return { results, failed: results.filter((item) => item.outcome === "failed").length };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  let origin = "";
  try { origin = new URL(sender.url || "").origin; } catch { origin = ""; }
  if (origin !== LOCAL_ORIGIN) { sendResponse({ ok: false, error: "只接受本地 Pairwise 系统发起的请求" }); return false; }
  const action = message?.type === "PAIRWISE_GSB_SUBMIT"
    ? () => batch(message.payload || {}, submitOne)
    : message?.type === "PAIRWISE_GSB_REPAIR"
      ? () => batch(message.payload || {}, repairOne)
      : message?.type === "PAIRWISE_GSB_SYNC" ? () => syncRemote(message.payload || {}) : null;
  if (!action) { sendResponse({ ok: false, error: "未知的提交助手操作" }); return false; }
  action().then((data) => sendResponse({ ok: true, data })).catch((error) => sendResponse({
    ok: false, error: error instanceof Error ? error.message : String(error),
  }));
  return true;
});
