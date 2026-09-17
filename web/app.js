const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = {
  page: "dashboard",
  pages: { tasks: 1, pairs: 1, reviews: 1, codex: 1, bugs: 1, evidence: 1, exports: 1 },
  sizes: { tasks: 20, pairs: 20, reviews: 20, codex: 20, bugs: 20, evidence: 20, exports: 20 },
  filters: { tasks: {}, pairs: {}, reviews: {}, evidence: {}, exports: {} },
  reviewSelection: new Set(), reviewItems: [],
  exportSelection: new Set(), exportItems: [], preflight: null,
  recheckRunning: new Set(),
  soloQaSyncRunning: new Set(),
  soloQaRepairRunning: new Set(),
  helperReady: false, helperVersion: "", helperRequests: new Map(),
};
const titles = {
  dashboard: "数据概览", tasks: "题目池", pairs: "A/B 项目", reviews: "复核与人工确认",
  codex: "Codex 作业", bugs: "Bug 复现", evidence: "产物验收与录像", exports: "导出轮次", settings: "系统设置",
};
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
})[char]);
const statusLabels = {
  passed: "通过", failed: "失败", rejected: "未通过", draft: "草稿", confirmed: "已确认",
  suggested_revision: "建议修改", fact_conflict: "事实冲突", ready: "可提交",
  blocked: "待补资料", not_submitted: "待提交", ready_to_submit: "待提交",
  submitting: "提交中", qc_pending: "质检中", qc_passed: "质检通过",
  submitted: "已提交", needs_fix: "待返修", discarded: "已废弃", needs_review: "待重新确认", missing: "缺失",
  SUBMITTED: "质检中", QC_PASSED: "质检通过", PENDING_FIX: "待返修", DISCARDED: "已废弃",
  repository: "仓库准备", ready_to_start: "等待启动", development: "A/B 开发",
  artifact_validation: "Docker 验收", difficulty_review: "实际难度复评",
  difficulty_rejected: "难度复评未通过", starting: "正在启动项目", recording: "正在录制",
  gsb_ready: "等待 GSB", gsb_confirmation: "GSB 确认",
  artifact_failed: "Docker 验收失败", development_failed: "开发失败",
  recording_failed: "录像失败", task_replacement: "正在自动换题",
  replaced: "已自动换题", replacement_failed: "自动换题失败", completed: "已完成",
  queued: "排队中", running: "运行中", review: "待复核", developing: "开发中", stopping: "正在保存",
  waiting_retry: "等待重试", checkpointing: "正在收尾", active: "进行中",
  used: "已使用", candidate: "待复核", converted: "已转任务", recording: "正在录制",
  reproducing: "正在复现", reproduction_failed: "复现失败", exported: "已导出", cancelled: "已取消",
  applied: "已应用", rechecking: "复检中",
};
const badge = (value) => `<span class="badge ${esc(value)}">${esc(statusLabels[value] || value || "—")}</span>`;
const taskTypeLabels = { zero_to_one: "0–1", feature: "Feature 迭代", bugfix: "Bug 修复" };
const categoryTone = (value) => value === "纯前端" ? "frontend" : value === "全栈" ? "fullstack" : value === "纯后端" ? "backend" : "neutral";
const projectCategoryBadge = (value) => `<span class="category-badge ${categoryTone(value)}">${esc(value || "未记录")}</span>`;
const taskTypeBadge = (value) => `<span class="task-type-badge ${esc(value || "unknown")}">${esc(taskTypeLabels[value] || value || "未记录")}</span>`;
const date = (value) => value ? new Date(value).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" }) : "—";
const recheckBadge = (row) => state.recheckRunning.has(row.pair_id) ? badge("rechecking") : row.recheck_applied_at ? badge("applied") : row.recheck_status ? badge(row.recheck_status) : badge("未复检");
const difficultyDisplay = (row) => {
  const original = row.original_difficulty || row.difficulty || "—";
  const assessed = row.assessed_difficulty || "";
  if (assessed) return `<span class="difficulty">出题 ${esc(original)} → 实际 ${esc(assessed)}</span>${row.difficulty_review_status ? `<small class="block">${badge(row.difficulty_review_status)}</small>` : ""}`;
  if (row.difficulty_review_status) return `<span class="difficulty">出题 ${esc(original)}</span><small class="block">${badge(row.difficulty_review_status)}</small>`;
  return `<span class="difficulty">${esc(row.difficulty || original)}</span>`;
};
const pairIdentity = (value) => value ? `<span class="pair-identity"><span class="pair-identity-label">Pair 唯一 ID</span><code>${esc(value)}</code><button type="button" class="pair-copy" onclick="copyPairId('${esc(value)}')">复制</button></span>` : '<span class="pair-identity"><span class="pair-identity-label">尚未创建 Pair</span></span>';

async function copyPairId(value) {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const input = document.createElement("textarea");
    input.value = value; input.style.position = "fixed"; input.style.opacity = "0";
    document.body.appendChild(input); input.select(); document.execCommand("copy"); input.remove();
  }
  notify(`已复制 Pair 唯一 ID：${value}`);
}

async function locatePair() {
  const input = $("#pair-locator"), value = String(input?.value || "").trim();
  if (!/^pair-[a-zA-Z0-9]+$/.test(value)) return notify("请输入完整的 Pair 唯一 ID，例如 pair-686fbf9c7d664fd5", true);
  try {
    await api(`/api/pairs/${encodeURIComponent(value)}`);
    await showPair(value);
  } catch (error) { notify(`没有找到该 Pair：${error.message}`, true); }
}

