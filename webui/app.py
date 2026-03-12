from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
import pandas as pd
import requests
import uvicorn
from dotenv import load_dotenv

from cli.stats_handler import StatsCallbackHandler
from tradingagents.dataflows.alpha_vantage_stock import get_stock as get_alpha_vantage_stock
from tradingagents.dataflows.alpha_vantage_news import get_news as get_alpha_vantage_news
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.local_csv import get_stock_data_local
from tradingagents.dataflows.seeking_alpha_rss import get_news_seeking_alpha
from tradingagents.dataflows.stooq import get_stock_data_stooq
from tradingagents.dataflows.y_finance import get_YFin_data_online
from tradingagents.dataflows.yfinance_news import get_news_yfinance
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients import create_llm_client

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

# Ensure web service picks up API keys from repository-level .env
load_dotenv(BASE_DIR.parent / ".env")

REPORT_FIELDS = [
    ("fundamentals_report", "基本面"),
    ("market_report", "市场面"),
    ("news_report", "新闻面"),
    ("sentiment_report", "情绪面"),
    ("bull_debate_report", "Bull Debate Report"),
    ("bear_debate_report", "Bear Debate Report"),
    ("investment_plan", "研究团队决策"),
    ("trader_investment_plan", "交易员方案"),
    ("final_trade_decision", "风控团队最终报告"),
]


def _normalize_market_ticker(raw: str, market: str = "AUTO") -> str:
    # Keep alnum + dot only for consistent parsing, then normalize HK variants.
    cleaned = re.sub(r"[^A-Za-z0-9.]", "", raw).upper()
    if not cleaned:
        return ""

    market = (market or "AUTO").upper()

    if market == "HK":
        m_hk = re.fullmatch(r"(\d{1,5})(?:\.?)(HK|HKG|HKEX)?", cleaned)
        if m_hk:
            code = m_hk.group(1)
            code = f"{int(code):04d}" if len(code) <= 4 else code
            return f"{code}.HK"

    if market == "CN":
        m_cn = re.fullmatch(r"(\d{6})(?:\.?)(SH|SS|SZ)?", cleaned)
        if m_cn:
            code = m_cn.group(1)
            suffix = m_cn.group(2)
            if suffix in {"SH", "SS"}:
                return f"{code}.SS"
            if suffix == "SZ":
                return f"{code}.SZ"
            # Auto infer exchange by common A-share code prefixes.
            return f"{code}.SS" if code.startswith(("5", "6", "9")) else f"{code}.SZ"

    # 0700HK / 700HK / 0700.HK / 700.HK -> 0700.HK
    m = re.fullmatch(r"(\d{1,4})(?:\.?)(HK|HKG|HKEX)", cleaned)
    if m:
        return f"{int(m.group(1)):04d}.HK"

    # Pure HK code with leading zero already included: 0700
    # Only auto-convert 4-digit starting with 0 to avoid hijacking US tickers like AAPL.
    if re.fullmatch(r"0\d{3}", cleaned):
        return f"{cleaned}.HK"

    return cleaned


