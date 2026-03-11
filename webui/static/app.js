const form = document.getElementById("run-form");
const tickerInput = document.getElementById("ticker");
const marketInput = document.getElementById("market");
const dateInput = document.getElementById("trade_date");
const researchUrlsInput = document.getElementById("research-urls");
const runBtn = document.getElementById("run-btn");
const probeBtn = document.getElementById("probe-btn");
const cancelBtn = document.getElementById("cancel-btn");
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

async function createRun(payload) {
  const response = await fetch("/api/runs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`启动任务失败: ${text}`);
  }
  return response.json();
}

async function probeSources(payload) {
  const response = await fetch("/api/data-sources/probe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`检测失败: ${text}`);
  }
  return response.json();
}

async function cancelRun(runId) {
  const response = await fetch(`/api/runs/${runId}/cancel`, {
    method: "POST",
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`取消失败: ${text}`);
  }
  return response.json();
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
      `决策日期: ${data.trade_date}\n市场: ${data.market || "AUTO"}\n标的: ${data.ticker}\n结论: ${decisionMap[decision] || "未知"}`;

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
    cancelBtn.disabled = true;
  });

  eventSource.addEventListener("cancelled", (event) => {
    const data = JSON.parse(event.data);
    decisionBox.textContent = "已取消";
    decisionBox.className = "decision-box HOLD";
    finalDecisionMetaEl.textContent = data.message || "任务已取消。";
    setModuleState("final_decision", "已取消", false);
    addTimeline("任务已取消。");
    setStatus("任务已取消。");
    cancelBtn.disabled = true;
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
    cancelBtn.disabled = true;
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

    runBtn.disabled = false;
    cancelBtn.disabled = true;
    currentRunId = null;
    if (eventSource) {
      eventSource.close();
      eventSource = null;
    }
  });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();

  const ticker = tickerInput.value.trim().toUpperCase();
  const market = (marketInput?.value || "AUTO").toUpperCase();
  const tradeDate = dateInput.value;
  const researchUrls = (researchUrlsInput?.value || "")
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line.startsWith("http://") || line.startsWith("https://"));
  if (!ticker || !tradeDate) {
    setStatus("请填写股票代码和决策日期。");
    return;
  }

  runBtn.disabled = true;
  cancelBtn.disabled = false;
  resetUI();
  setStatus("正在启动任务...");

  try {
    const { run_id: runId, cached } = await createRun({
      ticker,
      market,
      trade_date: tradeDate,
      research_urls: researchUrls,
    });
    currentRunId = runId;
    addTimeline(`任务编号: ${runId}${cached ? "（缓存）" : ""}`);
    if (cached) {
      setStatus("命中缓存，正在快速回放结果...");
    }
    connectEvents(runId);
  } catch (err) {
    runBtn.disabled = false;
    cancelBtn.disabled = true;
    decisionBox.textContent = "错误";
    decisionBox.className = "decision-box SELL";
    finalDecisionMetaEl.textContent = String(err);
    setStatus("任务启动失败。");
  }
});

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

setupTOCNavigation();

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
      ];
      for (const r of data.results || []) {
        lines.push(
          `[${r.ok ? "OK" : "FAIL"}] ${r.source} | rows=${r.rows} | latency=${r.latency_ms}ms | ${r.message}`
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
