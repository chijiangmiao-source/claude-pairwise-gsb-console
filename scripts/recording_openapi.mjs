export function resolveSchema(schema, spec) {
  if (!schema?.$ref) return schema || {};
  return schema.$ref.slice(2).split("/").reduce((value, key) => value?.[key], spec) || {};
}

function stringSample(schema, fieldName) {
  const name = fieldName.toLowerCase();
  if (/sha|digest|checksum|hash/.test(name)) return "0".repeat(64);
  if (/file.*name|filename/.test(name)) return "demo.bin";
  if (/id$|_id$|uuid/.test(name)) return "demo-1";
  if (schema.format === "email") return "demo@example.com";
  if (schema.format === "date") return "2026-01-01";
  if (schema.format === "date-time") return "2026-01-01T00:00:00Z";
  const minimum = Math.max(1, Number(schema.minLength || 1));
  return "demo".padEnd(minimum, "x");
}

export function sampleValue(inputSchema, spec, depth = 0, fieldName = "") {
  const schema = resolveSchema(inputSchema, spec);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.const !== undefined) return schema.const;
  if (schema.enum?.length) return schema.enum[0];
  if (schema.oneOf?.length || schema.anyOf?.length) {
    const options = schema.oneOf || schema.anyOf;
    const option = options.find((item) => resolveSchema(item, spec).type !== "null") || options[0];
    return sampleValue(option, spec, depth + 1, fieldName);
  }
  if (depth > 8) return null;
  if (schema.type === "object" || schema.properties) {
    const properties = schema.properties || {};
    if (properties.L && properties.n && properties.distances) {
      return { L: 10, n: 5, distances: [2, 4, 7, 10, 2, 5, 8, 3, 6, 3] };
    }
    const required = new Set(schema.required || []);
    return Object.fromEntries(Object.entries(properties)
      .filter(([key, value]) => required.has(key)
        || value?.example !== undefined || value?.default !== undefined || value?.const !== undefined)
      .map(([key, value]) => [key, sampleValue(value, spec, depth + 1, key)]));
  }
  if (schema.type === "array") {
    const name = fieldName.toLowerCase();
    if (/holes?/.test(name)) return [];
    if (/exterior|boundary|ring/.test(name)) return [[0, 0], [10, 0], [10, 10], [0, 10]];
    if (/path|polyline|transect/.test(name)) return [[-5, 5], [15, 5]];
    const count = Math.max(1, Number(schema.minItems || 1));
    return Array.from({ length: Math.min(count, 4) }, (_, index) => {
      if (schema.prefixItems?.length) {
        return schema.prefixItems.map((item, itemIndex) => {
          const value = sampleValue(item, spec, depth + 1, `${fieldName}_${itemIndex}`);
          return typeof value === "number" ? value + index : value;
        });
      }
      return sampleValue(schema.items || {}, spec, depth + 1, fieldName);
    });
  }
  if (schema.type === "integer" || schema.type === "number") {
    const name = fieldName.toLowerCase();
    if (/total.*size|file.*size|length|bytes/.test(name)) return Math.max(1024, Number(schema.minimum || 0));
    if (/chunk.*size/.test(name)) return Math.max(256, Number(schema.minimum || 0));
    return Math.max(1, Number(schema.minimum || 1));
  }
  if (schema.type === "boolean") return true;
  return stringSample(schema, fieldName);
}
