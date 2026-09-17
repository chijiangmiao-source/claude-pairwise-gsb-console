import { chromium } from "playwright";
import { copyFile } from "node:fs/promises";
import { existsSync } from "node:fs";

const [url, outputPath, profileDir, maximumRaw = "88", stopFile = `${outputPath}.stop`] = process.argv.slice(2);
if (!url || !outputPath || !profileDir) {
  console.error("usage: browser_recorder.mjs URL OUTPUT PROFILE [MAX_SECONDS]");
  process.exit(2);
}

const maximum = Math.max(5, Math.min(88, Number(maximumRaw) || 88));
let context;
let stopping = false;
let finishing = false;
let demonstrationPromise = Promise.resolve({ required: false, ok: true });
let monitorRequests = false;
const successfulRequests = [];

function resolveSchema(schema, spec) {
  if (!schema?.$ref) return schema || {};
  return schema.$ref.slice(2).split("/").reduce((value, key) => value?.[key], spec) || {};
}

function sampleValue(schema, spec, depth = 0) {
  schema = resolveSchema(schema, spec);
  if (schema.example !== undefined) return schema.example;
  if (schema.default !== undefined) return schema.default;
  if (schema.enum?.length) return schema.enum[0];
  if (depth > 5) return null;
  if (schema.type === "object" || schema.properties) {
    const properties = schema.properties || {};
    if (properties.L && properties.n && properties.distances) {
      return { L: 10, n: 5, distances: [2, 4, 7, 10, 2, 5, 8, 3, 6, 3] };
    }
    return Object.fromEntries(Object.entries(properties).map(([key, value]) => [key, sampleValue(value, spec, depth + 1)]));
  }
  if (schema.type === "array") {
    const count = Math.max(1, Number(schema.minItems || 1));
    return Array.from({ length: Math.min(count, 3) }, () => sampleValue(schema.items || {}, spec, depth + 1));
  }
  if (schema.type === "integer" || schema.type === "number") return Math.max(1, Number(schema.minimum || 1));
  if (schema.type === "boolean") return true;
  if (schema.format === "email") return "demo@example.com";
  if (schema.format === "date") return "2026-01-01";
  if (schema.format === "date-time") return "2026-01-01T00:00:00Z";
  return "demo";
}

async function moveAndClick(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("目标控件不可见");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 24 });
  await page.waitForTimeout(700);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

async function selectSwaggerOperation(page) {
  const spec = await page.evaluate(async () => {
    const response = await fetch("/openapi.json");
    if (!response.ok) throw new Error(`OpenAPI ${response.status}`);
    return response.json();
  });
  const methods = ["post", "put", "patch", "get"];
  const candidates = [];
  for (const [path, definition] of Object.entries(spec.paths || {})) {
    if (/health|ready|live/i.test(path)) continue;
    for (const method of methods) {
      const operation = definition?.[method];
      if (!operation) continue;
      const content = operation.requestBody?.content?.["application/json"];
      candidates.push({ path, method, operation, content, spec });
    }
  }
  candidates.sort((left, right) => {
    const leftScore = (left.content ? 100 : 0) + (left.method === "post" ? 20 : 0) - left.path.length;
    const rightScore = (right.content ? 100 : 0) + (right.method === "post" ? 20 : 0) - right.path.length;
    return rightScore - leftScore;
  });
  if (!candidates.length) throw new Error("没有可演示的业务接口");
  const selected = candidates[0];
  const body = selected.content
    ? (selected.content.example ?? selected.content.examples?.default?.value ?? sampleValue(selected.content.schema, spec))
    : null;
  return { path: selected.path, method: selected.method, body };
}

async function findSwaggerBlock(page, selected) {
  const blocks = page.locator(".opblock");
  for (let index = 0; index < await blocks.count(); index += 1) {
    const block = blocks.nth(index);
    const path = (await block.locator(".opblock-summary-path").first().innerText()).trim();
    const method = (await block.locator(".opblock-summary-method").first().innerText()).trim().toLowerCase();
    if (path === selected.path && method === selected.method) return block;
  }
  throw new Error(`Swagger 中未找到 ${selected.method.toUpperCase()} ${selected.path}`);
}

