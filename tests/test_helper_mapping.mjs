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
