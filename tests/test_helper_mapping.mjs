import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../chrome-solo-qa-gsb-helper/background.js", import.meta.url), "utf8");
const context = vm.createContext({
  chrome: {
    runtime: { onMessage: { addListener() {} } },
    tabs: { query: async () => [] },
    scripting: { executeScript: async () => [] },
  },
  Headers,
  URL,
  fetch,
  crypto,
  btoa,
  setTimeout,
});
vm.runInContext(source, context);

function buildData(schema, bundle, uploaded) {
  context.schema = schema;
  context.bundle = bundle;
  context.uploaded = uploaded;
  return vm.runInContext("buildData(schema, bundle, uploaded)", context);
}

test("maps the current SOLO-QA recording labels to local A/B videos", () => {
  const schema = {
    fields: [
      { field_key: "a_runtime_recording", label: "A-运行录屏", is_required: true },
      { field_key: "b_runtime_recording", label: "B-运行录屏", is_required: true },
    ],
  };
  const result = buildData(schema, { values: {} }, {
    a_video: "https://files.example/a.mp4",
    b_video: "https://files.example/b.mp4",
  });
  assert.equal(result.a_runtime_recording, "https://files.example/a.mp4");
  assert.equal(result.b_runtime_recording, "https://files.example/b.mp4");
});

test("keeps exact field-key mapping for existing SOLO-QA fields", () => {
  const schema = { fields: [{ field_key: "user_prompt", label: "User Prompt", is_required: true }] };
  const result = buildData(schema, { values: { user_prompt: "实现复杂工作流" } }, {});
  assert.equal(result.user_prompt, "实现复杂工作流");
});

test("keeps sending validity when the form schema omits it but the validator requires it", () => {
  const schema = { fields: [{ field_key: "user_prompt", label: "User Prompt", is_required: true }] };
  const result = buildData(schema, {
    values: { user_prompt: "实现复杂工作流", validity: "有效" },
  }, {});
  assert.equal(result.validity, "有效");
});

test("still rejects an unknown required field", () => {
  const schema = { fields: [{ field_key: "new_required", label: "全新必填项", is_required: true }] };
  assert.throws(
    () => buildData(schema, { values: {} }, {}),
    /SOLO-QA 本期新增了无法映射的必填项：全新必填项/,
  );
});

test("limits a status sync to the requested Pair when a row button is used", () => {
  context.syncItems = [
    { pair_id: "pair-1111111111111111", remote_id: "51" },
    { pair_id: "pair-2222222222222222", remote_id: "52" },
  ];
  context.syncPayload = { pair_ids: ["pair-2222222222222222"] };
  const result = vm.runInContext("selectSyncItems(syncItems, syncPayload)", context);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), [
    { pair_id: "pair-2222222222222222", remote_id: "52" },
  ]);
});

test("keeps the existing all-record sync when no Pair is selected", () => {
  context.syncItems = [
    { pair_id: "pair-1111111111111111" },
    { pair_id: "pair-2222222222222222" },
  ];
  context.syncPayload = {};
  const result = vm.runInContext("selectSyncItems(syncItems, syncPayload)", context);
  assert.equal(result.length, 2);
});

test("uses the current SOLO-QA GSB API routes", () => {
  assert.match(source, /remoteJson\("\/gsb\/form-schema"\)/);
  assert.match(source, /remoteJson\(`\/gsb\/submissions\/\$\{encodeURIComponent\(remoteId\)\}`\)/);
});

test("falls back to the submissions list when the detail endpoint is unavailable", async () => {
  vm.runInContext(`
    remoteJson = async (path) => {
      if (path.startsWith("/gsb/submissions?")) return { items: [{
        id: "1542", status: "PENDING_FIX",
        data: { a_session_id: "a-session", b_session_id: "b-session", user_prompt: "prompt" }
      }] };
      throw new Error("数据不存在或无权访问");
    };
    fallbackBundle = { values: {
      a_session_id: "a-session", b_session_id: "b-session", user_prompt: "prompt"
    } };
  `, context);
  const result = await vm.runInContext('loadRemoteDetail(fallbackBundle, "1542")', context);
  assert.equal(result.id, "1542");
  assert.equal(result.status, "PENDING_FIX");
});

test("coalesces concurrent submit requests for the same Pair", async () => {
  vm.runInContext(`
    submitCallCount = 0;
    submitOneUnlocked = async (pairId) => {
      submitCallCount += 1;
      await new Promise((resolve) => setTimeout(resolve, 10));
      return { pair_id: pairId, outcome: "submitted", remote_id: "474" };
    };
  `, context);
  const result = await vm.runInContext(`Promise.all([
    submitOne("pair-1111111111111111"), submitOne("pair-1111111111111111")
  ])`, context);
  assert.equal(vm.runInContext("submitCallCount", context), 1);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), [
    { pair_id: "pair-1111111111111111", outcome: "submitted", remote_id: "474" },
    { pair_id: "pair-1111111111111111", outcome: "submitted", remote_id: "474" },
  ]);
});