async function demonstrateSwaggerWorkflow(page) {
  if (!new URL(page.url()).pathname.startsWith("/docs")) return { required: false, ok: true };
  await page.waitForTimeout(3000);
  const selected = await selectSwaggerOperation(page);
  const block = await findSwaggerBlock(page, selected);
  if (!(await block.getAttribute("class") || "").includes("is-open")) {
    await moveAndClick(page, block.locator(".opblock-summary").first());
  }
  await page.waitForTimeout(3000);
  await moveAndClick(page, block.locator("button.try-out__btn").first());
  await page.waitForTimeout(3000);
  if (selected.body !== null) {
    const textarea = block.locator("textarea").first();
    await moveAndClick(page, textarea);
    await textarea.fill(JSON.stringify(selected.body, null, 2));
    await page.waitForTimeout(4000);
  }
  await moveAndClick(page, block.locator("button.execute").first());
  const responses = block.locator(".live-responses-table").first();
  await responses.waitFor({ state: "visible", timeout: 15000 });
  await responses.scrollIntoViewIfNeeded();
  const statusTexts = await responses.locator(".response-col_status").allTextContents();
  const status = Number(statusTexts.map((text) => text.match(/\d{3}/)?.[0]).find(Boolean) || 0);
  await page.waitForTimeout(7000);
  const result = { required: true, ok: status >= 200 && status < 300, status, method: selected.method, path: selected.path };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function finish(reason) {
  if (finishing) return;
  finishing = true;
  try {
    let demonstration = await Promise.race([
      demonstrationPromise,
      new Promise((resolve) => setTimeout(() => resolve({ required: true, ok: false, error: "真实功能演示尚未完成" }), 6000)),
    ]);
    const pages = context?.pages() || [];
    if (!demonstration.required && pages[0]) {
      const metrics = await pages[0].evaluate(() => window.__pairwiseRecordingMetrics || { clicks: 0 });
      demonstration = {
        required: true,
        ok: Number(metrics.clicks || 0) > 0 && successfulRequests.length > 0,
        clicks: Number(metrics.clicks || 0),
        requests: successfulRequests.length,
        error: "没有检测到真实功能点击和成功接口请求",
      };
    }
    stopping = true;
    const video = pages[0]?.video();
    await context?.close();
    if (video) await copyFile(await video.path(), outputPath);
    if (demonstration.required && !demonstration.ok) {
      throw new Error(demonstration.error || `真实接口请求失败：HTTP ${demonstration.status || "未知"}`);
    }
    process.stdout.write(`${JSON.stringify({ event: "finished", reason, demonstration })}\n`);
    process.exit(0);
  } catch (error) {
    console.error(error?.stack || String(error));
    process.exit(1);
  }
}

try {
  context = await chromium.launchPersistentContext(profileDir, {
    executablePath: "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: false,
    viewport: { width: 1280, height: 720 },
    recordVideo: { dir: `${profileDir}/videos`, size: { width: 1280, height: 720 } },
    args: ["--no-first-run", "--no-default-browser-check", "--disable-session-crashed-bubble"],
  });
  await context.addInitScript(() => {
    window.__pairwiseRecordingMetrics = { clicks: 0 };
    const ensureCursor = () => {
      if (document.getElementById("pairwise-recording-cursor")) return;
      const cursor = document.createElement("div");
      cursor.id = "pairwise-recording-cursor";
      cursor.innerHTML = '<svg viewBox="0 0 28 36" width="28" height="36" aria-hidden="true"><path d="M2 2L2 28L9 21L14 33L20 30L15 19L25 19Z" fill="#111827" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>';
      Object.assign(cursor.style, {
        position: "fixed", left: "0", top: "0", width: "28px", height: "36px",
        transform: "translate3d(48px,48px,0)", pointerEvents: "none", zIndex: "2147483647",
        filter: "drop-shadow(0 1px 2px rgba(0,0,0,.7))", transition: "transform .05s linear",
      });
      document.documentElement.appendChild(cursor);
    };
    const hideOpenApiLink = () => {
      document.querySelectorAll('.swagger-ui a[href*="openapi.json"]').forEach((link) => {
        const wrapper = link.parentElement;
        link.remove();
        if (wrapper && !wrapper.textContent.trim()) wrapper.style.display = "none";
      });
    };
    const observer = new MutationObserver(hideOpenApiLink);
    const observeDocument = () => {
      observer.observe(document.documentElement, { childList: true, subtree: true });
      hideOpenApiLink();
      ensureCursor();
    };
    document.documentElement ? observeDocument() : document.addEventListener("DOMContentLoaded", observeDocument, { once: true });
    document.addEventListener("pointermove", (event) => {
      ensureCursor();
      const cursor = document.getElementById("pairwise-recording-cursor");
      cursor.style.transform = `translate3d(${event.clientX}px,${event.clientY}px,0)`;
    }, true);
    document.addEventListener("pointerdown", (event) => {
      window.__pairwiseRecordingMetrics.clicks += 1;
      const dot = document.createElement("div");
      Object.assign(dot.style, {
        position: "fixed", left: `${event.clientX - 15}px`, top: `${event.clientY - 15}px`,
        width: "30px", height: "30px", border: "3px solid #ef4444", borderRadius: "50%",
        background: "rgba(239,68,68,.16)", pointerEvents: "none", zIndex: "2147483647",
        transition: "transform .45s ease-out, opacity .45s ease-out",
      });
      document.documentElement.appendChild(dot);
      requestAnimationFrame(() => { dot.style.transform = "scale(1.8)"; dot.style.opacity = "0"; });
      setTimeout(() => dot.remove(), 520);
    }, true);
  });
  const pages = context.pages();
  const page = pages[0] || await context.newPage();
  page.on("response", (response) => {
    if (!monitorRequests || response.status() < 200 || response.status() >= 300) return;
    const request = response.request();
    if (!["xhr", "fetch"].includes(request.resourceType())) return;
    try {
      if (new URL(response.url()).origin === new URL(page.url()).origin) {
        successfulRequests.push({ method: request.method(), url: response.url(), status: response.status() });
      }
    } catch {}
  });
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.bringToFront();
  monitorRequests = true;
  process.stdout.write(`${JSON.stringify({ event: "ready", url: page.url() })}\n`);
  demonstrationPromise = demonstrateSwaggerWorkflow(page).catch((error) => ({
    required: new URL(page.url()).pathname.startsWith("/docs"), ok: false, error: error?.message || String(error),
  }));
  setTimeout(() => finish("maximum_duration"), maximum * 1000);
  setInterval(() => { if (existsSync(stopFile)) finish("manual_stop"); }, 250);
  process.on("SIGINT", () => finish("manual_stop"));
  process.on("SIGTERM", () => finish("terminated"));
} catch (error) {
  console.error(error?.stack || String(error));
  try { await context?.close(); } catch {}
  process.exit(1);
}
