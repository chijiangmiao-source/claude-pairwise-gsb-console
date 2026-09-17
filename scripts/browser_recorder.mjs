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

async function demonstrateReadOnlyDocs(page) {
  if (!new URL(page.url()).pathname.startsWith("/docs")) return;
  await page.waitForTimeout(4000);
  const operations = page.locator(".opblock-summary");
  const count = Math.min(2, await operations.count());
  for (let index = 0; index < count && !stopping; index += 1) {
    const operation = operations.nth(index);
    await operation.scrollIntoViewIfNeeded();
    await operation.click({ timeout: 5000 });
    await page.waitForTimeout(5000);
  }
  if (count && !stopping) {
    const first = operations.first();
    await first.scrollIntoViewIfNeeded();
    await first.click({ timeout: 5000 });
    await page.waitForTimeout(5000);
    await first.click({ timeout: 5000 });
    await page.waitForTimeout(5000);
  }
  if (!stopping) {
    await page.locator("body").press("Home");
    await page.waitForTimeout(2000);
  }
}

async function finish(reason) {
  if (stopping) return;
  stopping = true;
  try {
    const pages = context?.pages() || [];
    const video = pages[0]?.video();
    await context?.close();
    if (video) await copyFile(await video.path(), outputPath);
    process.stdout.write(`${JSON.stringify({ event: "finished", reason })}\n`);
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
    };
    document.documentElement ? observeDocument() : document.addEventListener("DOMContentLoaded", observeDocument, { once: true });
    document.addEventListener("pointerdown", (event) => {
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
  await page.goto(url, { waitUntil: "domcontentloaded", timeout: 60000 });
  await page.bringToFront();
  process.stdout.write(`${JSON.stringify({ event: "ready", url: page.url() })}\n`);
  demonstrateReadOnlyDocs(page).catch(() => {});
  setTimeout(() => finish("maximum_duration"), maximum * 1000);
  setInterval(() => { if (existsSync(stopFile)) finish("manual_stop"); }, 250);
  process.on("SIGINT", () => finish("manual_stop"));
  process.on("SIGTERM", () => finish("terminated"));
} catch (error) {
  console.error(error?.stack || String(error));
  try { await context?.close(); } catch {}
  process.exit(1);
}
