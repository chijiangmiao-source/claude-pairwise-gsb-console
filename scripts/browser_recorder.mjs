import { chromium } from "playwright";
import ffmpegPath from "ffmpeg-static";
import { spawn } from "node:child_process";
import { copyFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { automaticFinishDelayMs, finalizeInteractionEvidence, isSafeFeatureControl } from "./recording_timing.mjs";
import { rankOpenApiOperations, sampleValue } from "./recording_openapi.mjs";

const [url, outputPath, profileDir, maximumRaw = "88", stopFile = `${outputPath}.stop`, interactionMode = "auto"] = process.argv.slice(2);
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
const recordingStartedAt = Date.now();

async function saveVideo(sourcePath, targetPath) {
  if (!targetPath.toLowerCase().endsWith(".mp4")) {
    await copyFile(sourcePath, targetPath);
    return;
  }
  await new Promise((resolve, reject) => {
    const command = spawn(ffmpegPath, [
      "-hide_banner", "-loglevel", "error", "-y", "-i", sourcePath,
      "-map", "0:v:0", "-an", "-c:v", "libx264", "-preset", "medium",
      "-crf", "21", "-pix_fmt", "yuv420p", "-movflags", "+faststart", targetPath,
    ]);
    let error = "";
    command.stderr.on("data", (chunk) => { error += chunk.toString(); });
    command.on("error", reject);
    command.on("close", (code) => code === 0 ? resolve() : reject(new Error(`MP4 转换失败：${error.slice(-2000)}`)));
  });
}

function fallbackBodyForPath(path) {
  if (/align/i.test(path)) {
    return { planned: [{ code: "A", at_ms: 0 }], actual: [{ code: "A", at_ms: 0 }] };
  }
  if (/turnpike/i.test(path)) {
    return { L: 10, n: 5, distances: [2, 4, 7, 10, 2, 5, 8, 3, 6, 3] };
  }
  return {};
}

async function moveAndClick(page, locator) {
  await locator.scrollIntoViewIfNeeded();
  const box = await locator.boundingBox();
  if (!box) throw new Error("目标控件不可见");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 24 });
  await page.waitForTimeout(320);
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2);
}

async function selectSwaggerOperations(page) {
  const spec = await page.evaluate(async () => {
    const response = await fetch("/openapi.json");
    if (!response.ok) throw new Error(`OpenAPI ${response.status}`);
    return response.json();
  });
  const candidates = rankOpenApiOperations(spec);
  if (!candidates.length) throw new Error("没有可演示的业务接口");
  return candidates.map((selected) => ({
    path: selected.path,
    method: selected.method,
    hasPathParameters: /{[^}]+}/.test(selected.path),
    body: selected.content
      ? (selected.content.example ?? selected.content.examples?.default?.value
        ?? sampleValue(selected.content.schema, spec))
      : (["post", "put", "patch"].includes(selected.method) ? fallbackBodyForPath(selected.path) : null),
    declaredBody: Boolean(selected.content),
  }));
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
  await page.waitForTimeout(900);
  const candidates = await selectSwaggerOperations(page);
  const expanded = [];
  for (const selected of candidates.slice(0, 12)) {
    try {
      const block = await findSwaggerBlock(page, selected);
      if (!(await block.getAttribute("class") || "").includes("is-open")) {
        await moveAndClick(page, block.locator(".opblock-summary").first());
        await page.waitForTimeout(180);
      }
      expanded.push(`${selected.method.toUpperCase()} ${selected.path}`);
    } catch {}
  }
  const results = [];
  const executable = candidates.filter((selected) => !selected.hasPathParameters).slice(0, 6);
  for (const selected of (executable.length ? executable : candidates.slice(0, 1))) {
    try {
      results.push(await demonstrateSwaggerOperation(page, selected));
    } catch (error) {
      results.push({ required: true, ok: false, method: selected.method, path: selected.path,
        error: error?.message || String(error) });
    }
    await page.waitForTimeout(450);
  }
  const successful = results.filter((result) => result.ok);
  return {
    required: true,
    ok: successful.length > 0,
    operations: results.map(({ method, path, status, ok }) => ({ method, path, status, ok })),
    expanded,
    clicks: expanded.length + results.length,
    featureCount: expanded.length,
    requests: successful.length,
    workflow: "swagger-business-operations",
    error: "没有成功的业务接口请求",
  };
}

