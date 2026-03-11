from __future__ import annotations

from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests

from .config import get_config


SEEKING_ALPHA_SYMBOL_FEED = "https://seekingalpha.com/api/sa/combined/{symbol}.xml"
SEEKING_ALPHA_GLOBAL_FEEDS = [
    "https://seekingalpha.com/feed.xml",
    "https://seekingalpha.com/market_currents.xml",
]


def _normalize_sa_symbol_candidates(ticker: str) -> list[str]:
    t = (ticker or "").strip().upper()
    if not t:
        return []

    candidates: list[str] = []

    cfg = get_config()
    overrides = cfg.get("seeking_alpha_symbol_overrides", {}) or {}
    mapped = overrides.get(ticker) or overrides.get(t)
    if isinstance(mapped, str) and mapped.strip():
        candidates.append(mapped.strip().upper())
    elif isinstance(mapped, list):
        for item in mapped:
            if isinstance(item, str) and item.strip():
                candidates.append(item.strip().upper())

    candidates.append(t)

    if "." in t:
        base, exch = t.split(".", 1)
        candidates.append(base)
        if exch in {"HK"}:
            # SA may use HK tickers without dot, sometimes without leading zeros.
            candidates.append(base.lstrip("0") or base)

    # Deduplicate while preserving order.
    seen = set()
    deduped = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _parse_rss_items(xml_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    root = ET.fromstring(xml_text)

    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        description = (item.findtext("description") or "").strip()
        pub_date_raw = (item.findtext("pubDate") or "").strip()
        category = (item.findtext("category") or "").strip()

        pub_date: datetime | None = None
        if pub_date_raw:
            try:
                pub_date = parsedate_to_datetime(pub_date_raw)
                if pub_date.tzinfo is not None:
                    pub_date = pub_date.replace(tzinfo=None)
            except Exception:
                pub_date = None

        if not title and not link:
            continue

        items.append(
            {
                "title": title or "Untitled",
                "link": link,
                "summary": description,
                "pub_date": pub_date,
                "category": category,
                "source": "Seeking Alpha RSS",
            }
        )
    return items


def _fetch_rss(url: str) -> list[dict[str, Any]]:
    resp = requests.get(
        url,
        timeout=15,
        headers={
            "User-Agent": "TradingAgents/0.2 (+research material ingestion)",
            "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
        },
    )
    resp.raise_for_status()
    return _parse_rss_items(resp.text)


def _get_user_research_urls() -> list[str]:
    cfg = get_config()
    urls = cfg.get("research_material_urls", []) or []
    out: list[str] = []
    for u in urls:
        if not isinstance(u, str):
            continue
        v = u.strip()
        if not v:
            continue
        out.append(v)
    return out


def _score_item(item: dict[str, Any], ticker: str, start_dt: datetime, end_dt: datetime) -> int:
    score = 0
    title = (item.get("title") or "").upper()
    summary = (item.get("summary") or "").upper()
    category = (item.get("category") or "").upper()
    t = ticker.upper()

    if t in title or t in summary:
        score += 3
    if any(k in category for k in ["EARNINGS", "GUIDANCE", "ANALYSIS", "NEWS"]):
        score += 2
    if len(item.get("summary") or "") > 120:
        score += 1

    dt = item.get("pub_date")
    if isinstance(dt, datetime):
        if start_dt <= dt <= end_dt:
            score += 3
        else:
            score -= 2

    return score


def _format_items(
    ticker: str, start_date: str, end_date: str, items: list[dict[str, Any]], tag_prefix: str = "SA"
) -> str:
    if not items:
        return f"No Seeking Alpha RSS materials found for {ticker} in range {start_date} to {end_date}."

    lines: list[str] = [
        f"## {ticker} Seeking Alpha Materials, from {start_date} to {end_date}",
        "",
    ]

    references: list[str] = []
    for i, item in enumerate(items, start=1):
        cite = f"[{tag_prefix}{i}]"
        pub = item.get("pub_date")
        pub_s = pub.strftime("%Y-%m-%d %H:%M") if isinstance(pub, datetime) else "Unknown"
        score = item.get("quality_score", 0)
        title = item.get("title", "Untitled")
        summary = (item.get("summary") or "").strip()
        url = (item.get("link") or "").strip()
        source = item.get("source", "Seeking Alpha")

        lines.append(f"### {cite} {title}")
        lines.append(f"Source: {source}")
        lines.append(f"Published: {pub_s}")
        lines.append(f"Quality Score: {score}")
        if summary:
            lines.append(f"Summary: {summary}")
        if url:
            lines.append(f"URL: {url}")
        lines.append("")
        if url:
            references.append(f"{cite} {title} - {url}")

    if references:
        lines.append("## References")
        lines.append("")
        lines.extend(references)
        lines.append("")

    return "\n".join(lines)


def get_news_seeking_alpha(ticker: str, start_date: str, end_date: str) -> str:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    end_dt = end_dt.replace(hour=23, minute=59, second=59)

    all_items: list[dict[str, Any]] = []
    errors: list[str] = []

    for symbol in _normalize_sa_symbol_candidates(ticker):
        url = SEEKING_ALPHA_SYMBOL_FEED.format(symbol=symbol)
        try:
            all_items.extend(_fetch_rss(url))
        except Exception as exc:
            errors.append(f"{symbol}: {exc}")

    # Merge user-provided research URLs (no crawling article body).
    for u in _get_user_research_urls():
        try:
            host = urlparse(u).netloc.lower()
        except Exception:
            host = ""
        if "seekingalpha.com" not in host:
            continue
        all_items.append(
            {
                "title": "User Provided Seeking Alpha Material",
                "link": u,
                "summary": "Provided by user as a primary research input.",
                "pub_date": None,
                "category": "USER_INPUT",
                "source": "User Input URL",
            }
        )

    # Deduplicate by link+title
    seen = set()
    uniq_items: list[dict[str, Any]] = []
    for item in all_items:
        key = ((item.get("link") or "").strip(), (item.get("title") or "").strip())
        if key in seen:
            continue
        seen.add(key)
        uniq_items.append(item)

    # Score & filter
    for item in uniq_items:
        item["quality_score"] = _score_item(item, ticker, start_dt, end_dt)
    filtered = [
        i
        for i in uniq_items
        if i["quality_score"] >= 2 or i.get("source") == "User Input URL"
    ]
    filtered.sort(
        key=lambda i: (
            int(i.get("quality_score", 0)),
            i.get("pub_date") or datetime.min,
        ),
        reverse=True,
    )
    top_items = filtered[:12]

    result = _format_items(ticker, start_date, end_date, top_items, tag_prefix="SA")
    if errors and not top_items:
        result += "\nErrors while fetching feeds:\n- " + "\n- ".join(errors)
    return result


def get_global_news_seeking_alpha(curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    end_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = end_dt - timedelta(days=look_back_days)
    start_date = start_dt.strftime("%Y-%m-%d")
    end_date = curr_date

    items: list[dict[str, Any]] = []
    for url in SEEKING_ALPHA_GLOBAL_FEEDS:
        try:
            items.extend(_fetch_rss(url))
        except Exception:
            continue

    # Simple recency filter
    filtered = []
    for it in items:
        dt = it.get("pub_date")
        if not isinstance(dt, datetime):
            continue
        if start_dt <= dt <= end_dt.replace(hour=23, minute=59, second=59):
            it["quality_score"] = 2
            filtered.append(it)

    filtered.sort(key=lambda x: x.get("pub_date") or datetime.min, reverse=True)
    return _format_items("GLOBAL", start_date, end_date, filtered[:limit], tag_prefix="SA-G")
