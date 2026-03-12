const form = document.getElementById("run-form");
const tickerInput = document.getElementById("ticker");
const marketInput = document.getElementById("market");
const dateInput = document.getElementById("trade_date");
const researchUrlsInput = document.getElementById("research-urls");
const prepareBtn = document.getElementById("prepare-btn");
const confirmEvidenceBtn = document.getElementById("confirm-evidence-btn");
const runBtn = document.getElementById("run-btn");
const probeBtn = document.getElementById("probe-btn");
const cancelBtn = document.getElementById("cancel-btn");
const uploadEvidenceBtn = document.getElementById("upload-evidence-btn");
const addEvidenceUrlsBtn = document.getElementById("add-evidence-urls-btn");
const evidenceFileInput = document.getElementById("evidence-file");
const evidenceExtraUrlsInput = document.getElementById("evidence-extra-urls");
const evidenceStateEl = document.getElementById("evidence-state");
const evidenceSummaryEl = document.getElementById("evidence-summary");
const evidenceListEl = document.getElementById("evidence-list");

const statusEl = document.getElementById("status");
const timelineEl = document.getElementById("timeline");
const decisionBox = document.getElementById("decision-box");
const finalReportEl = document.getElementById("final-report");
const finalDecisionMetaEl = document.getElementById("final-decision-meta");
const decisionTraceEl = document.getElementById("decision-trace");
const downloadLinkEl = document.getElementById("download-link");
const runtimeDataEl = document.getElementById("runtime-data");
const runtimeModelEl = document.getElementById("runtime-model");
const sourceProbeEl = document.getElementById("source-probe");

const today = new Date().toISOString().slice(0, 10);
dateInput.value = dateInput.value || today;

let eventSource = null;
let currentRunId = null;
let currentEvidencePack = null;
let evidencePackSignature = "";

const completedReports = new Set();
const moduleContent = new Map();

const reportCards = new Map(
  Array.from(document.querySelectorAll(".module-card")).map((card) => [
    card.dataset.field,
    card,
  ])
);

const tocItems = new Map(
  Array.from(document.querySelectorAll(".toc-item")).map((item) => [
    item.dataset.field,
    item,
  ])
);

const reportOrder = [
  "final_decision",
  "fundamentals_report",
  "market_report",
  "news_report",
  "bull_debate_report",
  "bear_debate_report",
  "investment_plan",
  "final_trade_decision",
  "sentiment_report",
  "trader_investment_plan",
];

const liveUpdateFields = new Set(["bull_debate_report", "bear_debate_report"]);

const decisionMap = {
  BUY: "买入",
  SELL: "卖出",
  HOLD: "观望",
  UNKNOWN: "未知",
};

function timestamp() {
  return new Date().toLocaleTimeString();
}

function addTimeline(message) {
  const li = document.createElement("li");
  li.textContent = `[${timestamp()}] ${message}`;
  timelineEl.prepend(li);
}

function setStatus(text) {
  statusEl.textContent = text;
}

function parseUrls(text) {
  const seen = new Set();
  return (text || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => (line.startsWith("http://") || line.startsWith("https://")) && !seen.has(line) && seen.add(line));
}

function currentRequestSignature() {
  const ticker = tickerInput.value.trim().toUpperCase();
  const market = (marketInput?.value || "AUTO").toUpperCase();
  const tradeDate = dateInput.value;
  return `${ticker}|${market}|${tradeDate}`;
}

function setControlStates() {
  const hasPack = Boolean(currentEvidencePack);
  const confirmed = Boolean(currentEvidencePack?.confirmed);
  const running = Boolean(currentRunId);

  if (prepareBtn) prepareBtn.disabled = running;
  if (confirmEvidenceBtn) confirmEvidenceBtn.disabled = running || !hasPack || confirmed;
  if (runBtn) runBtn.disabled = running || !hasPack || !confirmed;
  if (uploadEvidenceBtn) uploadEvidenceBtn.disabled = running || !hasPack;
  if (addEvidenceUrlsBtn) addEvidenceUrlsBtn.disabled = running || !hasPack;
  if (cancelBtn) cancelBtn.disabled = !running;
}

