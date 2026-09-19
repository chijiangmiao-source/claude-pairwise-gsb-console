import assert from "node:assert/strict";
import test from "node:test";
import { rankOpenApiOperations, sampleValue } from "../scripts/recording_openapi.mjs";

test("builds a valid non-degenerate polygon sample from an OpenAPI reference", () => {
  const spec = {
    components: { schemas: {
      PolygonIn: {
        type: "object", required: ["exterior"], properties: {
          exterior: { type: "array", items: { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }] } },
          holes: { type: "array", items: { type: "array" } },
        },
      },
      Request: {
        type: "object", required: ["a", "b"], properties: {
          a: { type: "array", items: { $ref: "#/components/schemas/PolygonIn" } },
          b: { type: "array", items: { $ref: "#/components/schemas/PolygonIn" } },
          optional_note: { type: "string" },
        },
      },
    } },
  };
  const value = sampleValue({ $ref: "#/components/schemas/Request" }, spec);
  assert.deepEqual(value.a[0].exterior, [[0, 0], [10, 0], [10, 10], [0, 10]]);
  assert.deepEqual(value.b[0].exterior, [[0, 0], [10, 0], [10, 10], [0, 10]]);
  assert.equal("optional_note" in value, false);
});

test("builds distinct coordinates for required paths", () => {
  const value = sampleValue({
    type: "object", required: ["path"], properties: {
      path: { type: "array", items: { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }] } },
    },
  }, {});
  assert.deepEqual(value.path, [[-5, 5], [15, 5]]);
});

test("uses a stable future date for expiring resources", () => {
  assert.equal(sampleValue({ type: "string", format: "date-time" }, {}, 0, "expires_at"),
    "2099-01-01T00:00:00Z");
});

test("builds a safe switch migration plan instead of unrelated placeholder ids", () => {
  const spec = {
    components: { schemas: {
      PlanCreateIn: {
        type: "object", required: ["idempotency_key", "topology"],
        properties: {
          idempotency_key: { type: "string" },
          topology: { type: "object" },
        },
      },
    } },
  };
  const sample = sampleValue({ $ref: "#/components/schemas/PlanCreateIn" }, spec);
  assert.equal(sample.topology.ingresses[0], "s1");
  assert.deepEqual(sample.topology.switches.map((item) => item.id), ["s1", "s2"]);
  assert.equal(sample.topology.switches[0].new_next, "DELIVER");
});

test("prefers a root create operation without unresolved identifier dependencies", () => {
  const spec = {
    paths: {
      "/runs": { post: { requestBody: { content: { "application/json": {
        schema: { $ref: "#/components/schemas/CreateRun" },
      } } } } },
      "/pipelines": { post: { requestBody: { content: { "application/json": {
        schema: { $ref: "#/components/schemas/CreatePipeline" },
      } } } } },
    },
    components: { schemas: {
      CreateRun: {
        type: "object", required: ["pipeline_id", "seal_id", "idempotency_key"],
        properties: { pipeline_id: { type: "string" }, seal_id: { type: "string" }, idempotency_key: { type: "string" } },
      },
      CreatePipeline: { type: "object", properties: { name: { type: "string" } } },
    } },
  };
  assert.equal(rankOpenApiOperations(spec)[0].path, "/pipelines");
});