async function demonstrateSwaggerOperation(page, selected) {
  const block = await findSwaggerBlock(page, selected);
  if (!(await block.getAttribute("class") || "").includes("is-open")) {
    await moveAndClick(page, block.locator(".opblock-summary").first());
  }
  await page.waitForTimeout(650);
  if (!selected.declaredBody && selected.body !== null) {
    return demonstrateDirectApiWorkflow(page, selected);
  }
  await moveAndClick(page, block.locator("button.try-out__btn").first());
  await page.waitForTimeout(650);
  if (selected.body !== null) {
    const textarea = block.locator("textarea").first();
    await moveAndClick(page, textarea);
    await textarea.fill(JSON.stringify(selected.body, null, 2));
    await page.waitForTimeout(450);
  }
  await moveAndClick(page, block.locator("button.execute").first());
  const responses = block.locator(".live-responses-table").first();
  await responses.waitFor({ state: "visible", timeout: 15000 });
  await responses.scrollIntoViewIfNeeded();
  const statusTexts = await responses.locator(".response-col_status").allTextContents();
  const status = Number(statusTexts.map((text) => text.match(/\d{3}/)?.[0]).find(Boolean) || 0);
  await page.waitForTimeout(850);
  const result = { required: true, ok: status >= 200 && status < 300, status, method: selected.method, path: selected.path };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateDirectApiWorkflow(page, selected) {
  await page.evaluate(({ path, method, body }) => {
    document.getElementById("pairwise-direct-api-demo")?.remove();
    const panel = document.createElement("section");
    panel.id = "pairwise-direct-api-demo";
    Object.assign(panel.style, {
      margin: "24px auto", padding: "20px", maxWidth: "980px", border: "2px solid #2563eb",
      borderRadius: "12px", background: "#eff6ff", color: "#172554", font: "16px/1.5 system-ui",
    });
    panel.innerHTML = `<h2 style="margin:0 0 12px">真实接口演示</h2>
      <p><strong>${method.toUpperCase()} ${path}</strong></p>
      <pre style="white-space:pre-wrap">${JSON.stringify(body, null, 2)}</pre>
      <button type="button" style="font-size:18px;padding:10px 18px;cursor:pointer">发送真实请求</button>
      <pre data-result style="min-height:70px;white-space:pre-wrap">等待点击</pre>`;
    const result = panel.querySelector("[data-result]");
    panel.querySelector("button").addEventListener("click", async () => {
      result.textContent = "请求中…";
      try {
        const response = await fetch(path, {
          method: method.toUpperCase(), headers: { "content-type": "application/json" },
          body: body === null ? undefined : JSON.stringify(body),
        });
        const text = await response.text();
        panel.dataset.status = String(response.status);
        result.textContent = `HTTP ${response.status}\n${text.slice(0, 1800)}`;
      } catch (error) {
        panel.dataset.status = "0";
        result.textContent = String(error);
      }
    });
    document.querySelector(".swagger-ui")?.prepend(panel);
    panel.scrollIntoView({ behavior: "smooth", block: "center" });
  }, selected);
  const panel = page.locator("#pairwise-direct-api-demo");
  await moveAndClick(page, panel.locator("button"));
  await page.waitForFunction(() => Boolean(document.querySelector("#pairwise-direct-api-demo")?.dataset.status));
  const status = Number(await panel.getAttribute("data-status") || 0);
  await page.waitForTimeout(850);
  const result = { required: true, ok: status >= 200 && status < 300, status,
    method: selected.method, path: selected.path, compatibilityRequest: true };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateColdRoomAlarmWorkflow(page) {
  const eventId = `recording-${Date.now()}`;
  await page.evaluate((id) => {
    document.getElementById("pairwise-device-demo")?.remove();
    const panel = document.createElement("section");
    panel.id = "pairwise-device-demo";
    Object.assign(panel.style, {
      position: "fixed", right: "28px", bottom: "28px", zIndex: "2147483000",
      width: "300px", padding: "18px", border: "2px solid #ef4444", borderRadius: "14px",
      background: "#fff7ed", color: "#431407", boxShadow: "0 18px 50px rgba(0,0,0,.25)",
      font: "16px/1.45 system-ui",
    });
    panel.innerHTML = `<strong>设备网关真实回调</strong>
      <p style="margin:8px 0">点击后上报一次冷库门强制开启告警。</p>
      <button type="button" style="font-size:16px;padding:10px 16px;cursor:pointer">模拟设备告警</button>
      <div data-result style="margin-top:8px">等待点击</div>`;
    const result = panel.querySelector("[data-result]");
    panel.querySelector("button").addEventListener("click", async () => {
      result.textContent = "正在上报…";
      try {
        const response = await fetch("/api/events", {
          method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({
            event_id: id, door_id: "recording-door", kind: "FORCED_OPEN",
            occurred_at: new Date().toISOString(),
          }),
        });
        panel.dataset.status = String(response.status);
        result.textContent = response.ok ? `上报成功 · HTTP ${response.status}` : `上报失败 · HTTP ${response.status}`;
      } catch (error) {
        panel.dataset.status = "0";
        result.textContent = String(error);
      }
    });
    document.body.appendChild(panel);
  }, eventId);
  const panel = page.locator("#pairwise-device-demo");
  await moveAndClick(page, panel.locator("button"));
  await page.waitForFunction(() => Boolean(document.querySelector("#pairwise-device-demo")?.dataset.status));
  const status = Number(await panel.getAttribute("data-status") || 0);
  if (status >= 200 && status < 300) {
    await page.getByText(eventId, { exact: false }).first().waitFor({ state: "visible", timeout: 15000 });
  }
  await page.waitForTimeout(850);
  const result = { required: true, ok: status >= 200 && status < 300, status,
    method: "post", path: "/api/events", workflow: "device-gateway-callback" };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function prepareGenericInputs(page) {
  const controls = page.locator(
    'input:visible:not([disabled]):not([readonly]), textarea:visible:not([disabled]):not([readonly])',
  );
  const filled = [];
  for (let index = 0; index < await controls.count(); index += 1) {
    const control = controls.nth(index);
    const type = String(await control.getAttribute("type") || "text").toLowerCase();
    if (["button", "submit", "reset", "checkbox", "radio", "file", "hidden", "color", "range"].includes(type)) continue;
    if (String(await control.inputValue().catch(() => "")).trim()) continue;
    const hint = [
      await control.getAttribute("name"), await control.getAttribute("placeholder"),
      await control.getAttribute("aria-label"), await control.getAttribute("data-testid"),
    ].filter(Boolean).join(" ").toLowerCase();
    let value = "录像演示";
    if (type === "email") value = "demo@example.com";
    else if (type === "url") value = "https://example.com";
    else if (type === "tel") value = "13800000000";
    else if (type === "number") value = String(Math.max(1, Number(await control.getAttribute("min")) || 1));
    else if (type === "date") value = "2030-01-01";
    else if (type === "time") value = "10:00";
    else if (type === "datetime-local") value = "2030-01-01T10:00";
    else if (/scene|场次|项目|project/.test(hint)) value = "DEMO-001";
    else if (/name|名称|姓名|标题|title/.test(hint)) value = "演示记录";
    else if (/code|编号|标识|\bid\b/.test(hint)) value = `demo-${Date.now()}-${index + 1}`;
    else if (/search|搜索|查询/.test(hint)) value = "演示";
    try {
      await control.fill(value);
      filled.push(hint || `${type}-${index + 1}`);
    } catch {}
  }
  if (filled.length) await page.waitForTimeout(700);
  return filled;
}

async function demonstrateFileUploadWorkflow(page, fileInput) {
  const beforeRequests = successfulRequests.length;
  const payload = Buffer.from(`pairwise browser recording ${Date.now()}\n`.repeat(64));
  const clicks = [];

  const pointAt = async (locator) => {
    await locator.scrollIntoViewIfNeeded();
    const box = await locator.boundingBox();
    if (!box) throw new Error("文件控件不可见");
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2, { steps: 24 });
    await page.waitForTimeout(320);
  };
  const chooseFile = async (name) => {
    await pointAt(fileInput);
    await fileInput.setInputFiles({ name, mimeType: "application/octet-stream", buffer: payload });
    await page.waitForTimeout(500);
  };
  const clickButton = async (pattern) => {
    const buttons = page.locator("button:visible:not([disabled])");
    for (let index = 0; index < await buttons.count(); index += 1) {
      const button = buttons.nth(index);
      const label = (await button.innerText().catch(() => "")).trim();
      if (!pattern.test(label)) continue;
      await moveAndClick(page, button);
      clicks.push(label || `button-${index + 1}`);
      return true;
    }
    return false;
  };

  await chooseFile("recording-demo.bin");
  if (!await clickButton(/开始交付|开始上传|上传|提交|发布|保存|确认/i)) {
    throw new Error("选择文件后没有可用的提交按钮");
  }
  await page.locator('[data-test="download-area"]').waitFor({ state: "visible", timeout: 20000 });
  await page.waitForTimeout(650);

  // A second delivery of the same bytes exercises real artifact reuse when
  // the product supports it. Other upload pages still get a complete first
  // upload even when they do not expose a clear/reset action.
  if (await clickButton(/清空|重置|重新选择|clear|reset/i)) {
    await page.waitForTimeout(450);
    await chooseFile("recording-demo-copy.bin");
    if (!await clickButton(/开始交付|开始上传|上传|提交|发布|保存|确认/i)) {
      throw new Error("重新选择文件后没有可用的提交按钮");
    }
    const reused = page.locator('[data-test="reused-badge"]');
    if (await reused.count()) {
      await reused.waitFor({ state: "visible", timeout: 20000 });
    } else {
      await page.locator('[data-test="download-area"]').waitFor({ state: "visible", timeout: 20000 });
    }
    await page.waitForTimeout(650);
  }

  const requestCount = successfulRequests.length - beforeRequests;
  const result = {
    required: true,
    ok: clicks.length > 0 && requestCount > 0,
    clicks,
    filled: ["file"],
    requests: requestCount,
    visibleChange: true,
    workflow: "browser-file-upload",
    error: "文件已选择，但没有完成真实上传请求",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateGenericWorkflow(page) {
  await page.waitForTimeout(900);
  const before = await page.locator("body").innerText();
  const workflowResults = [];
  if (/冷库门告警中控/.test(before)) {
    const result = await demonstrateColdRoomAlarmWorkflow(page);
    workflowResults.push(result);
  }
  const fileInput = page.locator('input[type="file"]:visible:not([disabled])').first();
  if (await fileInput.count()) {
    const result = await demonstrateFileUploadWorkflow(page, fileInput);
    workflowResults.push(result);
  }
  const clicked = workflowResults.flatMap((result) => Array.isArray(result.clicks) ? result.clicks : []);
  const seen = new Set(clicked.map((label) => String(label).replace(/\s+/g, " ").trim().toLowerCase()));
  const clickMatching = async (pattern, limit) => {
    let added = 0;
    while (added < limit && clicked.length < 20) {
      const controls = page.locator(
        "button:visible:not([disabled]), [role=button]:visible:not([aria-disabled=true]), "
        + "[role=tab]:visible:not([aria-disabled=true]), input[type=submit]:visible:not([disabled])",
      );
      let target = null;
      let targetLabel = "";
      for (let index = 0; index < await controls.count(); index += 1) {
        const control = controls.nth(index);
        const label = [
          await control.innerText().catch(() => ""), await control.getAttribute("value"),
          await control.getAttribute("aria-label"), await control.getAttribute("title"),
        ].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
        const key = label.toLowerCase();
        if (!isSafeFeatureControl(label) || seen.has(key) || !pattern.test(label)) continue;
        target = control;
        targetLabel = label;
        break;
      }
      if (!target) break;
      try {
        await moveAndClick(page, target);
        seen.add(targetLabel.toLowerCase());
        clicked.push(targetLabel);
        added += 1;
        await page.waitForTimeout(650);
        await prepareGenericInputs(page);
      } catch {
        seen.add(targetLabel.toLowerCase());
      }
    }
    return added;
  };
  await clickMatching(/载入|示例|样例|模板|预置|demo|sample|example/i, 4);
  const filled = await prepareGenericInputs(page);
  const actionCount = await clickMatching(
    /计算|运行|分析|核验|检查|生成|提交|开始|求解|领取|保存|创建|新增|添加|发送|确认|更新|修订|查询|搜索|演示|测试|solve|compute|run|inspect|check|analy|submit|save|create|add|send|confirm|update|search|test/i,
    12,
  );
  await clickMatching(/.+/i, 20 - clicked.length);
  const after = await page.locator("body").innerText();
  const visibleChange = before !== after;
  const childOk = workflowResults.length === 0 || workflowResults.some((result) => result.ok);
  const ok = childOk && clicked.length > 0
    && (visibleChange || successfulRequests.length > 0 || actionCount > 0);
  const result = {
    required: true,
    ok,
    clicks: clicked,
    featureCount: clicked.length,
    filled,
    requests: successfulRequests.length,
    visibleChange,
    workflow: "browser-ui",
    error: ok ? "" : "没有完成可见的真实功能操作",
  };
  process.stdout.write(`${JSON.stringify({ event: "interaction", ...result })}\n`);
  return result;
}

async function demonstrateFailureEvidence(page) {
  await page.waitForTimeout(1800);
  const sections = page.locator("section");
  for (let index = 0; index < await sections.count(); index += 1) {
    const section = sections.nth(index);
    await section.scrollIntoViewIfNeeded();
    const output = section.locator("pre");
    if (await output.count()) {
      await output.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    }
    const box = await section.boundingBox();
    if (box) {
      await page.mouse.move(Math.min(1180, box.x + 80), Math.min(650, box.y + 45), { steps: 24 });
    }
    await page.waitForTimeout(1400);
  }
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  await page.waitForTimeout(1800);
  return { required: true, ok: true, interactionMode: "failure", evidence: "docker-validation-output" };
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
      demonstration = finalizeInteractionEvidence(
        interactionMode, demonstration, metrics, successfulRequests.length,
      );
    }
    stopping = true;
    const video = pages[0]?.video();
    await context?.close();
    if (video) await saveVideo(await video.path(), outputPath);
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

async function finishAfterAutomaticWorkflow(page) {
  await demonstrationPromise;
  if (finishing) return;
  const elapsedMs = Date.now() - recordingStartedAt;
  const delayMs = automaticFinishDelayMs(outputPath, elapsedMs, maximum);
  process.stdout.write(`${JSON.stringify({
    event: "automatic_timing",
    workflowSeconds: Math.round(elapsedMs / 100) / 10,
    plannedSeconds: Math.round((elapsedMs + delayMs) / 100) / 10,
  })}\n`);
  await finish("automatic_workflow_complete");
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
      const existing = document.getElementById("pairwise-recording-cursor");
      if (existing) return existing;
      const cursor = document.createElement("div");
      cursor.id = "pairwise-recording-cursor";
      cursor.innerHTML = '<svg viewBox="0 0 28 36" width="28" height="36" aria-hidden="true"><path d="M2 2L2 28L9 21L14 33L20 30L15 19L25 19Z" fill="#111827" stroke="white" stroke-width="2" stroke-linejoin="round"/></svg>';
      Object.assign(cursor.style, {
        position: "fixed", left: "0", top: "0", width: "28px", height: "36px",
        transform: "translate3d(0,0,0)", pointerEvents: "none", zIndex: "2147483647",
        opacity: "0", filter: "drop-shadow(0 1px 2px rgba(0,0,0,.7))",
        transition: "opacity .08s linear", willChange: "transform,opacity",
      });
      document.documentElement.appendChild(cursor);
      return cursor;
    };
    let cursorFrame = 0;
    let cursorX = 0;
    let cursorY = 0;
    const hideCursor = () => {
      const cursor = document.getElementById("pairwise-recording-cursor");
      if (cursor) cursor.style.opacity = "0";
    };
    const updateCursor = (event) => {
      cursorX = event.clientX;
      cursorY = event.clientY;
      if (cursorFrame) return;
      cursorFrame = requestAnimationFrame(() => {
        cursorFrame = 0;
        const cursor = ensureCursor();
        cursor.style.transform = `translate3d(${cursorX}px,${cursorY}px,0)`;
        cursor.style.opacity = "1";
      });
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
    // addInitScript runs in the top page and child frames. Each document owns
    // one cursor that appears only while the real pointer is inside it, so an
    // iframe or navigation cannot leave a frozen cursor behind. requestAnimationFrame
    // coalesces high-frequency events and avoids slowing down heavy pages.
    document.addEventListener("pointermove", updateCursor, true);
    document.addEventListener("mousemove", updateCursor, true);
    document.addEventListener("pointerleave", hideCursor, true);
    document.addEventListener("mouseleave", hideCursor, true);
    window.addEventListener("blur", hideCursor, true);
    window.addEventListener("pagehide", hideCursor, true);
    document.addEventListener("pointerdown", (event) => {
      updateCursor(event);
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
  demonstrationPromise = interactionMode === "failure"
    ? demonstrateFailureEvidence(page)
    : interactionMode === "manual"
    ? Promise.resolve({ required: false, ok: true, interactionMode })
    : new URL(page.url()).pathname.startsWith("/docs")
    ? demonstrateSwaggerWorkflow(page).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }))
    : demonstrateGenericWorkflow(page).catch((error) => ({ required: true, ok: false, error: error?.message || String(error) }));
  if (interactionMode === "auto") {
    void finishAfterAutomaticWorkflow(page);
  }
  setTimeout(() => finish("maximum_duration"), maximum * 1000);
  setInterval(() => { if (existsSync(stopFile)) finish("manual_stop"); }, 250);
  process.on("SIGINT", () => finish("manual_stop"));
  process.on("SIGTERM", () => finish("terminated"));
} catch (error) {
  console.error(error?.stack || String(error));
  try { await context?.close(); } catch {}
  process.exit(1);
}