function setModuleState(field, stateText, isDone = false) {
  const card = reportCards.get(field);
  const toc = tocItems.get(field);
  if (card) {
    const badge = card.querySelector(".badge");
    badge.textContent = stateText;
    badge.classList.toggle("done", isDone);
  }
  if (toc) {
    const status = toc.querySelector("em");
    status.textContent = stateText;
    toc.classList.toggle("done", isDone);
  }
}

function markActiveSection(sectionId) {
  document.querySelectorAll(".toc-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.target === sectionId);
  });
}

function setupTOCNavigation() {
  document.querySelectorAll(".toc-item").forEach((item) => {
    item.addEventListener("click", (event) => {
      event.preventDefault();
      const target = document.getElementById(item.dataset.target);
      if (target) {
        target.scrollIntoView({ behavior: "smooth", block: "start" });
        markActiveSection(item.dataset.target);
      }
    });
  });

  const sections = Array.from(document.querySelectorAll(".module-card"));
  const observer = new IntersectionObserver(
    (entries) => {
      const visible = entries
        .filter((e) => e.isIntersecting)
        .sort((a, b) => b.intersectionRatio - a.intersectionRatio);
      if (visible.length > 0) {
        markActiveSection(visible[0].target.id);
      }
    },
    {
      rootMargin: "-35% 0px -55% 0px",
      threshold: [0.1, 0.3, 0.6],
    }
  );

  sections.forEach((section) => observer.observe(section));
}

function resetUI() {
  timelineEl.innerHTML = "";
  completedReports.clear();

  reportOrder.forEach((field) => {
    const card = reportCards.get(field);
    if (!card) return;

    setModuleState(field, "等待", false);

    if (field === "final_decision") {
      decisionBox.textContent = "等待运行...";
      decisionBox.className = "decision-box waiting";
      finalDecisionMetaEl.textContent = "该模块正在等待生成...";
      decisionTraceEl.textContent = "决策溯源将在任务完成后显示...";
    } else {
      const pre = card.querySelector(".full-report");
      if (pre) pre.textContent = "该模块正在等待生成...";
    }
    moduleContent.set(field, "");
  });

  if (finalReportEl) {
    finalReportEl.textContent = "该模块正在等待生成...";
  }
  downloadLinkEl.style.display = "none";
  downloadLinkEl.removeAttribute("href");
  runtimeDataEl.textContent = "等待任务启动...";
  runtimeModelEl.textContent = "等待任务启动...";
}

function resetEvidenceUI() {
  currentEvidencePack = null;
  evidencePackSignature = "";
  if (evidenceStateEl) evidenceStateEl.textContent = "尚未准备证据包。";
  if (evidenceSummaryEl) evidenceSummaryEl.textContent = "准备完成后会显示来源分层、事件类型统计与可选条目。";
  if (evidenceListEl) evidenceListEl.innerHTML = "";
  setControlStates();
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `请求失败: ${response.status}`);
  }
  return response.json();
}

async function createRun(payload) {
  return postJson("/api/runs", payload);
}

async function probeSources(payload) {
  return postJson("/api/data-sources/probe", payload);
}

async function prepareEvidence(payload) {
  return postJson("/api/evidence/prepare", payload);
}

async function confirmEvidence(packId) {
  return postJson(`/api/evidence/${packId}/confirm`, {});
}

async function addEvidenceUrls(packId, urls) {
  return postJson(`/api/evidence/${packId}/urls`, { urls });
}

async function updateEvidenceSelection(packId, selectedIds) {
  return postJson(`/api/evidence/${packId}/selection`, { selected_cite_ids: selectedIds });
}

