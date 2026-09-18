import assert from "node:assert/strict";
import test from "node:test";
import { sampleValue } from "../scripts/recording_openapi.mjs";

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