async function api(path, options = {}) {
  const response = await fetch(path, { cache: "no-store", headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json().catch(() => ({ error: `HTTP ${response.status}` }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
function notify(message, error = false) {
  const notice = $("#notice"); notice.textContent = message; notice.className = `notice${error ? " error" : ""}`;
  window.setTimeout(() => notice.classList.add("hidden"), 6500);
}
function helperCall(type, payload = {}) {
  if (!state.helperReady) return Promise.reject(new Error("提交助手未连接，请在 Chrome 扩展页加载或重新加载本期 GSB 提交小助手"));
  const requestId = `gsb-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => { state.helperRequests.delete(requestId); reject(new Error("提交助手等待超时，请检查 SOLO-QA 页面和网络")); }, 30 * 60 * 1000);
    state.helperRequests.set(requestId, { resolve, reject, timer });
    window.postMessage({ source: "pairwise-gsb-console", type, requestId, payload }, window.location.origin);
  });
}
window.addEventListener("message", (event) => {
  if (event.source !== window || event.origin !== window.location.origin) return;
  const message = event.data;
  if (!message || message.source !== "solo-qa-gsb-helper") return;
  if (message.type === "PAIRWISE_GSB_BRIDGE_READY") {
    const changed = !state.helperReady || state.helperVersion !== String(message.payload?.version || "");
    state.helperReady = true; state.helperVersion = String(message.payload?.version || "");
    if (changed && state.page === "exports") renderExports();
    return;
  }
  const pending = state.helperRequests.get(message.requestId);
  if (!pending) return;
  window.clearTimeout(pending.timer); state.helperRequests.delete(message.requestId);
  if (message.type === "PAIRWISE_GSB_BRIDGE_RESULT") pending.resolve(message.payload || {});
  else if (message.type === "PAIRWISE_GSB_BRIDGE_ERROR") pending.reject(new Error(message.payload?.error || "提交助手执行失败"));
});
function loading() { $("#content").innerHTML = '<div class="loading">正在读取数据…</div>'; }
async function render() {
  if ($("#dialog")?.open) $("#dialog").close();
  loading(); $("#page-title").textContent = titles[state.page];
  $$("#nav button").forEach((button) => button.classList.toggle("active", button.dataset.page === state.page));
  syncUrl();
  try { await pages[state.page](); }
  catch (error) { $("#content").innerHTML = `<div class="card empty">读取失败：${esc(error.message)}</div>`; }
}

const pages = {
  dashboard: async () => {
    const data = await api("/api/dashboard"), summary = data.summary;
    const max = Math.max(1, ...data.trend24h.map((item) => item.count));
    $("#content").innerHTML = `<div class="grid stats">
      <div class="stat"><small>全部 Pair</small><strong>${summary.totalPairs}</strong><em>一个 Pair 只计一条数据</em></div>
      <div class="stat"><small>累计完成 Pair</small><strong>${summary.completedPairs}</strong><em>默认或人工确认后的正式记录</em></div>
      <div class="stat"><small>当前活动 Pair</small><strong>${summary.activePairs}</strong><em>排队、开发与评审</em></div>
      <div class="stat"><small>已确认 GSB</small><strong>${summary.confirmedGsb}</strong><em>A better / Same / B better</em></div></div>
      <div class="grid two-col"><div class="card"><div class="trend-heading"><div><h2>24 小时产出趋势</h2><p class="sub">按完成时间统计 A/B Pair，每根柱子上方直接显示产出数</p></div><div class="trend-total"><small>24 小时完成</small><strong>${summary.completedPairs24h}</strong><span>个 Pair</span></div></div><div class="bar-chart">${data.trend24h.map((item, index) => { const count = Number(item.count || 0); return `<div class="bar-wrap" title="${esc(item.hour)}:00 · ${count} 个 Pair"><b class="bar-count">${count}</b><div class="bar ${count ? "" : "zero"}" style="height:${count ? Math.max(8, count / max * 118) : 2}px"></div><small>${index % 3 === 0 || index === data.trend24h.length - 1 ? `${esc(item.label)}时` : ""}</small></div>`; }).join("")}</div></div>
      <div class="card"><h2>GSB 分布</h2><p class="sub">只统计已确认记录</p>${distribution(data.verdicts)}</div></div>
      <div class="grid dashboard-three" style="margin-top:16px"><div class="card"><h2>Pair 状态</h2><p class="sub">按 Pair 统计，不拆分 A/B</p>${distribution(data.pairStatus)}</div><div class="card"><h2>系统类型统计</h2><p class="sub">纯后端、纯前端与全栈按 Pair 计数</p>${distribution(data.projectCategories)}</div><div class="card"><h2>任务类型统计</h2><p class="sub">0–1、Feature 迭代与 Bug 修复按 Pair 计数</p>${distribution(data.taskTypes)}</div></div>`;
  },
  tasks: async () => {
    const filters = state.filters.tasks;
    const [data, automation] = await Promise.all([
      api(`/api/tasks?${queryString("tasks")}`), api("/api/automation"),
    ]);
    const automationAction = automation.enabled
      ? `<span class="badge running">自动运行中</span><button class="danger" onclick="toggleAutomation(false)">停止自动运行</button>`
      : `<button class="primary" onclick="toggleAutomation(true)">一键自动运行完整流程</button>`;
    const stageText = (automation.stages || []).map((item) => `${statusLabels[item.stage] || item.stage} ${item.count}`).join(" · ") || "等待启动";
    $("#content").innerHTML = `<div class="card"><div class="toolbar"><div><h2>题目池</h2><p class="sub">困难与地狱题目进入 A/B</p></div><div class="toolbar-group"><button class="secondary" onclick="importTasks()">导入历史困难题</button>${automationAction}</div></div>
      <div class="automation-strip ${automation.enabled ? "enabled" : ""}"><div><strong>${automation.enabled ? "完整流程持续运行" : "完整流程尚未启动"}</strong><small>${automation.enabled ? "现有合格题优先；空位自动补 Pair，题目不足自动出题" : "点击后自动完成仓库准备、A/B 开发、Docker 验收、录像和 GSB"}</small></div><div class="automation-metrics"><span>活动 Pair <b>${automation.activePairs}/${automation.targetPairs}</b></span><span>可用题目 <b>${automation.readyTasks}</b></span><span>${esc(stageText)}</span></div></div>
      ${filterBar("tasks", [["usage", "select", "全部使用状态", filters.usage, [["used", "已使用"], ["unused", "未使用"]]]])}
      ${table(data.items, [
      ["题目", (row) => `<div class="title-cell"><strong>${esc(row.title)}</strong><small>${esc(row.prompt)}</small>${row.locked_by ? pairIdentity(row.locked_by) : ""}</div>`],
      ["任务 / 系统类型", (row) => `<div class="tag-stack">${taskTypeBadge(row.task_type)} ${projectCategoryBadge(row.project_category)}</div>`], ["难度", (row) => `<span class="difficulty">${esc(row.difficulty)}</span>`],
      ["来源", (row) => esc(row.source)], ["状态", (row) => badge(row.status)],
      ["操作", (row) => `<button class="tiny" onclick="showTask('${row.id}')">查看</button> ${row.status === "candidate" ? `<button class="tiny" onclick="validateTask('${row.id}')">复核</button>` : ""} ${row.status === "ready" ? `<button class="tiny" onclick="createPair('${row.id}')">创建 Pair</button>` : ""}`],
    ])}${pager("tasks", data)}</div>`;
  },
  pairs: async () => {
    const filters = state.filters.pairs, data = await api(`/api/pairs?${queryString("pairs")}`);
    $("#content").innerHTML = `<div class="card"><div class="toolbar"><div><h2>A/B 项目</h2><p class="sub">同一 main 基线，大写 A/B 分支，两个独立 Claude 容器</p></div></div>
      ${filterBar("pairs", [["q", "search", "Pair 唯一 ID、项目编号或题目", filters.q], ["task_type", "select", "全部任务类型", filters.task_type, [["zero_to_one", "0–1"], ["feature", "Feature 迭代"], ["bugfix", "Bug 修复"]]], ["project_category", "select", "全部系统类型", filters.project_category, ["纯后端", "纯前端", "全栈"]], ["difficulty", "select", "全部难度", filters.difficulty, ["困难", "地狱"]], ["stage", "select", "全部项目阶段", filters.stage, [["repository", "仓库准备"], ["ready_to_start", "等待启动"], ["development", "A/B 开发"], ["artifact_validation", "Docker 验收"], ["difficulty_review", "实际难度复评"], ["recording", "正在录制"], ["gsb_ready", "等待 GSB"], ["gsb_confirmation", "GSB 确认"], ["completed", "已完成"], ["artifact_failed", "Docker 验收失败"], ["difficulty_rejected", "难度复评未通过"], ["development_failed", "开发失败"], ["recording_failed", "录像失败"], ["task_replacement", "正在自动换题"], ["replacement_failed", "自动换题失败"]]], ["status", "select", "全部项目状态", filters.status, [["queued", "排队中"], ["running", "运行中"], ["review", "待复核"], ["completed", "已完成"], ["failed", "失败"]]]])}
      ${table(data.items, [
      ["项目 / Pair 唯一 ID", (row) => `<div class="title-cell"><strong>${esc(row.title)}</strong>${pairIdentity(row.id)}</div>`],
      ["类型 / 难度", (row) => `<div class="tag-stack">${taskTypeBadge(row.task_type)} ${projectCategoryBadge(row.project_category)} ${difficultyDisplay(row)}</div>`],
      ["阶段", (row) => badge(row.stage)], ["状态", (row) => badge(row.status)], ["GSB", (row) => row.verdict ? badge(row.verdict) : "—"],
      ["操作", (row) => `<button class="tiny" onclick="showPair('${row.id}')">打开</button>`],
    ])}${pager("pairs", data)}</div>`;
  },
  reviews: renderReviews,
  codex: async () => {
    const data = await api(`/api/codex-jobs?page=${state.pages.codex}&size=${state.sizes.codex}`);
    $("#content").innerHTML = `<div class="card"><h2>Codex CLI 作业</h2><p class="sub">除 A/B 开发外的模型节点均在这里留痕</p>${table(data.items, [
      ["作业", (row) => `<div class="title-cell"><strong>${esc(row.job_type)}</strong><small>${esc(row.id)}</small>${row.pair_id ? pairIdentity(row.pair_id) : ""}</div>`],
      ["模型", (row) => esc(row.model)], ["推理强度", (row) => badge(row.reasoning_effort)], ["状态", (row) => badge(row.status)],
      ["尝试", (row) => row.attempt_count], ["开始时间", (row) => date(row.started_at || row.created_at)],
      ["错误", (row) => `<div class="title-cell"><small>${esc(row.error)}</small></div>`],
    ])}${pager("codex", data)}</div>`;
  },
  bugs: async () => {
    const data = await api(`/api/bug-candidates?page=${state.pages.bugs}&size=${state.sizes.bugs}`);
    $("#content").innerHTML = `<div class="card"><h2>真实 Bug 复现</h2><p class="sub">困难或地狱候选必须在两次清洁 Compose 环境得到一致结果</p>${table(data.items, [
      ["候选", (row) => `<div class="title-cell"><strong>${esc(row.title)}</strong><small>${esc(row.actual_result)}</small></div>`],
      ["来源", (row) => `${pairIdentity(row.source_pair_id)}<small class="block">来源 Arm：${esc(row.source_arm)}</small>`], ["难度", (row) => `<span class="difficulty">${esc(row.difficulty)}</span>`],
      ["复现", (row) => `${row.reproduce_count}/2`], ["状态", (row) => badge(row.status)],
      ["操作", (row) => row.status === "awaiting_reproduction" ? `<button class="tiny" onclick="bugAction('${row.id}','reproduce')">双次复现</button>` : row.status === "reproduced" ? `<button class="tiny" onclick="bugAction('${row.id}','convert')">转为 Bug 任务</button>` : "—"],
    ])}${pager("bugs", data)}</div>`;
  },
  evidence: renderEvidence,
  exports: renderExports,
  settings: async () => {
    const [settings, preflight] = await Promise.all([api("/api/settings"), api("/api/preflight")]);
    state.preflight = preflight; $("#runtime-summary").textContent = `Codex ${settings.codex_model} · Claude ${settings.claude_model}`;
    $("#content").innerHTML = `<div class="card"><h2>运行环境预检</h2><p class="sub">Git、Codex、Claude 容器和浏览器录像相互独立</p><div class="preflight">${preflightCard("Git / GitHub", preflight.git?.ok, preflight.git?.gh?.account || preflight.git?.gh?.error || "未认证")}${preflightCard("Codex CLI", preflight.codex?.ok, preflight.codex?.version || preflight.codex?.error)}${preflightCard("Claude Docker", preflight.claude?.ok, preflight.claude?.image_id || preflight.claude?.image)}${preflightCard("浏览器页面录像", preflight.browserRecording?.ok, preflight.browserRecording?.ok ? "Chrome · Playwright · 1280×720" : "需要安装浏览器录像依赖")}</div></div>
      <div class="card" style="margin-top:16px"><h2>默认模型与流程参数</h2><p class="sub">Claude 只用于 A/B 开发；其余模型节点全部使用 Codex CLI</p><div class="setting-list">
      ${setting("codex_model", "Codex 默认模型", settings.codex_model, "出题、复核、找 Bug、GSB 与下一步判断")}${setting("codex_default_effort", "Codex 常规强度", settings.codex_default_effort, "默认 medium")}${setting("codex_bug_effort", "找 Bug 强度", settings.codex_bug_effort, "默认 high")}
      ${setting("gsb_recheck_model", "GSB 复检模型", settings.gsb_recheck_model || "gpt-6-astra", "默认 gpt-6-astra")}${setting("gsb_recheck_effort", "GSB 复检强度", settings.gsb_recheck_effort || "high", "建议 high 或更高")}
      ${setting("claude_model", "Claude 开发模型", settings.claude_model, "仅用于 A/B 开发")}${setting("claude_image", "Claude 容器镜像", settings.claude_image, "从现系统部署配置读取")}${setting("task_generation_max_parallel", "出题并发", settings.task_generation_max_parallel, "与开发并发分开")}${setting("task_pool_min_ready", "题库低水位", settings.task_pool_min_ready, "低于此值自动补题")}${setting("task_pool_target_ready", "题库目标水位", settings.task_pool_target_ready, "每轮补到此数量")}${setting("max_pairs_parallel", "Pair 并发", settings.max_pairs_parallel, "每个 Pair 使用 A/B 两个终端，最多 3 个 Pair（共 6 个终端）", 'type="number" min="1" max="3"')}${setting("ab_prompt_stagger_seconds", "A/B 题面发送间隔", settings.ab_prompt_stagger_seconds ?? 30, "默认先发送 A，30 秒后发送 B；两侧分别从实际发送时刻计时", 'type="number" min="0" max="300"')}${setting("git_author_name", "Git 提交人", settings.git_author_name, "默认刘昱")}${setting("git_author_email", "Git 提交邮箱", settings.git_author_email, "创建远端仓库前必填")}${setting("github_owner", "GitHub Owner", settings.github_owner, "留空时使用 gh 当前账号")}${setting("github_visibility", "仓库可见性", settings.github_visibility, "SOLO-QA 必须能够公开读取，默认 public")}
      </div><div style="margin-top:18px"><button class="primary" onclick="saveSettings()">保存设置</button></div></div>`;
  },
};

async function renderEvidence() {
  const filters = state.filters.evidence, data = await api(`/api/evidence?${queryString("evidence")}`);
  $("#content").innerHTML = `<div class="card"><div class="toolbar"><div><h2>Docker 产物验收与真实操作录像</h2><p class="sub">每行对应一个 Pair 的一个 Arm，可直接播放最终提交的真实演示</p></div></div>
    ${filterBar("evidence", [["q", "search", "Pair 唯一 ID、项目编号、题目或提交", filters.q], ["arm", "select", "全部 Arm", filters.arm, ["A", "B"]], ["task_type", "select", "全部任务类型", filters.task_type, [["zero_to_one", "0–1"], ["feature", "Feature 迭代"], ["bugfix", "Bug 修复"]]], ["project_category", "select", "全部系统类型", filters.project_category, ["纯后端", "纯前端", "全栈"]], ["pair_status", "select", "全部最终数据状态", filters.pair_status, [["completed", "已生成最终数据"], ["running", "流程处理中"], ["review", "等待确认"], ["failed", "流程失败"], ["cancelled", "已取消"]]], ["artifact_status", "select", "全部验收状态", filters.artifact_status, [["queued", "排队中"], ["running", "验收中"], ["passed", "验收通过"], ["failed", "验收失败"]]], ["recording_status", "select", "全部录像状态", filters.recording_status, [["queued", "排队中"], ["recording", "录制中"], ["passed", "录像通过"], ["failed", "录像失败"]]], ["missing", "select", "全部资料", filters.missing, [["1", "只看缺失或失败"]]]])}
    ${table(data.items, [["项目 / Arm / Pair 唯一 ID", (row) => `<div class="title-cell"><strong>${esc(row.title)} · ${row.arm}</strong><small>项目编号：${esc(row.project_number)}</small>${pairIdentity(row.pair_id)}</div>`], ["类型 / 难度", (row) => `<div class="tag-stack">${taskTypeBadge(row.task_type)} ${projectCategoryBadge(row.project_category)} ${difficultyDisplay(row)}</div>`], ["最终提交", (row) => `<span class="code">${esc((row.commit_sha || "").slice(0, 12))}</span>`], ["Docker 验收", (row) => badge(row.artifact_status || "missing")], ["真实操作录像", (row) => `${badge(["starting", "recording", "stopping"].includes(row.latest_attempt_status) ? row.latest_attempt_status : row.recording_status || "missing")}<small class="block">${[recordingFormat(row), row.latest_interaction_mode === "manual" ? "人工操作" : row.latest_attempt_id ? "自动操作" : "", row.capture_mode === "browser" ? "浏览器视口" : row.recording_id ? "历史屏幕录像" : ""].filter(Boolean).join(" · ")} ${row.width || 0}×${row.height || 0} · ${row.duration_seconds || 0}s</small><small class="block">${row.review_status === "confirmed" ? `默认审核通过 · ${esc(row.reviewed_by || "刘昱")}` : "待审核"}</small>`], ["最终数据", finalDataState], ["提交匹配", (row) => row.commit_match ? '<span class="ok">● 匹配</span>' : '<span class="bad">● 未匹配</span>'], ["操作", evidenceActions]])}${pager("evidence", data)}</div>`;
}

function finalDataState(row) {
  if (row.pair_status === "completed") return `${badge("completed")}<small class="block">已生成 GSB 与提交数据</small>`;
  const stateValue = row.pair_stage || row.pair_status || "missing";
  const detail = row.pair_error || (row.pair_status === "failed" ? "当前 Pair 未形成最终数据" : "完成两侧验收和 GSB 后生成");
  return `${badge(stateValue)}<small class="block">${esc(detail)}</small>`;
}

function recordingFormat(row) {
  const path = String(row?.path || "").toLowerCase();
  if (path.endsWith(".mp4")) return "H.264 MP4";
  if (path.endsWith(".webm")) return "WebM";
  if (path.endsWith(".mov")) return "MOV";
  return "";
}

function evidenceActions(row) {
  const play = row.recording_id && row.recording_status === "passed"
    ? `<button class="tiny" onclick="playRecording('${row.recording_id}','${row.arm}','${esc(row.title)}','${row.pair_id}')">播放</button>` : "";
  let record;
  if (row.latest_attempt_status === "recording") {
    record = `<button class="tiny danger" onclick="recordAction('${row.pair_id}','${row.arm}','stop')">停止并保存</button>`;
  } else if (row.latest_attempt_status === "stopping") {
    record = '<button class="tiny" disabled>正在保存 MP4…</button>';
  } else if (row.latest_attempt_status === "starting") {
    record = '<button class="tiny" disabled>正在启动项目…</button>';
  } else if (["difficulty_review", "difficulty_rejected"].includes(row.pair_stage)) {
    record = '<button class="tiny" disabled>等待实际难度复评</button>';
  } else if (!["passed", "failed"].includes(row.artifact_status) || !row.commit_sha) {
    record = '<button class="tiny" disabled>等待 Docker 验收</button>';
  } else {
    record = row.recording_id
      ? `<button class="tiny primary" onclick="recordAction('${row.pair_id}','${row.arm}','start',true)">人工重新录制</button>`
      : row.artifact_status === "failed"
      ? `<button class="tiny danger" onclick="recordAction('${row.pair_id}','${row.arm}','start',false)">录制真实报错</button>`
      : `<button class="tiny primary" onclick="recordAction('${row.pair_id}','${row.arm}','start',false)">一键自动录制</button>`;
  }
  return `${play} ${record} <button class="tiny" onclick="showEvidence('${row.pair_id}','${row.arm}')">详情/历史</button>`;
}

async function renderReviews() {
  const filters = state.filters.reviews, data = await api(`/api/gsb-reviews?${queryString("reviews")}`);
  state.reviewItems = data.items;
  const allSelected = data.items.length > 0 && data.items.every((row) => state.reviewSelection.has(row.pair_id));
  const rows = data.items.map((row) => `<article class="review-list-row">
    <label class="review-select"><input type="checkbox" aria-label="选择 ${esc(row.pair_id)}" ${state.reviewSelection.has(row.pair_id) ? "checked" : ""} onchange="toggleReview('${row.pair_id}',this.checked)"></label>
    <div class="review-summary"><strong>${esc(row.title)}</strong><small>项目编号：${esc(row.project_number)}</small>${pairIdentity(row.pair_id)}<div class="tag-stack">${taskTypeBadge(row.task_type)} ${projectCategoryBadge(row.project_category)} ${difficultyDisplay(row)}</div><small>${date(row.confirmed_at || row.updated_at)}</small></div>
    <div class="review-result"><span>${badge(row.verdict)}</span><span>${recheckBadge(row)}</span></div>
    <div class="review-reason"><span class="mobile-label">公开理由</span><p><b>A：</b>${esc(row.a_reason || "未填写")}</p><p><b>B：</b>${esc(row.b_reason || "未填写")}</p></div>
    <div class="review-confirm"><div>${badge(row.status)}<small>${esc(row.confirmed_by || "未确认")}</small></div><div class="review-row-actions"><button class="tiny" onclick="reviewGsb('${row.pair_id}')">查看/编辑</button><button class="tiny primary" onclick="recheckGsb('${row.pair_id}')">复检</button></div></div>
  </article>`).join("");
  $("#content").innerHTML = `<div class="card review-page"><div class="toolbar review-heading"><div><h2>复核与人工确认</h2><p class="sub">公开理由可用高强度模型复检；事实冲突会阻止正式导出，措辞建议不会阻止确认</p></div><div class="review-batch-actions"><b>已选 ${state.reviewSelection.size} 项</b><label><input type="checkbox" ${allSelected ? "checked" : ""} onchange="toggleReviewPage(this.checked)"> 选择本页</label><button class="secondary" onclick="applyReviewSuggestionsSelected()" ${state.reviewSelection.size ? "" : "disabled"}>批量应用建议</button><button class="primary" onclick="recheckReviewsSelected()" ${state.reviewSelection.size ? "" : "disabled"}>批量复检</button></div></div>
    ${filterBar("reviews", [["q", "search", "Pair 唯一 ID、项目编号、题目、理由或审核人", filters.q], ["task_type", "select", "全部任务类型", filters.task_type, [["zero_to_one", "0–1"], ["feature", "Feature 迭代"], ["bugfix", "Bug 修复"]]], ["project_category", "select", "全部系统类型", filters.project_category, ["纯后端", "纯前端", "全栈"]], ["difficulty", "select", "全部难度", filters.difficulty, ["困难", "地狱"]], ["verdict", "select", "全部结论", filters.verdict, ["A better", "Same", "B better"]], ["status", "select", "全部确认状态", filters.status, [["draft", "草稿"], ["confirmed", "已确认"]]], ["recheck_status", "select", "全部复检状态", filters.recheck_status, [["passed", "复检通过"], ["suggested_revision", "建议修改"], ["fact_conflict", "事实冲突"]]], ["date_from", "date", "记录日期从", filters.date_from], ["date_to", "date", "记录日期到", filters.date_to]])}
    <div class="review-list"><div class="review-list-head"><span></span><span>项目 / Pair 唯一 ID / 记录时间</span><span>结论 / 复检</span><span>公开理由</span><span>确认 / 操作</span></div>${rows || '<div class="empty">暂无记录</div>'}</div>${pager("reviews", data)}</div>`;
}

function readinessDetail(row, expanded = false) {
  const issues = Array.isArray(row.readiness_issues) ? row.readiness_issues : [];
  if (!issues.length) return `<div class="readiness-detail ready">${badge("ready")}<small>资料完整，可进行提交前检查</small></div>`;
  return `<div class="readiness-detail blocked ${expanded ? "expanded" : ""}">${badge("blocked")}<strong>缺少 ${issues.length} 项</strong><ul>${issues.map((issue) => `<li>${esc(issue)}</li>`).join("")}</ul></div>`;
}

async function renderExports() {
  const filters = state.filters.exports, data = await api(`/api/deliveries?${queryString("exports")}`); state.exportItems = data.items;
  const helperStatus = state.helperReady ? `<span class="ok">● 提交助手已连接 ${esc(state.helperVersion)}</span>` : '<span class="bad">● 提交助手未连接</span><small>请从部署目录加载 chrome-solo-qa-gsb-helper</small>';
  const repairableSelected = data.items.filter((row) => state.exportSelection.has(row.pair_id) && row.submission_status === "needs_fix").length;
  $("#content").innerHTML = `<div class="card"><div class="toolbar export-heading"><div><h2>完成 Pair 导出与正式提交</h2><p class="sub">本期 Pair-wise 表单：一道题、双轨迹、双产物、双录像和一份 GSB 对比理由</p><div class="helper-status">${helperStatus}</div></div><div class="toolbar-group"><b>已选 ${state.exportSelection.size} 项 · 待返修 ${repairableSelected} 项</b><button class="secondary" onclick="syncSoloQa()" ${state.helperReady ? "" : "disabled"}>同步全部状态</button><button class="secondary" onclick="preflightSelected()" ${state.exportSelection.size ? "" : "disabled"}>提交前检查</button><button class="secondary" onclick="selectReady()">只选可提交项</button><button class="secondary" onclick="selectNeedsFix()">只选待返修项</button><button class="secondary" onclick="exportSelected()" ${state.exportSelection.size ? "" : "disabled"}>导出 Excel</button><button class="primary" onclick="submitSelected()" ${state.exportSelection.size && state.helperReady ? "" : "disabled"}>提交到 SOLO-QA</button><button class="primary repair-submit" onclick="repairRemoteSelected()" ${repairableSelected && state.helperReady ? "" : "disabled"}>批量返修提交</button><button class="danger" onclick="hideSelected()" ${state.exportSelection.size ? "" : "disabled"}>从列表隐藏</button></div></div>
    ${filterBar("exports", [["q", "search", "Pair 唯一 ID、项目编号、题目或题面", filters.q], ["task_type", "select", "全部任务类型", filters.task_type, [["zero_to_one", "0–1"], ["feature", "Feature 迭代"], ["bugfix", "Bug 修复"]]], ["project_category", "select", "全部系统类型", filters.project_category, ["纯后端", "纯前端", "全栈"]], ["difficulty", "select", "全部难度", filters.difficulty, ["困难", "地狱"]], ["readiness", "select", "全部资料状态", filters.readiness, [["ready", "可提交"], ["blocked", "待补资料"]]], ["submission_status", "select", "全部提交状态", filters.submission_status, [["ready_to_submit", "待提交"], ["submitting", "提交中"], ["qc_pending", "质检中"], ["qc_passed", "质检通过"], ["needs_fix", "待返修"], ["discarded", "已废弃"], ["failed", "提交失败"], ["needs_review", "待重新确认"]]], ["date_from", "date", "完成日期从", filters.date_from], ["date_to", "date", "完成日期到", filters.date_to], ["include_hidden", "select", "未隐藏记录", filters.include_hidden, [["1", "包含已隐藏"]]]])}
    <div class="selection-row"><label><input type="checkbox" onchange="toggleExportPage(this.checked)"> 选择本页</label><button class="tiny" onclick="recheckSelected()" ${state.exportSelection.size ? "" : "disabled"}>复检所选</button><button class="tiny" onclick="repairSelected()" ${state.exportSelection.size ? "" : "disabled"}>应用复检建议</button><button class="tiny primary" onclick="repairRemoteSelected()" ${state.exportSelection.size && state.helperReady ? "" : "disabled"}>提交所选返修</button><button class="tiny" onclick="syncSelectedSoloQa()" ${state.exportSelection.size && state.helperReady ? "" : "disabled"}>同步所选状态</button><button class="tiny" onclick="restoreSelected()" ${state.exportSelection.size ? "" : "disabled"}>恢复所选</button></div>
    ${table(data.items, [["", (row) => `<input type="checkbox" aria-label="选择 ${esc(row.pair_id)}" ${state.exportSelection.has(row.pair_id) ? "checked" : ""} onchange="toggleExport('${row.pair_id}',this.checked)">`], ["项目 / Pair 唯一 ID", (row) => `<div class="title-cell"><strong>${esc(row.title)}</strong><small>项目编号：${esc(row.project_number)}</small>${pairIdentity(row.pair_id)}</div>`], ["类型 / 难度", (row) => `<div class="tag-stack">${taskTypeBadge(row.task_type)} ${projectCategoryBadge(row.project_category)} ${difficultyDisplay(row)}</div>`], ["A/B 资料", (row) => `<small class="block">A：${esc(row.a_session_id ? "Session ✓" : "缺 Session")} · ${esc(statusLabels[row.a_check_status] || row.a_check_status || "无验收")} · ${esc(statusLabels[row.a_recording_status] || row.a_recording_status || "无录像")}</small><small class="block">B：${esc(row.b_session_id ? "Session ✓" : "缺 Session")} · ${esc(statusLabels[row.b_check_status] || row.b_check_status || "无验收")} · ${esc(statusLabels[row.b_recording_status] || row.b_recording_status || "无录像")}</small>`], ["GSB / 复检", (row) => `${badge(row.verdict || "无结论")} ${recheckBadge(row)}`], ["资料明细", (row) => readinessDetail(row)], ["提交", (row) => `${badge(row.submission_status || "not_submitted")}${row.remote_id ? `<small class="block">平台 #${esc(row.remote_id)} · ${esc(statusLabels[row.remote_status] || row.remote_status || "待同步")}</small>` : ""}${row.qc_summary ? `<small class="block submission-summary">${esc(row.qc_summary)}</small>` : ""}${row.remote_updated_at ? `<small class="block">平台更新：${date(row.remote_updated_at)}</small>` : ""}${row.submission_error ? `<small class="block bad">${esc(row.submission_error)}</small>` : ""}`], ["完成时间", (row) => date(row.completed_at)], ["操作", (row) => `<div class="row-actions"><button class="tiny" onclick="showDelivery('${row.pair_id}')">展开</button>${row.submission_status === "needs_fix" ? ` <button class="tiny primary" onclick="repairRemotePair('${row.pair_id}')" ${state.helperReady && !state.soloQaRepairRunning.has(row.pair_id) ? "" : "disabled"}>${state.soloQaRepairRunning.has(row.pair_id) ? "返修中…" : "提交返修"}</button>` : ""}${row.remote_id ? ` <button class="tiny" onclick="syncSoloQa(['${row.pair_id}'])" ${state.helperReady && !state.soloQaSyncRunning.has(row.pair_id) ? "" : "disabled"}>${state.soloQaSyncRunning.has(row.pair_id) ? "同步中…" : "同步状态"}</button>` : ""}${row.a_recording_id ? ` <button class="tiny" onclick="playRecording('${row.a_recording_id}','A','${esc(row.title)}','${row.pair_id}')">播放 A</button>` : ""}${row.b_recording_id ? ` <button class="tiny" onclick="playRecording('${row.b_recording_id}','B','${esc(row.title)}','${row.pair_id}')">播放 B</button>` : ""}</div>`]])}${pager("exports", data)}</div>`;
}

function filterBar(name, fields) {
  return `<form class="filters" onsubmit="applyFilters(event,'${name}')">${fields.map(([key, type, placeholder, value, options]) => {
    if (type === "select") { const normalized = (options || []).map((option) => Array.isArray(option) ? option : [option, option]); return `<label><span>${esc(placeholder)}</span><select name="${key}"><option value="">${esc(placeholder)}</option>${normalized.map(([optionValue, label]) => `<option value="${esc(optionValue)}" ${String(value || "") === String(optionValue) ? "selected" : ""}>${esc(label)}</option>`).join("")}</select></label>`; }
    return `<label><span>${esc(placeholder)}</span><input name="${key}" type="${type}" value="${esc(value || "")}" placeholder="${esc(placeholder)}"></label>`;
  }).join("")}<div class="filter-actions"><button class="primary" type="submit">搜索</button><button class="secondary" type="button" onclick="clearFilters('${name}')">清除</button></div></form>`;
}
function applyFilters(event, name) { event.preventDefault(); const data = new FormData(event.currentTarget); state.filters[name] = Object.fromEntries([...data.entries()].filter(([, value]) => String(value).trim())); state.pages[name] = 1; render(); }
function clearFilters(name) { state.filters[name] = {}; state.pages[name] = 1; render(); }
function queryString(name) { return new URLSearchParams({ page: state.pages[name], size: state.sizes[name], ...state.filters[name] }).toString(); }
function distribution(rows) { if (!rows.length) return '<div class="empty">还没有数据</div>'; const max = Math.max(...rows.map((item) => item.count)); return `<div class="legend-list">${rows.map((item) => { const raw = item.project_category || item.task_type || item.verdict || item.status; const label = item.task_type ? (taskTypeLabels[item.task_type] || item.task_type) : item.status ? (statusLabels[item.status] || item.status) : raw; return `<div class="legend-row"><span>${esc(label)}</span><div class="legend-track"><div class="legend-fill ${item.project_category ? categoryTone(item.project_category) : item.task_type || ""}" style="width:${item.count / max * 100}%"></div></div><b>${item.count}</b></div>`; }).join("")}</div>`; }
function table(rows, columns) { if (!rows.length) return '<div class="empty">暂无记录</div>'; return `<div class="table-scroll"><table><thead><tr>${columns.map((column) => `<th>${column[0]}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td>${column[1](row)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`; }
function pager(name, data) { const pages = data.total_pages || Math.max(1, Math.ceil(data.total / (data.page_size || data.size))), size = data.page_size || data.size; return `<div class="pagination"><span>共 ${data.total} 条 · 第 ${data.page}/${pages} 页</span><label>每页 <select onchange="setPageSize('${name}',this.value)">${[20, 50, 100].map((value) => `<option ${Number(size) === value ? "selected" : ""}>${value}</option>`).join("")}</select></label><button class="tiny" ${data.page <= 1 ? "disabled" : ""} onclick="goPage('${name}',${data.page - 1})">上一页</button><button class="tiny" ${data.page >= pages ? "disabled" : ""} onclick="goPage('${name}',${data.page + 1})">下一页</button><label>跳到 <input class="page-jump" type="number" min="1" max="${pages}" value="${data.page}" onchange="goPage('${name}',this.value)"></label></div>`; }
function preflightCard(name, ok, detail) { return `<div class="preflight-item"><strong>${esc(name)}</strong><div class="${ok ? "ok" : "bad"}">${ok ? "● 可用" : "● 需要配置"}</div><small>${esc(detail || "—")}</small></div>`; }
function setting(key, label, value, help, attributes = "") { return `<div class="setting"><label for="set-${key}">${esc(label)}</label><input id="set-${key}" data-setting="${key}" value="${esc(value)}" ${attributes}><small>${esc(help)}</small></div>`; }

async function importTasks() { try { const data = await api("/api/tasks/import-historical", { method: "POST", body: JSON.stringify({ limit: 1000 }) }); notify(`导入 ${data.imported} 条，跳过 ${data.skipped} 条`); render(); } catch (error) { notify(error.message, true); } }
async function generateTask() { try { const data = await api("/api/tasks/generate", { method: "POST", body: JSON.stringify({ count: 1, taskType: "zero_to_one" }) }); notify(`补题作业已启动：${data.operationId}`); poll(data.operationId); } catch (error) { notify(error.message, true); } }
async function toggleAutomation(enabled) { try { const data = await api(`/api/automation/${enabled ? "start" : "stop"}`, { method: "POST", body: "{}" }); notify(enabled ? `完整自动流程已启动，将持续保持 ${data.targetPairs} 个活动 Pair` : "已停止自动补位，正在执行的项目不会被强制中断"); render(); } catch (error) { notify(error.message, true); } }
async function validateTask(id) { try { const data = await api(`/api/tasks/${id}/validate`, { method: "POST", body: "{}" }); notify("难度、禁题、查重和基线复核已启动"); poll(data.operationId); } catch (error) { notify(error.message, true); } }
async function createPair(id) { try { const data = await api("/api/pairs", { method: "POST", body: JSON.stringify({ taskId: id }) }); notify(`已创建 ${data.id}`); state.page = "pairs"; render(); } catch (error) { notify(error.message, true); } }
async function showTask(id) { try { const data = await api("/api/tasks?page=1&size=100"), task = data.items.find((item) => item.id === id); showDialog(`<h2>${esc(task?.title || id)}</h2>${task?.locked_by ? pairIdentity(task.locked_by) : pairIdentity("")}<div class="kv"><b>任务类型</b><span>${taskTypeBadge(task?.task_type)}</span></div><div class="kv"><b>系统类型</b><span>${projectCategoryBadge(task?.project_category)}</span></div><div class="kv"><b>难度</b><span>${esc(task?.difficulty)}</span></div><div class="kv"><b>状态</b><span>${badge(task?.status)}</span></div><div class="kv"><b>题面</b><div class="wrap">${esc(task?.prompt)}</div></div><div class="kv"><b>未通过原因</b><span>${esc(task?.rejection_reason || "—")}</span></div>`); } catch (error) { notify(error.message, true); } }
async function showPair(id) { try { const data = await api(`/api/pairs/${id}`), repo = data.repository, review = data.difficulty_review; const difficulty = review ? `<h3>实际难度复评</h3><div class="detail-grid"><div class="detail-item"><label>出题难度</label><strong>${esc(review.original_difficulty)}</strong></div><div class="detail-item"><label>A / B 实际难度</label><strong>${esc(review.a_difficulty || "复评中")} / ${esc(review.b_difficulty || "复评中")}</strong></div><div class="detail-item"><label>最终实际难度</label><strong>${esc(review.assessed_difficulty || "复评中")} ${badge(review.status)}</strong></div><div class="detail-item"><label>复评依据</label><span>${esc(review.reason || review.error || "正在读取真实改动、轨迹和 Docker 验收证据")}</span></div></div>` : ""; showDialog(`<h2>${esc(data.task.title)}</h2>${pairIdentity(data.id)}<p>${projectCategoryBadge(data.task.project_category)} ${taskTypeBadge(data.task.task_type)} ${badge(data.status)} ${badge(data.stage)}</p><div class="detail-grid"><div class="detail-item"><label>项目编号</label><strong class="code">${esc(data.chain_id)}</strong></div><div class="detail-item"><label>共同基线</label><strong class="code">${esc(data.baseline_sha || "待创建")}</strong></div><div class="detail-item"><label>远端仓库</label><strong class="code">${esc(repo?.remote_url || "待创建")}</strong></div><div class="detail-item"><label>分支</label><strong>main / A / B</strong></div></div><h3>A/B 执行</h3>${table(data.arms, [["Arm", (row) => row.arm], ["SessionID", (row) => `<span class="code">${esc(row.session_id)}</span>`], ["PromptID", (row) => `<span class="code">${esc(row.prompt_id)}</span>`], ["状态", (row) => badge(row.status)], ["提交", (row) => `<span class="code">${esc(row.commit_sha)}</span>`]])}${difficulty}<div style="margin-top:18px">${pairButtons(data)}</div>`); } catch (error) { notify(error.message, true); } }
function pairButtons(data) { const id = data.id; if (data.stage === "repository") return `<button class="primary" onclick="pairAction('${id}','prepare')">创建 GitHub 仓库并准备 A/B</button>`; if (data.stage === "ready_to_start") return `<button class="primary" onclick="pairAction('${id}','start')">并行启动 A/B 开发</button>`; if (data.stage === "difficulty_review") return data.difficulty_review?.status === "failed" ? `<button class="primary" onclick="pairAction('${id}','difficulty/review')">重新复评实际难度</button>` : `${badge(data.difficulty_review?.status || "running")} <span>正在根据真实开发结果复评难度</span>`; if (data.stage === "recording") { const byArm = Object.fromEntries((data.recordings || []).map((item) => [item.arm, item])); return ["A", "B"].map((arm) => byArm[arm]?.status === "recording" ? `<button class="danger" onclick="recordAction('${id}','${arm}','stop')">停止 ${arm} 录像</button>` : byArm[arm]?.status === "passed" ? `<span class="badge passed">${arm} 录像默认审核通过</span>` : `<button class="primary" onclick="recordAction('${id}','${arm}','start')">录制 ${arm} 真实操作</button>`).join(" "); } if (data.stage === "gsb_ready") return `<button class="primary" onclick="pairAction('${id}','gsb')">生成并默认确认 GSB</button>`; if (data.status === "completed") return `<button class="primary" onclick="pairAction('${id}','bugs/discover')">Codex 找 Bug</button>`; return `<span class="badge ${esc(data.status)}">当前阶段：${esc(statusLabels[data.stage] || data.stage)}</span>`; }
async function recordAction(id, arm, action, manual = false) { try { if ($("#dialog")?.open) $("#dialog").close(); if (action === "stop") notify(`${arm} 已收到停止请求，正在保存 MP4`); await api(`/api/pairs/${id}/recordings/${arm}/${action}`, { method: "POST", body: JSON.stringify({ manual }) }); if (action === "start") notify(manual ? `${arm} 项目已启动；请在独立 Chrome 中人工操作真实功能，完成后回本页停止并保存` : `${arm} 正在启动 Docker 项目并自动演示真实功能`); if (state.page === "evidence") renderEvidence(); watchRecording(id, arm); } catch (error) { notify(error.message, true); } }
async function watchRecording(id, arm) { for (let attempt = 0; attempt < 360; attempt += 1) { await new Promise((resolve) => window.setTimeout(resolve, 2000)); try { const pair = await api(`/api/pairs/${id}`), latest = pair.recording_attempts?.find((item) => item.arm === arm); if (state.page === "evidence") await renderEvidence(); if (latest && !["starting", "recording", "stopping"].includes(latest.status)) { notify(latest.status === "passed" ? `${arm} 浏览器录像已通过并自动替换当前录像` : `${arm} 录像失败：${latest.error}`, latest.status !== "passed"); return; } } catch (error) { notify(error.message, true); return; } } }
async function pairAction(id, action) { try { $("#dialog").close(); const data = await api(`/api/pairs/${id}/${action}`, { method: "POST", body: "{}" }); notify(`操作已启动：${data.operationId}`); poll(data.operationId); } catch (error) { notify(error.message, true); } }
function playRecording(id, arm, title, pairId = "") { showDialog(`<h2>${esc(title)} · ${esc(arm)} 真实操作录像</h2>${pairId ? pairIdentity(pairId) : ""}<video class="recording-player" controls autoplay preload="metadata" src="/api/recordings/${encodeURIComponent(id)}/content"></video><p class="sub">支持播放、暂停、拖动进度和全屏。</p>`); }
async function showEvidence(pairId, arm) { try { const data = await api(`/api/pairs/${pairId}`), check = data.checks.find((item) => item.arm === arm) || {}, recording = data.recordings.find((item) => item.arm === arm) || {}, attempts = (data.recording_attempts || []).filter((item) => item.arm === arm), steps = parseJson(check.checks_json, []); showDialog(`<h2>${esc(data.task.title)} · ${arm}</h2>${pairIdentity(data.id)}<div class="detail-grid"><div class="detail-item"><label>最终提交</label><strong class="code">${esc(check.commit_sha || recording.commit_sha || "—")}</strong></div><div class="detail-item"><label>Docker 验收</label><strong>${badge(check.status || "missing")}</strong></div><div class="detail-item"><label>当前录像</label><strong>${recordingFormat(recording)} · ${recording.capture_mode === "browser" ? "浏览器视口 · " : ""}${recording.width || 0}×${recording.height || 0} · ${recording.duration_seconds || 0}s</strong></div><div class="detail-item"><label>提交匹配</label><strong>${recording.commit_match ? "匹配" : "未匹配"}</strong></div></div><h3>录像历史</h3>${table(attempts, [["时间", (row) => date(row.created_at)], ["方式", (row) => `${recordingFormat(row)} · ${row.interaction_mode === "manual" ? "人工操作" : "自动操作"} · ${row.capture_mode === "browser" ? "浏览器视口" : "历史屏幕录像"}`], ["状态", (row) => badge(row.status)], ["规格", (row) => `${row.width || 0}×${row.height || 0} · ${row.duration_seconds || 0}s`], ["说明", (row) => esc(row.error || row.entry_url || "—")]])}<h3>验收步骤</h3>${table(steps, [["检查", (row) => esc(row.name)], ["结果", (row) => row.passed ? '<span class="ok">通过</span>' : '<span class="bad">失败</span>'], ["详情", (row) => `<div class="wrap log-detail">${esc(row.detail)}</div>`]])}${recording.id && recording.status === "passed" ? `<button class="primary" onclick="playRecording('${recording.id}','${arm}','${esc(data.task.title)}','${data.id}')">播放当前录像</button>` : ""}`); } catch (error) { notify(error.message, true); } }

async function reviewGsb(id) {
  try {
    const data = await api(`/api/pairs/${id}`), gsb = data.gsb; if (!gsb) throw new Error("尚未生成 GSB 草稿");
    const latest = data.gsb_rechecks?.[0], arms = Object.fromEntries(data.arms.map((item) => [item.arm, item])), recs = Object.fromEntries(data.recordings.map((item) => [item.arm, item])), issues = parseJson(latest?.issues_json, []);
    const legacy = !gsb.a_reason && !gsb.b_reason ? (gsb.reason || "") : "";
    showDialog(`<h2>复核与人工确认</h2><p>${esc(data.task.title)}</p>${pairIdentity(data.id)}<div class="ab-review-grid"><div class="review-side"><b>A</b><small>提交 ${esc((arms.A?.commit_sha || "").slice(0, 12))}</small><small>Session ${esc(arms.A?.session_id || "缺失")}</small>${recs.A?.id ? `<button type="button" class="tiny" onclick="playRecording('${recs.A.id}','A','${esc(data.task.title)}','${data.id}')">播放 A</button>` : ""}</div><div class="review-side"><b>B</b><small>提交 ${esc((arms.B?.commit_sha || "").slice(0, 12))}</small><small>Session ${esc(arms.B?.session_id || "缺失")}</small>${recs.B?.id ? `<button type="button" class="tiny" onclick="playRecording('${recs.B.id}','B','${esc(data.task.title)}','${data.id}')">播放 B</button>` : ""}</div></div><div class="choice-row">${["A better", "Same", "B better"].map((value) => `<label class="choice"><input type="radio" name="verdict" value="${value}" ${gsb.verdict === value ? "checked" : ""}>${value}</label>`).join("")}</div><label class="review-field"><b>A 的评价</b><textarea class="review-input" id="gsb-a-reason" maxlength="300">${esc(gsb.a_reason || legacy)}</textarea></label><label class="review-field"><b>B 的评价</b><textarea class="review-input" id="gsb-b-reason" maxlength="300">${esc(gsb.b_reason || legacy)}</textarea></label><p class="sub">只保留 A、B 两段评价，选择依据自然融入两段；禁止反引号和 Markdown。</p>${latest ? `<div class="recheck-result ${esc(latest.result_status)}"><strong>最近复检：${latest.applied_at ? "已应用" : esc(statusLabels[latest.result_status] || latest.result_status)}</strong><small>${esc(latest.model)} · ${esc(latest.reasoning_effort)} · ${date(latest.created_at)}${latest.applied_at ? ` · ${date(latest.applied_at)} 应用` : ""}</small>${issues.map((issue) => `<p>${esc(issue)}</p>`).join("")}<div class="review-text"><p><b>A 建议：</b>${esc(latest.suggested_a_reason || "—")}</p><p><b>B 建议：</b>${esc(latest.suggested_b_reason || "—")}</p></div>${latest.applied_at || latest.result_status === "passed" ? "" : `<button type="button" class="secondary" onclick="applyRecheck('${id}','${latest.id}')">应用复检建议</button>`}</div>` : '<div class="recheck-result"><strong>尚未复检</strong><p>可使用高强度模型检查公开理由与证据是否一致。</p></div>'}<div class="review-actions"><button type="button" class="secondary" onclick="recheckGsb('${id}')">复检公开理由</button><input id="confirmed-by" value="${esc(gsb.confirmed_by || "刘昱")}" placeholder="确认人"><button type="button" class="primary" onclick="confirmGsb('${id}')">确认并完成</button></div>`);
  } catch (error) { notify(error.message, true); }
}
async function confirmGsb(id) { const verdict = $('input[name="verdict"]:checked')?.value, aReason = $("#gsb-a-reason").value, bReason = $("#gsb-b-reason").value, confirmedBy = $("#confirmed-by").value; try { await api(`/api/pairs/${id}/gsb/confirm`, { method: "POST", body: JSON.stringify({ verdict, aReason, bReason, confirmedBy }) }); $("#dialog").close(); notify("GSB 已人工确认，记录进入待正式提交状态"); render(); } catch (error) { notify(error.message, true); } }
async function recheckGsb(id) { try { if ($("#dialog")?.open) $("#dialog").close(); const data = await api(`/api/pairs/${id}/gsb/recheck`, { method: "POST", body: "{}" }); state.recheckRunning.add(id); if (state.page === "reviews") await renderReviews(); else if (state.page === "exports") await renderExports(); notify("GSB 高强度复检已启动"); monitorReviewBatch([data.operationId], [id], state.page, () => reviewGsb(id)); } catch (error) { state.recheckRunning.delete(id); notify(error.message, true); } }
async function applyRecheck(pairId, recheckId) { try { await api(`/api/pairs/${pairId}/gsb/recheck/${recheckId}/apply`, { method: "POST", body: "{}" }); if (state.page === "reviews") await renderReviews(); else if (state.page === "exports") await renderExports(); notify("复检建议已应用并按授权默认确认"); await reviewGsb(pairId); } catch (error) { notify(error.message, true); } }

function toggleReview(pairId, checked) { checked ? state.reviewSelection.add(pairId) : state.reviewSelection.delete(pairId); renderReviews(); }
function toggleReviewPage(checked) { state.reviewItems.forEach((item) => checked ? state.reviewSelection.add(item.pair_id) : state.reviewSelection.delete(item.pair_id)); renderReviews(); }
async function recheckReviewsSelected() {
  const pairIds = [...state.reviewSelection];
  if (!pairIds.length) return;
  try {
    const data = await api("/api/gsb-reviews/recheck", { method: "POST", body: JSON.stringify({ pairIds }) });
    pairIds.forEach((id) => state.recheckRunning.add(id));
    notify(`已启动 ${data.count} 条 GSB 批量复检，完成后会自动刷新`);
    state.reviewSelection.clear();
    await renderReviews();
    monitorReviewBatch(data.operationIds || [], pairIds, "reviews");
  } catch (error) { notify(error.message, true); }
}
async function applyReviewSuggestionsSelected() {
  const pairIds = [...state.reviewSelection];
  if (!pairIds.length) return;
  try {
    const data = await api("/api/gsb-reviews/recheck/apply", { method: "POST", body: JSON.stringify({ pairIds }) });
    state.reviewSelection.clear();
    await renderReviews();
    notify(data.failed ? `已应用 ${data.applied} 条，${data.failed} 条失败，${data.skipped} 条跳过` : `已批量应用 ${data.applied} 条复检建议，${data.skipped} 条无需处理`, Boolean(data.failed));
  } catch (error) { notify(error.message, true); }
}
async function monitorReviewBatch(operationIds, pairIds = [], refreshPage = "reviews", onCompleted = null) {
  let previous = "";
  for (let attempt = 0; attempt < 900 && operationIds.length; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    const results = await Promise.all(operationIds.map((id) => api(`/api/operations/${id}`)));
    const signature = results.map((item) => item.status).join("|");
    let changed = signature !== previous;
    previous = signature;
    results.forEach((item, index) => {
      if (["completed", "failed"].includes(item.status) && pairIds[index] && state.recheckRunning.delete(pairIds[index])) changed = true;
    });
    if (changed && state.page === refreshPage) {
      if (refreshPage === "exports") await renderExports();
      else if (refreshPage === "reviews") await renderReviews();
    }
    if (results.every((item) => ["completed", "failed"].includes(item.status))) {
      const failed = results.filter((item) => item.status === "failed").length;
      notify(failed ? `复检完成，${failed} 条失败` : results.length === 1 ? "GSB 复检完成，页面已更新" : `批量复检完成，共 ${results.length} 条`, Boolean(failed));
      if (onCompleted) await onCompleted(results);
      return;
    }
  }
}

function toggleExport(pairId, checked) { checked ? state.exportSelection.add(pairId) : state.exportSelection.delete(pairId); renderExports(); }
function toggleExportPage(checked) { state.exportItems.forEach((item) => checked ? state.exportSelection.add(item.pair_id) : state.exportSelection.delete(item.pair_id)); renderExports(); }
function selectReady() { state.exportSelection.clear(); state.exportItems.filter((item) => item.readiness === "ready" && ["ready_to_submit", "failed", "not_submitted", ""].includes(item.submission_status || "")).forEach((item) => state.exportSelection.add(item.pair_id)); renderExports(); }
function selectNeedsFix() { state.exportSelection.clear(); state.exportItems.filter((item) => item.submission_status === "needs_fix").forEach((item) => state.exportSelection.add(item.pair_id)); renderExports(); }
async function preflightSelected() { try { const data = await api("/api/deliveries/preflight", { method: "POST", body: JSON.stringify({ pairIds: [...state.exportSelection] }) }); showDialog(`<h2>提交前检查</h2>${data.results.map((item) => `<section class="preflight-result">${pairIdentity(item.pair_id)}<strong>${item.eligible ? '<span class="ok">通过</span>' : '<span class="bad">不通过</span>'}</strong>${item.blockers.map((issue) => `<p class="bad">${esc(issue)}</p>`).join("")}${item.warnings.map((issue) => `<p class="warn">${esc(issue)}</p>`).join("")}</section>`).join("")}`); } catch (error) { notify(error.message, true); } }
async function exportSelected() { try { const response = await fetch("/api/deliveries/export.xlsx", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ pairIds: [...state.exportSelection] }) }); if (!response.ok) { const body = await response.json().catch(() => ({})); throw new Error(body.error || `HTTP ${response.status}`); } const blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement("a"); link.href = url; const disposition = response.headers.get("Content-Disposition") || ""; link.download = disposition.match(/filename="([^"]+)"/)?.[1] || "ab-gsb-completed-pairs.xlsx"; document.body.appendChild(link); link.click(); link.remove(); window.setTimeout(() => URL.revokeObjectURL(url), 1000); notify(`已导出 ${state.exportSelection.size} 个完成 Pair 的 Excel 复核副本`); } catch (error) { notify(error.message, true); } }
function showHelperResults(title, data) { const rows = (data.results || []).map((item) => `<section class="preflight-result">${pairIdentity(item.pair_id)}<strong>${esc(item.outcome)}</strong>${item.remote_id ? `<p class="code">远端 #${esc(item.remote_id)}</p>` : ""}${item.reason ? `<p>${esc(item.reason)}</p>` : ""}${item.error ? `<p class="bad">${esc(item.error)}</p>` : ""}</section>`).join(""); showDialog(`<h2>${esc(title)}</h2>${rows || '<p class="sub">没有需要处理的记录。</p>'}`); }
async function submitSelected() { const ids = [...state.exportSelection]; if (!window.confirm(`将向 SOLO-QA 正式提交 ${ids.length} 个 Pair。每条会上传 A/B 两份轨迹和两份录像，并立即创建本期 GSB 记录，是否继续？`)) return; try { notify("提交助手正在读取本期表单并上传文件…"); const data = await helperCall("PAIRWISE_GSB_SUBMIT", { pair_ids: ids }); showHelperResults("SOLO-QA 提交结果", data); notify(data.failed ? `${data.failed} 条提交失败，请查看结果` : `${ids.length} 条已提交或恢复远端记录`, Boolean(data.failed)); state.exportSelection.clear(); renderExports(); } catch (error) { notify(error.message, true); } }
async function repairRemote(pairIds, clearSelection = false) {
  const ids = [...new Set((pairIds || []).filter(Boolean))];
  if (!ids.length) return;
  const target = ids.length === 1 ? `平台记录 ${ids[0]}` : `所选 ${ids.length} 个平台记录`;
  if (!window.confirm(`将上传当前本地题面、双轨迹、双录像和 GSB，提交返修 ${target}，是否继续？`)) return;
  ids.forEach((id) => state.soloQaRepairRunning.add(id));
  await renderExports();
  try {
    notify(ids.length === 1 ? "正在提交这条返修…" : `正在提交 ${ids.length} 条返修…`);
    const data = await helperCall("PAIRWISE_GSB_REPAIR", { pair_ids: ids });
    showHelperResults("SOLO-QA 返修结果", data);
    notify(data.failed ? `${data.failed} 条返修失败` : "返修已提交，等待重新质检", Boolean(data.failed));
    if (clearSelection) state.exportSelection.clear();
  } catch (error) {
    notify(error.message, true);
  } finally {
    ids.forEach((id) => state.soloQaRepairRunning.delete(id));
    await renderExports();
  }
}
function repairRemotePair(pairId) { return repairRemote([pairId]); }
function repairRemoteSelected() {
  const ids = state.exportItems.filter((item) => state.exportSelection.has(item.pair_id) && item.submission_status === "needs_fix").map((item) => item.pair_id);
  if (!ids.length) return notify("请先选择待返修记录", true);
  return repairRemote(ids, true);
}
async function syncSoloQa(pairIds = []) {
  const ids = Array.isArray(pairIds) ? [...new Set(pairIds.filter(Boolean))] : [];
  ids.forEach((id) => state.soloQaSyncRunning.add(id));
  if (ids.length) await renderExports();
  try {
    notify(ids.length ? `正在同步所选 ${ids.length} 条 SOLO-QA 状态…` : "正在同步全部 SOLO-QA 状态…");
    const data = await helperCall("PAIRWISE_GSB_SYNC", ids.length ? { pair_ids: ids } : {});
    const total = data.results?.length || 0;
    notify(data.failed ? `同步完成：${total - data.failed} 条成功，${data.failed} 条失败` : `已同步 ${total} 条记录`, Boolean(data.failed));
  } catch (error) {
    notify(error.message, true);
  } finally {
    ids.forEach((id) => state.soloQaSyncRunning.delete(id));
    await renderExports();
  }
}
function syncSelectedSoloQa() { return syncSoloQa([...state.exportSelection]); }
async function hideSelected() { if (!window.confirm("从导出列表隐藏所选记录？项目、代码、轨迹、录像和审计记录都会保留。")) return; try { await api("/api/deliveries/hide", { method: "POST", body: JSON.stringify({ pairIds: [...state.exportSelection] }) }); notify("所选记录已从导出列表隐藏"); state.exportSelection.clear(); render(); } catch (error) { notify(error.message, true); } }
async function restoreSelected() { try { await api("/api/deliveries/restore", { method: "POST", body: JSON.stringify({ pairIds: [...state.exportSelection] }) }); notify("所选记录已恢复"); state.exportSelection.clear(); render(); } catch (error) { notify(error.message, true); } }
async function recheckSelected() { const ids = [...state.exportSelection]; if (!ids.length) return; try { const data = await api("/api/gsb-reviews/recheck", { method: "POST", body: JSON.stringify({ pairIds: ids }) }); ids.forEach((id) => state.recheckRunning.add(id)); state.exportSelection.clear(); await renderExports(); notify(`已启动 ${data.count} 个 GSB 复检作业，状态将自动更新`); monitorReviewBatch(data.operationIds || [], ids, "exports"); } catch (error) { ids.forEach((id) => state.recheckRunning.delete(id)); notify(error.message, true); } }
async function repairSelected() { try { const data = await api("/api/gsb-reviews/recheck/apply", { method: "POST", body: JSON.stringify({ pairIds: [...state.exportSelection] }) }); state.exportSelection.clear(); await renderExports(); notify(data.failed ? `已应用 ${data.applied} 条，${data.failed} 条失败` : data.applied ? `已对 ${data.applied} 条记录应用复检建议并按授权默认确认` : "所选记录没有可应用的复检建议", Boolean(data.failed)); } catch (error) { notify(error.message, true); } }
async function showDelivery(id) {
  try {
    const data = await api(`/api/pairs/${id}`);
    const repository = data.repository || {};
    const arms = Object.fromEntries(data.arms.map((item) => [item.arm, item]));
    const checks = Object.fromEntries(data.checks.map((item) => [item.arm, item]));
    const recordings = Object.fromEntries(data.recordings.map((item) => [item.arm, item]));
    const exportRow = state.exportItems.find((item) => item.pair_id === id);
    const commitLink = (arm) => repository.remote_url && arms[arm]?.commit_sha
      ? `${repository.remote_url.replace(/\.git$/, "")}/commit/${arms[arm].commit_sha}` : "";
    const completeness = exportRow
      ? `<h3>资料完整性</h3>${readinessDetail(exportRow, true)}` : "";
    const difficulty = data.difficulty_review ? `<h3>实际难度复评</h3><p>${esc(data.difficulty_review.original_difficulty)} → <b>${esc(data.difficulty_review.assessed_difficulty || "复评中")}</b> ${badge(data.difficulty_review.status)}</p><p>${esc(data.difficulty_review.reason || data.difficulty_review.error || "—")}</p>` : "";
    showDialog(`<h2>${esc(data.task.title)}</h2><p>${taskTypeBadge(data.task.task_type)} ${projectCategoryBadge(data.task.project_category)} ${badge(data.task.difficulty)} ${badge(data.delivery?.status || "not_submitted")}</p>${completeness}${pairIdentity(data.id)}<div class="kv"><b>项目编号</b><span class="code">${esc(data.chain_id)}</span></div><div class="kv"><b>语言/框架</b><span>${esc(data.task.stack || "未记录")}</span></div><div class="kv"><b>题面</b><div class="wrap">${esc(data.task.prompt)}</div></div>${difficulty}${["A", "B"].map((arm) => `<h3>${arm} 交付资料</h3><div class="detail-grid"><div class="detail-item"><label>SessionID</label><strong class="code">${esc(arms[arm]?.session_id || "缺失")}</strong></div><div class="detail-item"><label>PromptID</label><strong class="code">${esc(arms[arm]?.prompt_id || "缺失")}</strong></div><div class="detail-item"><label>提交</label><strong class="code">${esc(arms[arm]?.commit_sha || "缺失")}</strong>${commitLink(arm) ? `<a href="${esc(commitLink(arm))}" target="_blank" rel="noopener">永久链接</a>` : ""}</div><div class="detail-item"><label>Docker 验收</label><strong>${badge(checks[arm]?.status || "missing")}</strong>${checks[arm]?.error ? `<small class="bad block">${esc(checks[arm].error)}</small>` : ""}</div><div class="detail-item"><label>录像</label><strong>${recordings[arm] ? `${badge(recordings[arm].status)} · ${recordings[arm].width}×${recordings[arm].height} · ${recordings[arm].duration_seconds}s` : "缺失"}</strong>${recordings[arm]?.id ? `<button class="tiny" onclick="playRecording('${recordings[arm].id}','${arm}','${esc(data.task.title)}','${data.id}')">播放</button>` : ""}</div></div>`).join("")}<h3>GSB</h3><p><b>${esc(data.gsb?.verdict || "无结论")}</b> ${badge(data.gsb?.status || "missing")}</p><div class="review-text"><p><b>A：</b>${esc(data.gsb?.a_reason || "未填写")}</p><p><b>B：</b>${esc(data.gsb?.b_reason || "未填写")}</p></div><p class="sub">审核人：${esc(data.gsb?.confirmed_by || "未确认")} · ${date(data.gsb?.confirmed_at)}</p><button class="secondary" onclick="reviewGsb('${id}')">打开复核与人工确认</button>`);
  } catch (error) {
    notify(error.message, true);
  }
}

async function bugAction(id, action) { try { const data = await api(`/api/bug-candidates/${id}/${action}`, { method: "POST", body: "{}" }); if (data.operationId) { notify(`双次复现已启动：${data.operationId}`); poll(data.operationId); } else { notify(`已创建 Bug 任务：${data.id}`); render(); } } catch (error) { notify(error.message, true); } }
async function saveSettings() { const body = {}; $$('[data-setting]').forEach((input) => { body[input.dataset.setting] = ["task_generation_max_parallel", "task_pool_min_ready", "task_pool_target_ready", "max_pairs_parallel", "ab_prompt_stagger_seconds"].includes(input.dataset.setting) ? Number(input.value) : input.value; }); try { await api("/api/settings", { method: "POST", body: JSON.stringify(body) }); notify("设置已保存，新作业将使用这些值"); render(); } catch (error) { notify(error.message, true); } }
async function poll(id, onCompleted = null) { for (let attempt = 0; attempt < 900; attempt += 1) { await new Promise((resolve) => window.setTimeout(resolve, 2000)); const data = await api(`/api/operations/${id}`); if (data.status === "completed") { notify("后台操作已完成"); if (onCompleted) return onCompleted(data.result); return render(); } if (data.status === "failed") return notify(data.error, true); } notify("操作仍在后台运行，可在相应页面继续查看"); }
function parseJson(value, fallback) { try { return JSON.parse(value || ""); } catch { return fallback; } }
function goPage(name, page) { state.pages[name] = Math.max(1, Number(page) || 1); render(); }
function setPageSize(name, size) { state.sizes[name] = Number(size); state.pages[name] = 1; render(); }
function showDialog(html) { $("#dialog-content").innerHTML = html; $("#dialog").showModal(); }
function syncUrl() { const params = new URLSearchParams(); if (["reviews", "evidence", "exports"].includes(state.page)) { Object.entries(state.filters[state.page] || {}).forEach(([key, value]) => { if (value) params.set(key, value); }); params.set("page", state.pages[state.page]); params.set("size", state.sizes[state.page]); } const target = `#${state.page}${params.toString() ? `?${params}` : ""}`; if (location.hash !== target) history.replaceState(null, "", target); }
function loadUrl() { const raw = location.hash.replace(/^#/, "") || "dashboard", [page, query = ""] = raw.split("?"); if (titles[page]) state.page = page; if (["reviews", "evidence", "exports"].includes(state.page)) { const params = new URLSearchParams(query), filters = {}; params.forEach((value, key) => { if (key === "page") state.pages[state.page] = Math.max(1, Number(value) || 1); else if (key === "size") state.sizes[state.page] = [20, 50, 100].includes(Number(value)) ? Number(value) : 20; else filters[key] = value; }); state.filters[state.page] = filters; } }

loadUrl();
$$("#nav button").forEach((button) => { button.onclick = () => { state.page = button.dataset.page; render(); }; });
$("#refresh").onclick = render;
$("#locate-pair").onclick = locatePair;
$("#pair-locator").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); locatePair(); } });
window.addEventListener("hashchange", () => { loadUrl(); render(); });
window.setInterval(() => $("#clock").textContent = new Date().toLocaleString("zh-CN"), 1000);
render();
api("/api/settings").then((settings) => $("#runtime-summary").textContent = `Codex ${settings.codex_model} · Claude ${settings.claude_model}`).catch(() => {});
window.postMessage({ source: "pairwise-gsb-console", type: "PAIRWISE_GSB_BRIDGE_PING", requestId: "", payload: {} }, window.location.origin);