async function uploadEvidenceFile(packId, file) {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`/api/evidence/${packId}/files`, {
    method: "POST",
    body: formData,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `上传失败: ${response.status}`);
  }
  return response.json();
}

async function fetchEvidence(packId) {
  const response = await fetch(`/api/evidence/${packId}`);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `证据包获取失败: ${response.status}`);
  }
  return response.json();
}

async function cancelRun(runId) {
  return postJson(`/api/runs/${runId}/cancel`, {});
}

function collectSelectedEvidenceIds() {
  return Array.from(document.querySelectorAll(".evidence-select:checked")).map((el) => el.dataset.citeId);
}

function updateEvidenceSummaryView(pack) {
  const summary = pack?.summary || {};
  const byLayer = summary.by_layer || {};
  const selectedByLayer = summary.selected_by_layer || {};
  const byEvent = summary.by_event_type || {};
  const bySource = summary.by_source || {};

  const lines = [
    `Pack ID: ${pack?.pack_id || "-"}`,
    `状态: ${pack?.confirmed ? "已确认" : "未确认"}`,
    `Ticker: ${pack?.ticker || "-"}`,
    `Trade Date: ${pack?.trade_date || "-"}`,
    `资料总数: ${summary.total_items ?? 0}`,
    `已勾选: ${summary.selected_items ?? 0}`,
    "",
    "分层统计 (全部):",
  ];

  Object.entries(byLayer).forEach(([k, v]) => lines.push(`- ${k}: ${v}`));
  lines.push("", "分层统计 (已勾选):");
  Object.entries(selectedByLayer).forEach(([k, v]) => lines.push(`- ${k}: ${v}`));
  lines.push("", "事件类型:");
  Object.entries(byEvent).forEach(([k, v]) => lines.push(`- ${k}: ${v}`));
  lines.push("", "来源:");
  Object.entries(bySource).forEach(([k, v]) => lines.push(`- ${k}: ${v}`));

  if (pack?.fetch_errors?.length) {
    lines.push("", "抓取告警:");
    pack.fetch_errors.forEach((e) => lines.push(`- ${e}`));
  }

  evidenceSummaryEl.textContent = lines.join("\n");
}

function renderEvidenceList(pack) {
  const items = pack?.items || [];
  evidenceListEl.innerHTML = "";

  if (!items.length) {
    evidenceListEl.textContent = "暂无可用资料。";
    return;
  }

  for (const item of items) {
    const wrapper = document.createElement("article");
    wrapper.className = "evidence-item";

    const head = document.createElement("div");
    head.className = "evidence-item-head";

    const title = document.createElement("div");
    title.className = "evidence-item-title";
    const citeId = item.cite_id || "E";
    title.textContent = `[${citeId}] ${item.title || "Untitled"}`;

    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "evidence-select";
    checkbox.dataset.citeId = citeId;
    checkbox.checked = item.selected !== false;

    checkbox.addEventListener("change", async () => {
      if (!currentEvidencePack) return;
      const selectedIds = collectSelectedEvidenceIds();
      try {
        await updateEvidenceSelection(currentEvidencePack.pack_id, selectedIds);
        const refreshed = await fetchEvidence(currentEvidencePack.pack_id);
        applyEvidencePack(refreshed, false);
        setStatus("证据勾选已更新（已解除确认，请重新确认资料）。");
      } catch (err) {
        addTimeline(`更新证据勾选失败: ${String(err)}`);
        setStatus("更新证据勾选失败。");
      }
    });

    head.appendChild(title);
    head.appendChild(checkbox);
    wrapper.appendChild(head);

    const meta = document.createElement("div");
    meta.className = "evidence-item-meta";
    meta.textContent = `layer=${item.layer || "unknown"} | source=${item.source || "unknown"} | event=${item.event_type || "其他"} | published=${item.published_at || "-"}`;
    wrapper.appendChild(meta);

    const summary = document.createElement("p");
    summary.className = "evidence-item-summary";
    summary.textContent = item.summary || "(无摘要)";
    wrapper.appendChild(summary);

    if (item.url) {
      const link = document.createElement("a");
      link.className = "evidence-item-url";
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = item.url;
      wrapper.appendChild(link);
    }

    evidenceListEl.appendChild(wrapper);
  }
}