class RunRequest(BaseModel):
    ticker: str
    trade_date: str
    market: str = "AUTO"
    research_urls: list[str] = Field(default_factory=list)
    evidence_pack_id: str | None = None

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        val = value.strip()
        if not val:
            raise ValueError("股票代码不能为空。")
        if len(val) > 20:
            raise ValueError("股票代码过长。")
        return val

    @field_validator("market")
    @classmethod
    def normalize_market(cls, value: str) -> str:
        normalized = (value or "AUTO").strip().upper()
        if normalized not in {"AUTO", "US", "HK", "CN"}:
            raise ValueError("市场必须是 AUTO/US/HK/CN。")
        return normalized

    @model_validator(mode="after")
    def apply_market_ticker_normalization(self):
        normalized = _normalize_market_ticker(self.ticker, market=self.market)
        if not normalized:
            raise ValueError("股票代码不能为空。")
        if len(normalized) > 20:
            raise ValueError("股票代码过长。")
        self.ticker = normalized
        return self

    @field_validator("trade_date")
    @classmethod
    def validate_trade_date(cls, value: str) -> str:
        try:
            datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError("决策日期格式必须为 YYYY-MM-DD。") from exc
        return value

    @field_validator("research_urls")
    @classmethod
    def normalize_research_urls(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen = set()
        for raw in value or []:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out[:30]


class ProbeRequest(BaseModel):
    ticker: str
    trade_date: str
    market: str = "AUTO"

    @model_validator(mode="after")
    def normalize(self):
        ticker = (self.ticker or "").strip()
        if not ticker:
            raise ValueError("股票代码不能为空。")
        self.market = (self.market or "AUTO").strip().upper()
        if self.market not in {"AUTO", "US", "HK", "CN"}:
            raise ValueError("市场必须是 AUTO/US/HK/CN。")
        self.ticker = _normalize_market_ticker(ticker, market=self.market)
        datetime.strptime(self.trade_date, "%Y-%m-%d")
        return self


class EvidencePrepareRequest(BaseModel):
    ticker: str
    trade_date: str
    market: str = "AUTO"
    research_urls: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize(self):
        t = (self.ticker or "").strip()
        if not t:
            raise ValueError("股票代码不能为空。")
        self.market = (self.market or "AUTO").strip().upper()
        if self.market not in {"AUTO", "US", "HK", "CN"}:
            raise ValueError("市场必须是 AUTO/US/HK/CN。")
        self.ticker = _normalize_market_ticker(t, market=self.market)
        datetime.strptime(self.trade_date, "%Y-%m-%d")

        deduped: list[str] = []
        seen = set()
        for url in self.research_urls or []:
            if not isinstance(url, str):
                continue
            u = url.strip()
            if not u or not (u.startswith("http://") or u.startswith("https://")):
                continue
            if u in seen:
                continue
            seen.add(u)
            deduped.append(u)
        self.research_urls = deduped[:50]
        return self


class EvidenceUrlsRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)

    @field_validator("urls")
    @classmethod
    def normalize_urls(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen = set()
        for raw in value or []:
            if not isinstance(raw, str):
                continue
            url = raw.strip()
            if not url:
                continue
            if not (url.startswith("http://") or url.startswith("https://")):
                continue
            if url in seen:
                continue
            seen.add(url)
            out.append(url)
        return out[:50]


class EvidenceSelectionRequest(BaseModel):
    selected_cite_ids: list[str] = Field(default_factory=list)

    @field_validator("selected_cite_ids")
    @classmethod
    def normalize_ids(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        seen = set()
        for raw in value or []:
            if not isinstance(raw, str):
                continue
            cid = raw.strip()
            if not cid:
                continue
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
        return out


@dataclass
class RunState:
    run_id: str
    ticker: str
    trade_date: str
    market: str = "AUTO"
    research_urls: list[str] = field(default_factory=list)
    evidence_pack_id: str | None = None
    event_queue: queue.Queue[dict[str, Any]] = field(default_factory=queue.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancelled: bool = False
    artifact_path: str | None = None
    done: bool = False


app = FastAPI(title="TradingAgents Web UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_RUNS: dict[str, RunState] = {}
_RUNS_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_CACHE_LOCK = threading.Lock()
_EVIDENCE_PACKS: dict[str, dict[str, Any]] = {}
_EVIDENCE_LOCK = threading.Lock()


def _event(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _push(run: RunState, event_name: str, payload: dict[str, Any]) -> None:
    run.event_queue.put({"event": event_name, "payload": payload})


def _build_runtime_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()

    if not os.getenv("OPENAI_API_KEY") and os.getenv("MINIMAX_API_KEY"):
        os.environ["OPENAI_API_KEY"] = os.getenv("MINIMAX_API_KEY", "")

    default_model = os.getenv("TA_MODEL", config["quick_think_llm"])
    config["llm_provider"] = os.getenv("TA_LLM_PROVIDER", config["llm_provider"])
    config["backend_url"] = os.getenv(
        "MINIMAX_BASE_URL", os.getenv("TA_BACKEND_URL", config["backend_url"])
    )
    config["deep_think_llm"] = os.getenv("TA_DEEP_MODEL", default_model)
    config["quick_think_llm"] = os.getenv("TA_QUICK_MODEL", default_model)
    config["max_debate_rounds"] = int(os.getenv("TA_MAX_DEBATE_ROUNDS", "1"))

    data_vendor = os.getenv("TA_DATA_VENDOR", "")
    core_vendor = os.getenv(
        "TA_DATA_VENDOR_CORE",
        data_vendor or "stooq,yfinance,alpha_vantage,local",
    )
    tech_vendor = os.getenv(
        "TA_DATA_VENDOR_TECH",
        data_vendor or "alpha_vantage,yfinance,local",
    )
    fund_vendor = os.getenv(
        "TA_DATA_VENDOR_FUND",
        data_vendor or "yfinance,alpha_vantage",
    )
    news_vendor = os.getenv(
        "TA_DATA_VENDOR_NEWS",
        data_vendor or "seeking_alpha,yfinance,alpha_vantage",
    )
    config["data_vendors"] = {
        "core_stock_apis": core_vendor,
        "technical_indicators": tech_vendor,
        "fundamental_data": fund_vendor,
        "news_data": news_vendor,
    }

    overrides_raw = os.getenv("TA_SEEKING_ALPHA_SYMBOL_OVERRIDES", "").strip()
    if overrides_raw:
        try:
            parsed = json.loads(overrides_raw)
            if isinstance(parsed, dict):
                config["seeking_alpha_symbol_overrides"] = parsed
        except Exception:
            pass

    sa_min_items_raw = os.getenv("TA_SA_MIN_ITEMS", "").strip()
    if sa_min_items_raw:
        try:
            config["seeking_alpha_min_items"] = max(0, int(sa_min_items_raw))
        except ValueError:
            pass

    sa_strict_raw = os.getenv("TA_SA_STRICT_COVERAGE", "").strip().lower()
    if sa_strict_raw in {"1", "true", "yes", "on"}:
        config["seeking_alpha_strict_coverage"] = True
    elif sa_strict_raw in {"0", "false", "no", "off"}:
        config["seeking_alpha_strict_coverage"] = False

    return config


def _runtime_meta(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": config.get("llm_provider"),
        "backend_url": config.get("backend_url"),
        "deep_model": config.get("deep_think_llm"),
        "quick_model": config.get("quick_think_llm"),
        "data_vendors": config.get("data_vendors", {}),
        "research_urls_count": len(config.get("research_material_urls", []) or []),
        "prepared_items_count": len(config.get("prepared_news_items", []) or []),
    }


def _csv_row_count(raw: str) -> int:
    text = raw or ""
    if not text.strip():
        return 0
    data_lines = [ln for ln in text.splitlines() if ln.strip() and not ln.startswith("#")]
    if len(data_lines) <= 1:
        return 0
    try:
        return int(len(pd.read_csv(StringIO("\n".join(data_lines)))))
    except Exception:
        return max(0, len(data_lines) - 1)


def _probe_one_source(
    source: str, fetch_fn, ticker: str, start_date: str, end_date: str
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        raw = fetch_fn(ticker, start_date, end_date)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        rows = _csv_row_count(raw if isinstance(raw, str) else "")
        if rows <= 0:
            return {
                "source": source,
                "ok": False,
                "latency_ms": elapsed_ms,
                "rows": 0,
                "message": "返回为空或无有效K线数据",
            }
        return {
            "source": source,
            "ok": True,
            "latency_ms": elapsed_ms,
            "rows": rows,
            "message": "可用",
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "source": source,
            "ok": False,
            "latency_ms": elapsed_ms,
            "rows": 0,
            "message": str(exc),
        }


def _news_item_count(source: str, raw: str) -> int:
    text = raw or ""
    if not text.strip():
        return 0
    if source == "seeking_alpha":
        return len(re.findall(r"^###\s+\[SA(?:-G)?\d+\]", text, flags=re.MULTILINE))
    if source == "yfinance":
        return len(re.findall(r"^###\s+", text, flags=re.MULTILINE))
    if source == "alpha_vantage":
        try:
            payload = json.loads(text)
            feed = payload.get("feed", [])
            if isinstance(feed, list):
                return len(feed)
        except Exception:
            return 0
    return 0


def _probe_one_news_source(
    source: str, fetch_fn, ticker: str, start_date: str, end_date: str
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        raw = fetch_fn(ticker, start_date, end_date)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        items = _news_item_count(source, raw if isinstance(raw, str) else "")
        ok = items > 0
        message = "可用"
        if source == "seeking_alpha" and isinstance(raw, str):
            m = re.search(r"Coverage qualified items:\s*(\d+).+threshold:\s*(\d+)", raw)
            if m:
                got, need = int(m.group(1)), int(m.group(2))
                message = f"coverage={got}/{need}"
                ok = got >= need
        if not ok and not message:
            message = "返回为空或覆盖不足"
        return {
            "source": source,
            "ok": ok,
            "items": items,
            "latency_ms": elapsed_ms,
            "message": message if ok else (message or "返回为空或覆盖不足"),
        }
    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return {
            "source": source,
            "ok": False,
            "items": 0,
            "latency_ms": elapsed_ms,
            "message": str(exc),
        }


def _infer_event_type(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    if any(k in text for k in ["earnings", "eps", "quarter", "revenue", "guidance"]):
        return "财报/指引"
    if any(k in text for k in ["acquisition", "merger", "m&a", "deal"]):
        return "并购/交易"
    if any(k in text for k in ["sec", "regulator", "lawsuit", "investigation", "compliance", "hkex"]):
        return "监管/法律"
    if any(k in text for k in ["ceo", "cfo", "executive", "management"]):
        return "管理层变动"
    if any(k in text for k in ["product", "launch", "ai", "chip", "platform"]):
        return "产品/业务进展"
    return "其他"


def _short_summary(text: str, max_len: int = 220) -> str:
    clean = re.sub(r"\s+", " ", (text or "")).strip()
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 3] + "..."


def _extract_upload_excerpt(filename: str, data: bytes, max_len: int = 1800) -> str:
    lower = (filename or "").lower()
    try:
        if lower.endswith((".txt", ".md", ".csv", ".json", ".html", ".htm")):
            text = data.decode("utf-8", errors="ignore")
            return _short_summary(text, max_len=max_len)
        if lower.endswith(".pdf"):
            try:
                from pypdf import PdfReader  # type: ignore

                from io import BytesIO

                reader = PdfReader(BytesIO(data))
                pages = []
                for i, page in enumerate(reader.pages):
                    if i >= 2:
                        break
                    pages.append(page.extract_text() or "")
                return _short_summary("\n".join(pages), max_len=max_len)
            except Exception:
                return ""
    except Exception:
        return ""
    return ""


def _make_cite_id(prefix: str, idx: int) -> str:
    return f"{prefix}{idx}"


def _extract_sa_items(raw: str, ticker: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not raw:
        return items
    blocks = re.split(r"\n(?=###\s+\[SA(?:-G)?\d+\])", raw)
    for block in blocks:
        m = re.search(r"###\s+\[(SA(?:-G)?\d+)\]\s+(.+)", block)
        if not m:
            continue
        cite, title = m.group(1).strip(), m.group(2).strip()
        summary = ""
        ms = re.search(r"Summary:\s*(.+)", block)
        if ms:
            summary = ms.group(1).strip()
        mu = re.search(r"URL:\s*(https?://\S+)", block)
        url = mu.group(1).strip() if mu else ""
        mp = re.search(r"Published:\s*(.+)", block)
        published = mp.group(1).strip() if mp else ""
        items.append(
            {
                "cite_id": cite,
                "ticker": ticker,
                "title": title,
                "summary": _short_summary(summary or title),
                "url": url,
                "published_at": published,
                "source": "seeking_alpha",
                "layer": "opinion",
                "event_type": _infer_event_type(title, summary),
                "selected": True,
            }
        )
    return items


def _extract_yf_items(raw: str, ticker: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not raw:
        return items
    blocks = re.split(r"\n(?=###\s+)", raw)
    idx = 1
    for block in blocks:
        m = re.search(r"###\s+(.+?)(?:\s+\(source:\s*(.+?)\))?$", block, flags=re.MULTILINE)
        if not m:
            continue
        title = (m.group(1) or "").strip()
        source = (m.group(2) or "yfinance").strip()
        mu = re.search(r"Link:\s*(https?://\S+)", block)
        url = mu.group(1).strip() if mu else ""
        body = re.sub(r"^###.*$", "", block, flags=re.MULTILINE).strip()
        summary = _short_summary(body)
        items.append(
            {
                "cite_id": _make_cite_id("YF", idx),
                "ticker": ticker,
                "title": title or "Untitled",
                "summary": summary,
                "url": url,
                "published_at": "",
                "source": f"yfinance:{source}",
                "layer": "fact",
                "event_type": _infer_event_type(title, summary),
                "selected": True,
            }
        )
        idx += 1
    return items


def _extract_alpha_items(raw: str, ticker: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not raw:
        return items
    try:
        payload = json.loads(raw)
    except Exception:
        return items
    feed = payload.get("feed", [])
    if not isinstance(feed, list):
        return items
    for idx, article in enumerate(feed, start=1):
        if not isinstance(article, dict):
            continue
        title = str(article.get("title", "")).strip() or "Untitled"
        summary = str(article.get("summary", "")).strip()
        url = str(article.get("url", "")).strip()
        source = str(article.get("source", "alpha_vantage")).strip()
        published = str(article.get("time_published", "")).strip()
        items.append(
            {
                "cite_id": _make_cite_id("AV", idx),
                "ticker": ticker,
                "title": title,
                "summary": _short_summary(summary or title),
                "url": url,
                "published_at": published,
                "source": f"alpha_vantage:{source}",
                "layer": "fact",
                "event_type": _infer_event_type(title, summary),
                "selected": True,
            }
        )
    return items


def _extract_sec_items(ticker: str, trade_date: str) -> list[dict[str, Any]]:
    """
    SEC fact layer for US tickers.
    Uses SEC ticker map + submissions endpoint.
    """
    # Only US-style tickers by default.
    if "." in ticker:
        return []
    ua = os.getenv("TA_SEC_USER_AGENT", "TradingAgents/0.2 (research tooling)")
    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    try:
        mapping_resp = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            timeout=15,
            headers=headers,
        )
        mapping_resp.raise_for_status()
        mapping_data = mapping_resp.json()
    except Exception:
        return []

    cik = None
    for row in mapping_data.values():
        if str(row.get("ticker", "")).upper() == ticker.upper():
            cik = str(row.get("cik_str", "")).zfill(10)
            break
    if not cik:
        return []

    try:
        sub_resp = requests.get(
            f"https://data.sec.gov/submissions/CIK{cik}.json",
            timeout=15,
            headers=headers,
        )
        sub_resp.raise_for_status()
        sub_data = sub_resp.json()
    except Exception:
        return []

    forms = sub_data.get("filings", {}).get("recent", {})
    form_list = forms.get("form", []) or []
    filed_list = forms.get("filingDate", []) or []
    accession_list = forms.get("accessionNumber", []) or []
    primary_doc_list = forms.get("primaryDocument", []) or []

    end_dt = datetime.strptime(trade_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=45)

    items: list[dict[str, Any]] = []
    for idx in range(min(len(form_list), len(filed_list), len(accession_list), len(primary_doc_list))):
        form = str(form_list[idx])
        filed = str(filed_list[idx])
        accession = str(accession_list[idx]).replace("-", "")
        primary_doc = str(primary_doc_list[idx])
        try:
            filed_dt = datetime.strptime(filed, "%Y-%m-%d")
        except Exception:
            continue
        if not (start_dt <= filed_dt <= end_dt + timedelta(days=1)):
            continue
        if form not in {"8-K", "10-Q", "10-K", "6-K", "20-F"}:
            continue
        filing_url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}/{primary_doc}"
        title = f"SEC Filing {form} ({filed})"
        summary = f"Official SEC filing ({form}) disclosed on {filed}."
        items.append(
            {
                "cite_id": _make_cite_id("SEC", len(items) + 1),
                "ticker": ticker,
                "title": title,
                "summary": summary,
                "url": filing_url,
                "published_at": filed,
                "source": "sec_edgar",
                "layer": "fact",
                "event_type": "监管/披露",
                "selected": True,
            }
        )
        if len(items) >= 12:
            break
    return items


def _make_url_item(url: str, ticker: str, idx: int) -> dict[str, Any]:
    lower = url.lower()
    layer = "opinion"
    if any(
        k in lower
        for k in (
            "sec.gov",
            "hkexnews.hk",
            "investor.",
            "/investor",
            "/ir/",
            "annual-report",
            "financial-results",
        )
    ):
        layer = "fact"
    return {
        "cite_id": _make_cite_id("U", idx),
        "ticker": ticker,
        "title": "User Provided URL",
        "summary": _short_summary(f"User supplied source: {url}"),
        "url": url,
        "published_at": "",
        "source": "user_url",
        "layer": layer,
        "event_type": "用户输入",
        "selected": True,
    }


def _dedupe_evidence_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen = set()
    for item in items:
        url = (item.get("url") or "").strip().lower()
        title = re.sub(r"\s+", " ", (item.get("title") or "")).strip().lower()
        key = (url, title)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _build_evidence_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_layer: dict[str, int] = {}
    by_event: dict[str, int] = {}
    by_source: dict[str, int] = {}
    selected_by_layer: dict[str, int] = {}
    selected_total = 0
    for item in items:
        layer = item.get("layer", "unknown")
        evt = item.get("event_type", "其他")
        src = item.get("source", "unknown")
        is_selected = bool(item.get("selected", True))
        by_layer[layer] = by_layer.get(layer, 0) + 1
        by_event[evt] = by_event.get(evt, 0) + 1
        by_source[src] = by_source.get(src, 0) + 1
        if is_selected:
            selected_total += 1
            selected_by_layer[layer] = selected_by_layer.get(layer, 0) + 1
    return {
        "total_items": len(items),
        "selected_items": selected_total,
        "by_layer": by_layer,
        "selected_by_layer": selected_by_layer,
        "by_event_type": by_event,
        "by_source": by_source,
    }


def _build_evidence_signature(pack: dict[str, Any] | None) -> str:
    if not pack:
        return ""
    selected = [it.get("cite_id", "") for it in (pack.get("items") or []) if it.get("selected", True)]
    base = {
        "pack_id": pack.get("pack_id", ""),
        "updated_at": pack.get("updated_at", ""),
        "selected": sorted(selected),
    }
    return json.dumps(base, sort_keys=True, ensure_ascii=False)


def _require_fact_layer_reference(text: str) -> bool:
    lower = (text or "").lower()
    fact_markers = ("sec.gov", "hkexnews.hk", "alphavantage.co", "stooq.com", "sec_edgar")
    return any(m in lower for m in fact_markers)


def _format_pack_news_for_preview(items: list[dict[str, Any]], ticker: str, start_date: str, end_date: str) -> str:
    lines = [f"## Prepared Evidence Pack News for {ticker} ({start_date} to {end_date})", ""]
    for item in items:
        lines.append(f"### [{item.get('cite_id','E')}] {item.get('title','Untitled')}")
        lines.append(f"Layer: {item.get('layer','unknown')} | Source: {item.get('source','unknown')} | Event: {item.get('event_type','其他')}")
        if item.get("summary"):
            lines.append(f"Summary: {item.get('summary')}")
        if item.get("url"):
            lines.append(f"URL: {item.get('url')}")
        lines.append("")
    return "\n".join(lines)


def _build_cache_key(
    ticker: str,
    trade_date: str,
    market: str,
    config: dict[str, Any],
    evidence_signature: str = "",
) -> str:
    signature = {
        "ticker": ticker,
        "trade_date": trade_date,
        "market": market,
        "provider": config.get("llm_provider"),
        "backend_url": config.get("backend_url"),
        "deep": config.get("deep_think_llm"),
        "quick": config.get("quick_think_llm"),
        "data_vendors": config.get("data_vendors", {}),
        "research_urls": sorted(config.get("research_material_urls", []) or []),
        "evidence_signature": evidence_signature,
    }
    return json.dumps(signature, sort_keys=True, ensure_ascii=False)


def _cache_ttl_seconds() -> int:
    try:
        return int(os.getenv("TA_CACHE_TTL_SECONDS", "7200"))
    except ValueError:
        return 7200


def _get_cached_snapshot(cache_key: str) -> dict[str, Any] | None:
    ttl = _cache_ttl_seconds()
    with _CACHE_LOCK:
        item = _CACHE.get(cache_key)
        if not item:
            return None
        age = time.time() - item["created_at"]
        if age > ttl:
            _CACHE.pop(cache_key, None)
            return None
        return item["snapshot"]


def _set_cached_snapshot(cache_key: str, snapshot: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _CACHE[cache_key] = {
            "created_at": time.time(),
            "snapshot": snapshot,
        }


def _save_markdown(run: RunState, final_payload: dict[str, Any], reports: dict[str, str]) -> str:
    runs_dir = BASE_DIR.parent / "webui_runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_ticker = re.sub(r"[^A-Za-z0-9_.-]", "_", run.ticker)
    md_path = runs_dir / f"{ts}_{safe_ticker}_{run.trade_date}_{run.run_id[:8]}.md"

    lines = [
        f"# TradingAgents Report - {run.ticker}",
        "",
        f"- Run ID: `{run.run_id}`",
        f"- Evidence Pack ID: `{run.evidence_pack_id or ''}`",
        f"- Trade Date: `{run.trade_date}`",
        f"- Market: `{run.market}`",
        f"- Research URLs: `{len(run.research_urls)}`",
        f"- Generated At: `{datetime.now().isoformat(timespec='seconds')}`",
        f"- Decision: **{final_payload.get('decision', 'UNKNOWN')}**",
        f"- Cached: `{final_payload.get('cached', False)}`",
        f"- Fact Layer Check: `{final_payload.get('fact_layer_check', False)}`",
        "",
        "## Decision Trace",
        "",
    ]

    if run.research_urls:
        lines.extend(["## User Research URLs", ""])
        for idx, url in enumerate(run.research_urls, start=1):
            lines.append(f"{idx}. {url}")
        lines.append("")

    for field in ("investment_plan", "trader_investment_plan", "final_trade_decision"):
        if reports.get(field):
            lines.extend([f"### {field}", "", reports[field], ""])

    lines.extend(["## Full Modules", ""])
    for field, title in REPORT_FIELDS:
        content = reports.get(field, "")
        if not content:
            continue
        lines.extend([f"### {title} ({field})", "", content, ""])

    lines.extend(["## Final Report", "", final_payload.get("final_report", ""), ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return str(md_path)


def _replay_cached_run(run: RunState, snapshot: dict[str, Any]) -> None:
    _push(run, "progress", {"message": "命中缓存，正在回放历史结果..."})
    runtime = snapshot.get("runtime")
    if runtime:
        _push(run, "runtime", runtime)
    reports = snapshot.get("reports", {})
    for field, title in REPORT_FIELDS:
        content = reports.get(field, "")
        if content:
            _push(
                run,
                "report",
                {
                    "field": field,
                    "title": title,
                    "content": content,
                    "append": False,
                },
            )

    final_payload = dict(snapshot.get("final", {}))
    final_payload["cached"] = True
    final_payload["ticker"] = run.ticker
    final_payload["trade_date"] = run.trade_date
    final_payload["market"] = run.market
    final_payload["evidence_pack_id"] = run.evidence_pack_id
    run.artifact_path = _save_markdown(run, final_payload, reports)
    final_payload["download_path"] = f"/api/runs/{run.run_id}/artifact"
    _push(run, "final", final_payload)
    run.done = True
    _push(run, "done", {"ok": True})


def _extract_decision(text: str) -> str:
    # Prefer explicit final proposal markers if present.
    marker_patterns = [
        r"FINAL\s+TRANSACTION\s+PROPOSAL:\s*\*{0,2}\s*(BUY|SELL|HOLD)\s*\*{0,2}",
        r"FINAL\s+DECISION:\s*\*{0,2}\s*(BUY|SELL|HOLD)\s*\*{0,2}",
        r"\b(RECOMMENDATION|RECOMMEND)\b[:\s-]*\*{0,2}\s*(BUY|SELL|HOLD)\s*\*{0,2}",
    ]
    upper_text = text.upper()
    for pattern in marker_patterns:
        match = re.search(pattern, upper_text)
        if match:
            # Some patterns capture keyword in group(1), decision in group(2)
            candidate = match.group(match.lastindex)
            if candidate in {"BUY", "SELL", "HOLD"}:
                return candidate

    # Fallback: use the last occurrence instead of first occurrence.
    all_hits = re.findall(r"\b(BUY|SELL|HOLD)\b", upper_text)
    return all_hits[-1] if all_hits else "UNKNOWN"


def _build_source_appendix(reports: dict[str, str]) -> str:
    url_pattern = re.compile(r"https?://[^\s)>\"]+")
    opinion_urls: list[str] = []
    fact_urls: list[str] = []
    misc_urls: list[str] = []
    seen = set()
    scan_fields = (
        "news_report",
        "sentiment_report",
        "market_report",
        "fundamentals_report",
        "bull_debate_report",
        "bear_debate_report",
        "investment_plan",
        "trader_investment_plan",
        "final_trade_decision",
    )
    for field in scan_fields:
        text = reports.get(field, "") or ""
        for u in url_pattern.findall(text):
            clean = u.strip().rstrip(".,;")
            if clean in seen:
                continue
            seen.add(clean)
            lower = clean.lower()
            if "seekingalpha.com" in lower or "xueqiu.com" in lower:
                opinion_urls.append(clean)
            elif (
                "sec.gov" in lower
                or "hkexnews.hk" in lower
                or "alphavantage.co" in lower
                or "stooq.com" in lower
            ):
                fact_urls.append(clean)
            else:
                misc_urls.append(clean)

    if not (opinion_urls or fact_urls or misc_urls):
        return ""

    lines = ["## Source References", ""]

    if opinion_urls:
        lines.append("### Opinion Layer Sources")
        lines.append("Seeking Alpha / similar analysis platforms. Requires fact cross-check.")
        lines.append("")
        for i, u in enumerate(opinion_urls, start=1):
            lines.append(f"{i}. {u}")
        lines.append("")

    if fact_urls:
        lines.append("### Fact/Data Layer Sources")
        lines.append("Market/fundamental/official disclosure endpoints.")
        lines.append("")
        for i, u in enumerate(fact_urls, start=1):
            lines.append(f"{i}. {u}")
        lines.append("")

    if misc_urls:
        lines.append("### Additional Sources")
        lines.append("")
        for i, u in enumerate(misc_urls, start=1):
            lines.append(f"{i}. {u}")

    lines.append("")
    return "\n".join(lines)


def _build_translator(config: dict[str, Any]):
    """Build a lightweight translator model using the same provider credentials."""
    try:
        client = create_llm_client(
            provider=config["llm_provider"],
            model=os.getenv("TA_TRANSLATE_MODEL", config["quick_think_llm"]),
            base_url=config.get("backend_url"),
        )
        return client.get_llm()
    except Exception:
        return None


def _to_bilingual(text: str, translator) -> str:
    """Convert English report into EN-CN parallel format."""
    if not text or not translator:
        return text
    if len(text.strip()) < 12:
        return text

    prompt = (
        "You are a financial translator.\n"
        "Task: Convert the input report into bilingual markdown.\n"
        "Rules:\n"
        "1) Keep original English content as-is.\n"
        "2) For each English paragraph, place its Chinese translation directly below it.\n"
        "3) Preserve markdown structure, lists, headings, and tables.\n"
        "4) For tables, keep English table first, then provide a Chinese table right below.\n"
        "5) Output only the final bilingual markdown.\n"
    )
    try:
        result = translator.invoke(
            [
                ("system", prompt),
                ("human", text),
            ]
        )
        if getattr(result, "content", None):
            translated = str(result.content).strip()
            lower = translated.lower()
            invalid_markers = [
                "please provide the content",
                "message appears to be empty",
                "technical issue",
                "i notice the message appears to be empty",
            ]
            if translated and not any(m in lower for m in invalid_markers):
                return translated
        return text
    except Exception:
        return text


def _run_analysis(run: RunState) -> None:
    callback = StatsCallbackHandler()
    _push(run, "progress", {"message": "正在初始化多智能体策略..."})
    try:
        config = _build_runtime_config()
        config["research_material_urls"] = run.research_urls

        evidence_pack = None
        if run.evidence_pack_id:
            with _EVIDENCE_LOCK:
                evidence_pack = _EVIDENCE_PACKS.get(run.evidence_pack_id)
            if not evidence_pack:
                raise RuntimeError(f"Evidence pack not found: {run.evidence_pack_id}")
            selected_items = [
                it for it in (evidence_pack.get("items") or []) if it.get("selected", True)
            ]
            config["prepared_news_items"] = selected_items
            config.setdefault("tool_vendors", {})
            config["tool_vendors"]["get_news"] = "prepared_pack,seeking_alpha,alpha_vantage,yfinance"
            config["tool_vendors"]["get_global_news"] = "prepared_pack,seeking_alpha,yfinance,alpha_vantage"
            _push(
                run,
                "progress",
                {
                    "message": (
                        f"已加载证据包 {run.evidence_pack_id[:8]}，"
                        f"共 {len(selected_items)} 条证据，开始多智能体分析。"
                    )
                },
            )

        _push(run, "runtime", _runtime_meta(config))
        graph = TradingAgentsGraph(debug=False, config=config, callbacks=[callback])
        translator = _build_translator(config)

        args = graph.propagator.get_graph_args(callbacks=[callback])
        state = graph.propagator.create_initial_state(run.ticker, run.trade_date)

        latest_reports: dict[str, str] = {}
        translated_reports: dict[str, str] = {}
        last_tool_signature = ""
        final_state: dict[str, Any] | None = None

        for chunk in graph.graph.stream(state, **args):
            if run.cancel_event.is_set():
                run.cancelled = True
                _push(run, "cancelled", {"message": "任务已取消。"})
                return
            final_state = chunk

            debate_state = chunk.get("investment_debate_state") or {}
            bull_history = debate_state.get("bull_history")
            bear_history = debate_state.get("bear_history")

            if isinstance(bull_history, str) and bull_history.strip():
                prev_bull = latest_reports.get("bull_debate_report", "")
                if prev_bull != bull_history:
                    append_mode = bool(prev_bull) and bull_history.startswith(prev_bull)
                    bull_payload = (
                        bull_history[len(prev_bull):] if append_mode else bull_history
                    )
                    latest_reports["bull_debate_report"] = bull_history
                    translated_reports["bull_debate_report"] = _to_bilingual(
                        bull_history, translator
                    )
                    _push(
                        run,
                        "report",
                        {
                            "field": "bull_debate_report",
                            "title": "Bull Debate Report",
                            "content": _to_bilingual(bull_payload, translator),
                            "append": append_mode,
                        },
                    )
                    _push(run, "progress", {"message": "Bull 辩论内容已更新。"})

            if isinstance(bear_history, str) and bear_history.strip():
                prev_bear = latest_reports.get("bear_debate_report", "")
                if prev_bear != bear_history:
                    append_mode = bool(prev_bear) and bear_history.startswith(prev_bear)
                    bear_payload = (
                        bear_history[len(prev_bear):] if append_mode else bear_history
                    )
                    latest_reports["bear_debate_report"] = bear_history
                    translated_reports["bear_debate_report"] = _to_bilingual(
                        bear_history, translator
                    )
                    _push(
                        run,
                        "report",
                        {
                            "field": "bear_debate_report",
                            "title": "Bear Debate Report",
                            "content": _to_bilingual(bear_payload, translator),
                            "append": append_mode,
                        },
                    )
                    _push(run, "progress", {"message": "Bear 辩论内容已更新。"})

            messages = chunk.get("messages") or []
            if messages:
                last_message = messages[-1]
                tool_calls = getattr(last_message, "tool_calls", None)
                if tool_calls:
                    names = [c.get("name", "tool") for c in tool_calls]
                    signature = ",".join(names)
                    if signature != last_tool_signature:
                        last_tool_signature = signature
                        _push(
                            run,
                            "progress",
                            {"message": f"正在调用工具: {', '.join(names)}"},
                        )

            for field_name, title in REPORT_FIELDS:
                content = chunk.get(field_name)
                if isinstance(content, str) and content.strip():
                    if latest_reports.get(field_name) != content:
                        latest_reports[field_name] = content
                        bilingual_content = _to_bilingual(content, translator)
                        translated_reports[field_name] = bilingual_content
                        _push(
                            run,
                            "report",
                            {
                                "field": field_name,
                                "title": title,
                                "content": bilingual_content,
                            },
                        )
                        _push(
                            run,
                            "progress",
                            {"message": f"{title}已完成。"},
                        )

        if not final_state:
            raise RuntimeError("交易图未返回最终状态。")

        final_text = final_state.get("final_trade_decision", "")
        source_appendix = _build_source_appendix(latest_reports)
        final_text_with_refs = final_text
        if source_appendix and source_appendix not in final_text_with_refs:
            final_text_with_refs = f"{final_text_with_refs}\n\n{source_appendix}"
        final_text_bilingual = _to_bilingual(final_text_with_refs, translator)
        # Always run model-based extraction first to avoid regex picking an earlier opposite word.
        decision = "UNKNOWN"
        if final_text_with_refs:
            try:
                model_decision = str(graph.process_signal(final_text_with_refs)).upper().strip()
                if model_decision in {"BUY", "SELL", "HOLD"}:
                    decision = model_decision
            except Exception:
                decision = "UNKNOWN"

        if decision == "UNKNOWN":
            decision = _extract_decision(final_text_with_refs)

        has_fact_ref = _require_fact_layer_reference(final_text_with_refs)
        if not has_fact_ref:
            final_text_with_refs = (
                "VALIDATION WARNING: Missing Fact Layer citation (SEC/HKEX/market-data URL). "
                "Decision is downgraded to HOLD pending fact verification.\n\n"
                + final_text_with_refs
            )
            decision = "HOLD"
            final_text_bilingual = _to_bilingual(final_text_with_refs, translator)

        final_payload = {
            "ticker": run.ticker,
            "trade_date": run.trade_date,
            "market": run.market,
            "decision": decision,
            "final_report": final_text_bilingual,
            "stats": callback.get_stats(),
            "cached": False,
            "evidence_pack_id": run.evidence_pack_id,
            "fact_layer_check": has_fact_ref,
        }

        # Ensure decision chain modules are present for history export.
        for field in ("investment_plan", "trader_investment_plan", "final_trade_decision"):
            raw_content = final_state.get(field, "")
            if isinstance(raw_content, str) and raw_content.strip():
                translated_reports[field] = _to_bilingual(raw_content, translator)
        if source_appendix:
            translated_reports["source_references"] = _to_bilingual(source_appendix, translator)

        cache_key = _build_cache_key(
            run.ticker,
            run.trade_date,
            run.market,
            config,
            evidence_signature=_build_evidence_signature(evidence_pack),
        )
        snapshot = {
            "final": final_payload,
            "reports": translated_reports,
            "runtime": _runtime_meta(config),
        }
        _set_cached_snapshot(cache_key, snapshot)

        run.artifact_path = _save_markdown(run, final_payload, translated_reports)
        _push(
            run,
            "final",
            {
                **final_payload,
                "download_path": f"/api/runs/{run.run_id}/artifact",
            },
        )
    except Exception as exc:
        _push(
            run,
            "error",
            {
                "message": str(exc),
                "traceback": traceback.format_exc(limit=8),
            },
        )
    finally:
        run.done = True
        _push(run, "done", {"ok": True})


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/data-sources/probe")
def probe_data_sources(request: ProbeRequest) -> dict[str, Any]:
    trade_dt = datetime.strptime(request.trade_date, "%Y-%m-%d")
    start_date = (trade_dt - timedelta(days=180)).strftime("%Y-%m-%d")
    end_date = request.trade_date
    news_start_date = (trade_dt - timedelta(days=14)).strftime("%Y-%m-%d")

    probes = [
        ("stooq", get_stock_data_stooq),
        ("yfinance", get_YFin_data_online),
        ("alpha_vantage", get_alpha_vantage_stock),
        ("local", get_stock_data_local),
    ]
    results = [
        _probe_one_source(name, fn, request.ticker, start_date, end_date)
        for name, fn in probes
    ]

    cfg = _build_runtime_config()
    cfg["research_material_urls"] = []
    set_config(cfg)
    news_probes = [
        ("seeking_alpha", get_news_seeking_alpha),
        ("alpha_vantage", get_alpha_vantage_news),
        ("yfinance", get_news_yfinance),
    ]
    news_results = [
        _probe_one_news_source(name, fn, request.ticker, news_start_date, end_date)
        for name, fn in news_probes
    ]
    return {
        "ticker": request.ticker,
        "market": request.market,
        "start_date": start_date,
        "end_date": end_date,
        "results": results,
        "news_start_date": news_start_date,
        "news_results": news_results,
    }


def _build_evidence_pack(request: EvidencePrepareRequest) -> dict[str, Any]:
    trade_dt = datetime.strptime(request.trade_date, "%Y-%m-%d")
    news_start_date = (trade_dt - timedelta(days=14)).strftime("%Y-%m-%d")
    end_date = request.trade_date

    cfg = _build_runtime_config()
    cfg["research_material_urls"] = request.research_urls
    set_config(cfg)

    raw_reports: dict[str, str] = {}
    items: list[dict[str, Any]] = []
    fetch_errors: list[str] = []

    # Opinion layer: Seeking Alpha feed + user URLs through SA channel.
    try:
        sa_raw = get_news_seeking_alpha(request.ticker, news_start_date, end_date)
        raw_reports["seeking_alpha"] = sa_raw
        items.extend(_extract_sa_items(sa_raw, request.ticker))
    except Exception as exc:
        fetch_errors.append(f"seeking_alpha: {exc}")

    # Fact layer: Alpha Vantage structured news.
    try:
        av_raw = get_alpha_vantage_news(request.ticker, news_start_date, end_date)
        raw_reports["alpha_vantage"] = av_raw
        items.extend(_extract_alpha_items(av_raw, request.ticker))
    except Exception as exc:
        fetch_errors.append(f"alpha_vantage: {exc}")

    # Supplemental layer: yfinance news as additional feed.
    try:
        yf_raw = get_news_yfinance(request.ticker, news_start_date, end_date)
        raw_reports["yfinance"] = yf_raw
        items.extend(_extract_yf_items(yf_raw, request.ticker))
    except Exception as exc:
        fetch_errors.append(f"yfinance: {exc}")

    # Fact layer: SEC filings for US tickers.
    try:
        items.extend(_extract_sec_items(request.ticker, request.trade_date))
    except Exception as exc:
        fetch_errors.append(f"sec: {exc}")

    # Always include user-supplied URLs as first-class evidence items.
    for idx, url in enumerate(request.research_urls, start=1):
        items.append(_make_url_item(url, request.ticker, idx))

    items = _dedupe_evidence_items(items)
    summary = _build_evidence_summary(items)

    pack_id = uuid.uuid4().hex
    created = datetime.now().isoformat(timespec="seconds")
    pack = {
        "pack_id": pack_id,
        "ticker": request.ticker,
        "trade_date": request.trade_date,
        "market": request.market,
        "status": "prepared",
        "confirmed": False,
        "created_at": created,
        "updated_at": created,
        "news_start_date": news_start_date,
        "research_urls": request.research_urls,
        "items": items,
        "summary": summary,
        "fetch_errors": fetch_errors,
        "raw_reports": raw_reports,
    }
    return pack


@app.post("/api/evidence/prepare")
def prepare_evidence(request: EvidencePrepareRequest) -> dict[str, Any]:
    pack = _build_evidence_pack(request)
    with _EVIDENCE_LOCK:
        _EVIDENCE_PACKS[pack["pack_id"]] = pack
    return pack


@app.get("/api/evidence/{pack_id}")
def get_evidence(pack_id: str) -> dict[str, Any]:
    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="证据包不存在")
    return pack


@app.post("/api/evidence/{pack_id}/confirm")
def confirm_evidence(pack_id: str) -> dict[str, Any]:
    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        selected_count = int((pack.get("summary") or {}).get("selected_items", 0))
        if selected_count <= 0:
            raise HTTPException(status_code=400, detail="未选择任何证据，无法确认。")
        pack["confirmed"] = True
        pack["status"] = "confirmed"
        pack["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return {
        "ok": True,
        "pack_id": pack_id,
        "confirmed": True,
        "selected_items": selected_count,
        "summary": pack.get("summary", {}),
    }


@app.post("/api/evidence/{pack_id}/urls")
def add_evidence_urls(pack_id: str, req: EvidenceUrlsRequest) -> dict[str, Any]:
    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        existing = set((u or "").strip() for u in (pack.get("research_urls") or []))
        new_items = []
        next_idx = len(pack.get("research_urls") or []) + 1
        for u in req.urls:
            if u in existing:
                continue
            existing.add(u)
            pack.setdefault("research_urls", []).append(u)
            new_items.append(_make_url_item(u, pack["ticker"], next_idx))
            next_idx += 1

        pack["items"] = _dedupe_evidence_items((pack.get("items") or []) + new_items)
        pack["summary"] = _build_evidence_summary(pack["items"])
        pack["confirmed"] = False
        pack["status"] = "prepared"
        pack["updated_at"] = datetime.now().isoformat(timespec="seconds")
    return {"ok": True, "pack_id": pack_id, "added": len(new_items), "summary": pack["summary"]}


@app.post("/api/evidence/{pack_id}/files")
async def upload_evidence_file(pack_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

    uploads_dir = BASE_DIR.parent / "webui_uploads" / pack_id
    uploads_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", file.filename or "upload.bin")
    dst = uploads_dir / safe_name
    data = await file.read()
    dst.write_bytes(data)

    excerpt = _extract_upload_excerpt(safe_name, data)
    summary = _short_summary(excerpt, max_len=260) if excerpt else f"Uploaded file: {safe_name} ({len(data)} bytes)"
    item = {
        "cite_id": _make_cite_id("UFILE", len(pack.get("items") or []) + 1),
        "ticker": pack["ticker"],
        "title": f"User Uploaded File: {safe_name}",
        "summary": summary,
        "url": f"/api/evidence/{pack_id}/files/{safe_name}",
        "local_path": str(dst),
        "published_at": datetime.now().isoformat(timespec="seconds"),
        "source": "user_file",
        "layer": "fact",
        "event_type": "用户输入文件",
        "selected": True,
    }
    if excerpt:
        item["content_excerpt"] = excerpt

    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        pack["items"] = _dedupe_evidence_items((pack.get("items") or []) + [item])
        pack["summary"] = _build_evidence_summary(pack["items"])
        pack["confirmed"] = False
        pack["status"] = "prepared"
        pack["updated_at"] = datetime.now().isoformat(timespec="seconds")

    return {"ok": True, "pack_id": pack_id, "filename": safe_name, "summary": pack["summary"]}


@app.get("/api/evidence/{pack_id}/files/{filename}")
def get_evidence_file(pack_id: str, filename: str) -> FileResponse:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", filename or "")
    if not safe_name:
        raise HTTPException(status_code=400, detail="文件名不合法")

    uploads_dir = BASE_DIR.parent / "webui_uploads" / pack_id
    path = uploads_dir / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(path, filename=safe_name)


@app.post("/api/evidence/{pack_id}/selection")
def update_evidence_selection(pack_id: str, req: EvidenceSelectionRequest) -> dict[str, Any]:
    with _EVIDENCE_LOCK:
        pack = _EVIDENCE_PACKS.get(pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        selected_set = set(req.selected_cite_ids)
        items = pack.get("items") or []
        for item in items:
            cite_id = str(item.get("cite_id", "")).strip()
            item["selected"] = cite_id in selected_set

        pack["items"] = items
        pack["summary"] = _build_evidence_summary(items)
        pack["confirmed"] = False
        pack["status"] = "prepared"
        pack["updated_at"] = datetime.now().isoformat(timespec="seconds")

    return {
        "ok": True,
        "pack_id": pack_id,
        "selected_items": pack["summary"].get("selected_items", 0),
        "summary": pack["summary"],
    }


@app.post("/api/runs")
def create_run(request: RunRequest) -> dict[str, Any]:
    evidence_pack = None
    if not request.evidence_pack_id:
        raise HTTPException(status_code=400, detail="请先准备并确认证据包，再开始分析。")

    with _EVIDENCE_LOCK:
        evidence_pack = _EVIDENCE_PACKS.get(request.evidence_pack_id)
    if not evidence_pack:
        raise HTTPException(status_code=404, detail="证据包不存在，请重新准备。")
    if not evidence_pack.get("confirmed"):
        raise HTTPException(status_code=400, detail="证据包尚未确认，请先确认后再开始分析。")
    if evidence_pack.get("ticker") != request.ticker:
        raise HTTPException(status_code=400, detail="证据包股票代码与本次请求不一致。")
    if evidence_pack.get("trade_date") != request.trade_date:
        raise HTTPException(status_code=400, detail="证据包决策日期与本次请求不一致。")

    selected_count = int((evidence_pack.get("summary") or {}).get("selected_items", 0))
    if selected_count <= 0:
        raise HTTPException(status_code=400, detail="证据包未选择任何资料，请至少勾选一条证据。")

    run_id = uuid.uuid4().hex
    run = RunState(
        run_id=run_id,
        ticker=request.ticker,
        trade_date=request.trade_date,
        market=request.market,
        research_urls=list(evidence_pack.get("research_urls") or []),
        evidence_pack_id=request.evidence_pack_id,
    )
    with _RUNS_LOCK:
        _RUNS[run_id] = run

    config = _build_runtime_config()
    config["research_material_urls"] = run.research_urls
    cache_key = _build_cache_key(
        request.ticker,
        request.trade_date,
        request.market,
        config,
        evidence_signature=_build_evidence_signature(evidence_pack),
    )
    cached_snapshot = _get_cached_snapshot(cache_key)

    if cached_snapshot:
        thread = threading.Thread(
            target=_replay_cached_run, args=(run, cached_snapshot), daemon=True
        )
    else:
        thread = threading.Thread(target=_run_analysis, args=(run,), daemon=True)
    thread.start()
    return {"run_id": run_id, "cached": bool(cached_snapshot), "evidence_pack_id": run.evidence_pack_id}


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str) -> dict[str, bool | str]:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")
    if run.done:
        return {"ok": False, "message": "任务已结束，无法取消。"}
    run.cancel_event.set()
    _push(run, "progress", {"message": "正在中断任务，请稍候..."})
    return {"ok": True, "message": "已发送取消信号。"}


@app.get("/api/runs/{run_id}/events")
def stream_events(run_id: str) -> StreamingResponse:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")

    def event_stream():
        yield _event(
            "progress",
            {
                "message": f"任务已启动: {run.ticker} @ {run.trade_date} ({run.market})",
            },
        )
        while True:
            try:
                item = run.event_queue.get(timeout=1.0)
                yield _event(item["event"], item["payload"])
                if item["event"] == "done":
                    break
            except queue.Empty:
                yield ": keep-alive\n\n"
                if run.done and run.event_queue.empty():
                    break

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=headers,
    )


@app.get("/api/runs/{run_id}/artifact")
def download_artifact(run_id: str) -> FileResponse:
    with _RUNS_LOCK:
        run = _RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="任务不存在")
    if not run.artifact_path:
        raise HTTPException(status_code=404, detail="报告尚未生成")
    path = Path(run.artifact_path)
    if not path.exists():
        raise HTTPException(status_code=404, detail="报告文件不存在")
    return FileResponse(path, media_type="text/markdown", filename=path.name)


if __name__ == "__main__":
    uvicorn.run("webui.app:app", host="127.0.0.1", port=8080, reload=False)