function applyEvidencePack(pack, logTimeline = true) {
  currentEvidencePack = pack;
  evidencePackSignature = currentRequestSignature();
  const confirmedText = pack.confirmed ? "已确认，可启动分析" : "未确认，请先勾选并确认";
  evidenceStateEl.textContent = `证据包 ${pack.pack_id.slice(0, 8)} | ${confirmedText}`;
  updateEvidenceSummaryView(pack);
  renderEvidenceList(pack);
  if (logTimeline) {
    addTimeline(`证据包已更新: ${pack.pack_id.slice(0, 8)}，条目 ${pack.summary?.total_items ?? 0}，已选 ${pack.summary?.selected_items ?? 0}`);
  }
  setControlStates();
}

function invalidateEvidenceIfInputsChanged() {
  if (!currentEvidencePack) return;
  if (currentRunId) return;
  if (evidencePackSignature !== currentRequestSignature()) {
    resetEvidenceUI();
    setStatus("输入参数已变化，请重新点击“获取外部资料”。");
  }
}

function updateReportField(field, content, append = false) {
  if (!reportCards.has(field)) return;
  const isLiveUpdate = liveUpdateFields.has(field);
  if (!isLiveUpdate && completedReports.has(field)) return;

  const card = reportCards.get(field);
  const pre = card.querySelector(".full-report");
  if (pre) {
    if (isLiveUpdate && append) {
      const base = pre.textContent === "该模块正在等待生成..." ? "" : pre.textContent;
      const sep = base && !base.endsWith("\n") ? "\n\n" : "";
      pre.textContent = `${base}${sep}${content}`;
    } else {
      pre.textContent = content;
    }
  }
  const baseForStore = isLiveUpdate && append
    ? `${moduleContent.get(field) || ""}\n\n${content}`.trim()
    : content;
  moduleContent.set(field, baseForStore);

  if (isLiveUpdate) {
    setModuleState(field, "更新中", false);
  } else {
    setModuleState(field, "已完成", true);
    completedReports.add(field);
  }
  addTimeline(`${field} 已写入完整报告。`);
}

function connectEvents(runId) {
  if (eventSource) {
    eventSource.close();
  }

  eventSource = new EventSource(`/api/runs/${runId}/events`);

  eventSource.addEventListener("progress", (event) => {
    const data = JSON.parse(event.data);
    if (data.message) {
      addTimeline(data.message);
      setStatus(data.message);
    }
  });

  eventSource.addEventListener("runtime", (event) => {
    const data = JSON.parse(event.data);
    const vendors = data.data_vendors || {};
    runtimeDataEl.textContent = [
      `core_stock_apis: ${vendors.core_stock_apis || "-"}`,
      `technical_indicators: ${vendors.technical_indicators || "-"}`,
      `fundamental_data: ${vendors.fundamental_data || "-"}`,
      `news_data: ${vendors.news_data || "-"}`,
      `prepared_items: ${data.prepared_items_count ?? 0}`,
    ].join("\n");

    runtimeModelEl.textContent = [
      `provider: ${data.provider || "-"}`,
      `deep_model: ${data.deep_model || "-"}`,
      `quick_model: ${data.quick_model || "-"}`,
      `backend_url: ${data.backend_url || "-"}`,
      `research_urls: ${data.research_urls_count ?? 0}`,
    ].join("\n");
  });

  eventSource.addEventListener("report", (event) => {
    const data = JSON.parse(event.data);
    updateReportField(data.field, data.content, Boolean(data.append));
  });

  eventSource.addEventListener("final", (event) => {
    const data = JSON.parse(event.data);
    const decision = (data.decision || "UNKNOWN").toUpperCase();

    decisionBox.textContent = decisionMap[decision] || decisionMap.UNKNOWN;
    decisionBox.className = `decision-box ${decision}`;

    finalDecisionMetaEl.textContent =
      `决策日期: ${data.trade_date}\n市场: ${data.market || "AUTO"}\n标的: ${data.ticker}\n结论: ${decisionMap[decision] || "未知"}\n证据包: ${data.evidence_pack_id || "-"}`;

    const getSnippet = (field, title) => {
      const text = (moduleContent.get(field) || "").trim();
      if (!text) return `${title}: (无内容)`;
      const compact = text.replace(/\n+/g, " ").slice(0, 220);
      return `${title}: ${compact}${text.length > 220 ? "..." : ""}`;
    };
    decisionTraceEl.textContent = [
      getSnippet("investment_plan", "Research Team Decision"),
      "",
      getSnippet("trader_investment_plan", "Trader Plan"),
      "",
      getSnippet("final_trade_decision", "Risk Team Final Report"),
    ].join("\n");

    setModuleState("final_decision", "已完成", true);
    completedReports.add("final_decision");

    if (data.final_report) {
      updateReportField("final_trade_decision", data.final_report);
    }

    const stats = data.stats || {};
    addTimeline(
      `已完成。LLM 调用: ${stats.llm_calls ?? 0}，工具调用: ${stats.tool_calls ?? 0}。`
    );
    setStatus(data.cached ? "任务完成（命中缓存）。" : "任务完成。");
    if (data.download_path) {
      downloadLinkEl.href = data.download_path;
      downloadLinkEl.style.display = "inline-block";
    }
  });

  eventSource.addEventListener("cancelled", (event) => {
    const data = JSON.parse(event.data);
    decisionBox.textContent = "已取消";
    decisionBox.className = "decision-box HOLD";
    finalDecisionMetaEl.textContent = data.message || "任务已取消。";
    setModuleState("final_decision", "已取消", false);
    addTimeline("任务已取消。");
    setStatus("任务已取消。");
  });

  eventSource.addEventListener("error", (event) => {
    const data = JSON.parse(event.data);
    decisionBox.textContent = "错误";
    decisionBox.className = "decision-box SELL";
    finalDecisionMetaEl.textContent = `${data.message}\n\n${data.traceback || ""}`;
    decisionTraceEl.textContent = "任务失败，无法生成决策溯源。";
    setModuleState("final_decision", "失败", false);
    addTimeline(`错误: ${data.message}`);
    setStatus("任务失败。");
  });

  eventSource.addEventListener("done", () => {
    liveUpdateFields.forEach((field) => {
      const card = reportCards.get(field);
      if (!card) return;
      const text = card.querySelector(".full-report")?.textContent || "";
      if (text && text !== "该模块正在等待生成...") {
        setModuleState(field, "已完成", true);
      }
    });

    currentRunId = null;
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
    setControlStates();
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const ticker = tickerInput.value.trim().toUpperCase();
  const market = (marketInput?.value || "AUTO").toUpperCase();
  const tradeDate = dateInput.value;

  if (!ticker || !tradeDate) {
    setStatus("请填写股票代码和决策日期。");
    return;
  }

  if (!currentEvidencePack) {
    setStatus("请先点击“获取外部资料”。");
    return;
  }

  if (evidencePackSignature !== currentRequestSignature()) {
    setStatus("输入参数已变更，请重新准备证据包。");
    return;
  }

  if (!currentEvidencePack.confirmed) {
    setStatus("请先点击“确认资料”，再开始分析。");
    return;
  }

  resetUI();
  setStatus("正在启动任务...");

  try {
    const { run_id: runId, cached } = await createRun({
      ticker,
      market,
      trade_date: tradeDate,
      research_urls: parseUrls(researchUrlsInput?.value || ""),
      evidence_pack_id: currentEvidencePack.pack_id,
    });
    currentRunId = runId;
    setControlStates();
    addTimeline(`任务编号: ${runId}${cached ? "（缓存）" : ""}`);
    if (cached) {
      setStatus("命中缓存，正在快速回放结果...");
    }
    connectEvents(runId);
  } catch (err) {
    decisionBox.textContent = "错误";
    decisionBox.className = "decision-box SELL";
    finalDecisionMetaEl.textContent = String(err);
    setStatus("任务启动失败。");
    currentRunId = null;
    setControlStates();
  }
});

if (prepareBtn) {
  prepareBtn.addEventListener("click", async () => {
    const ticker = tickerInput.value.trim().toUpperCase();
    const market = (marketInput?.value || "AUTO").toUpperCase();
    const tradeDate = dateInput.value;
    const researchUrls = parseUrls(researchUrlsInput?.value || "");

    if (!ticker || !tradeDate) {
      setStatus("请先填写股票代码和决策日期。");
      return;
    }

    prepareBtn.disabled = true;
    setStatus("正在抓取外部资料并构建证据包...");

    try {
      const pack = await prepareEvidence({
        ticker,
        market,
        trade_date: tradeDate,
        research_urls: researchUrls,
      });
      applyEvidencePack(pack, true);
      setStatus("证据包已准备，请检查条目并确认资料。");
    } catch (err) {
      setStatus(`证据包准备失败: ${String(err)}`);
      addTimeline(`证据包准备失败: ${String(err)}`);
    } finally {
      if (!currentRunId) {
        prepareBtn.disabled = false;
      }
      setControlStates();
    }
  });
}

if (confirmEvidenceBtn) {
  confirmEvidenceBtn.addEventListener("click", async () => {
    if (!currentEvidencePack) {
      setStatus("请先准备证据包。");
      return;
    }
    confirmEvidenceBtn.disabled = true;
    setStatus("正在确认资料...");
    try {
      await confirmEvidence(currentEvidencePack.pack_id);
      const refreshed = await fetchEvidence(currentEvidencePack.pack_id);
      applyEvidencePack(refreshed, false);
      addTimeline("证据包已确认，可以启动分析。");
      setStatus("证据包已确认，可以开始分析。");
    } catch (err) {
      setStatus(`确认失败: ${String(err)}`);
      addTimeline(`证据包确认失败: ${String(err)}`);
    } finally {
      setControlStates();
    }
  });
}

if (addEvidenceUrlsBtn) {
  addEvidenceUrlsBtn.addEventListener("click", async () => {
    if (!currentEvidencePack) {
      setStatus("请先准备证据包。");
      return;
    }
    const urls = parseUrls(evidenceExtraUrlsInput?.value || "");
    if (!urls.length) {
      setStatus("请先输入要追加的 URL（每行一个）。");
      return;
    }

    addEvidenceUrlsBtn.disabled = true;
    setStatus("正在追加 URL 到证据包...");
    try {
      const res = await addEvidenceUrls(currentEvidencePack.pack_id, urls);
      const refreshed = await fetchEvidence(currentEvidencePack.pack_id);
      applyEvidencePack(refreshed, false);
      evidenceExtraUrlsInput.value = "";
      addTimeline(`已追加 URL ${res.added ?? 0} 条。`);
      setStatus("URL 已加入证据包（已解除确认，请重新确认）。");
    } catch (err) {
      setStatus(`URL 追加失败: ${String(err)}`);
      addTimeline(`URL 追加失败: ${String(err)}`);
    } finally {
      setControlStates();
    }
  });
}

if (uploadEvidenceBtn) {
  uploadEvidenceBtn.addEventListener("click", async () => {
    if (!currentEvidencePack) {
      setStatus("请先准备证据包。");
      return;
    }
    const files = Array.from(evidenceFileInput?.files || []);
    if (!files.length) {
      setStatus("请先选择要上传的文件。");
      return;
    }

    uploadEvidenceBtn.disabled = true;
    setStatus(`正在上传 ${files.length} 个文件...`);

    let okCount = 0;
    for (const file of files) {
      try {
        await uploadEvidenceFile(currentEvidencePack.pack_id, file);
        okCount += 1;
      } catch (err) {
        addTimeline(`文件上传失败 ${file.name}: ${String(err)}`);
      }
    }

    try {
      const refreshed = await fetchEvidence(currentEvidencePack.pack_id);
      applyEvidencePack(refreshed, false);
    } catch (err) {
      addTimeline(`刷新证据包失败: ${String(err)}`);
    }

    evidenceFileInput.value = "";
    setStatus(`文件上传完成：成功 ${okCount}/${files.length}（已解除确认，请重新确认）。`);
    addTimeline(`文件上传完成：成功 ${okCount}/${files.length}`);
    setControlStates();
  });
}

if (cancelBtn) {
  cancelBtn.addEventListener("click", async () => {
    if (!currentRunId) {
      return;
    }
    cancelBtn.disabled = true;
    setStatus("正在发送取消请求...");
    try {
      const result = await cancelRun(currentRunId);
      addTimeline(result.message || "已发送取消请求。");
    } catch (err) {
      addTimeline(String(err));
      setStatus("取消请求失败。");
      cancelBtn.disabled = false;
    }
  });
}

setupTOCNavigation();
resetEvidenceUI();
setControlStates();

if (probeBtn) {
  probeBtn.addEventListener("click", async () => {
    const ticker = tickerInput.value.trim().toUpperCase();
    const market = (marketInput?.value || "AUTO").toUpperCase();
    const tradeDate = dateInput.value;
    if (!ticker || !tradeDate) {
      setStatus("请先填写股票代码和决策日期。");
      return;
    }

    probeBtn.disabled = true;
    const oldText = probeBtn.textContent;
    probeBtn.textContent = "检测中...";
    addTimeline(`开始检测数据源: ${ticker} (${market})`);
    setStatus("正在检测数据源...");
    if (sourceProbeEl) {
      sourceProbeEl.textContent = "检测中，请稍候...";
    }

    try {
      const data = await probeSources({
        ticker,
        market,
        trade_date: tradeDate,
      });
      const lines = [
        `Ticker: ${data.ticker}`,
        `Market: ${data.market}`,
        `Range: ${data.start_date} -> ${data.end_date}`,
        "",
        "Price/OHLCV Sources:",
        "",
      ];
      for (const r of data.results || []) {
        lines.push(
          `[${r.ok ? "OK" : "FAIL"}] ${r.source} | rows=${r.rows} | latency=${r.latency_ms}ms | ${r.message}`
        );
      }
      lines.push("");
      lines.push(`News Coverage Window: ${data.news_start_date || "-"} -> ${data.end_date}`);
      lines.push("News Sources:");
      lines.push("");
      for (const r of data.news_results || []) {
        lines.push(
          `[${r.ok ? "OK" : "FAIL"}] ${r.source} | items=${r.items} | latency=${r.latency_ms}ms | ${r.message}`
        );
      }
      if (sourceProbeEl) {
        sourceProbeEl.textContent = lines.join("\n");
      }
      addTimeline("数据源检测完成。");
      setStatus("数据源检测完成。");
    } catch (err) {
      const msg = String(err);
      if (sourceProbeEl) {
        sourceProbeEl.textContent = msg;
      }
      addTimeline(`数据源检测失败: ${msg}`);
      setStatus("数据源检测失败。");
    } finally {
      probeBtn.disabled = false;
      probeBtn.textContent = oldText || "检测数据源";
    }
  });
}

[tickerInput, marketInput, dateInput].forEach((el) => {
  if (!el) return;
  el.addEventListener("change", invalidateEvidenceIfInputsChanged);
});
