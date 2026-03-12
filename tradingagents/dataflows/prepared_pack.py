from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .config import get_config


def _filter_items_for_ticker(items: list[dict[str, Any]], ticker: str) -> list[dict[str, Any]]:
    t = (ticker or "").upper()
    out = []
    for item in items:
        it_ticker = str(item.get("ticker", "")).upper()
        if not it_ticker or it_ticker == t:
            out.append(item)
    return out


def _format_items(items: list[dict[str, Any]], title: str) -> str:
    if not items:
        return f"{title}\n\nNo prepared evidence items available."

    lines = [title, ""]
    refs: list[str] = []
    for i, item in enumerate(items, start=1):
        cite = item.get("cite_id") or f"E{i}"
        it_title = item.get("title", "Untitled")
        source = item.get("source", "unknown")
        layer = item.get("layer", "unknown")
        event_type = item.get("event_type", "unknown")
        summary = item.get("summary", "")
        url = item.get("url", "")
        published_at = item.get("published_at", "")

        lines.append(f"### [{cite}] {it_title}")
        lines.append(f"Layer: {layer}")
        lines.append(f"Source: {source}")
        lines.append(f"Event Type: {event_type}")
        if published_at:
            lines.append(f"Published: {published_at}")
        if summary:
            lines.append(f"Summary: {summary}")
        if url:
            lines.append(f"URL: {url}")
            refs.append(f"[{cite}] {it_title} - {url}")
        lines.append("")

    if refs:
        lines.append("## References")
        lines.append("")
        lines.extend(refs)
        lines.append("")
    return "\n".join(lines)


def get_news_from_pack(ticker: str, start_date: str, end_date: str) -> str:
    cfg = get_config()
    items = cfg.get("prepared_news_items", []) or []
    items = _filter_items_for_ticker(items, ticker)

    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)

    filtered = []
    for item in items:
        pub = item.get("published_at")
        if not pub:
            filtered.append(item)
            continue
        try:
            pub_dt = datetime.fromisoformat(pub)
            if start_dt <= pub_dt <= end_dt:
                filtered.append(item)
        except Exception:
            filtered.append(item)

    title = f"## Prepared Evidence News for {ticker}, from {start_date} to {end_date}"
    return _format_items(filtered, title)


def get_global_news_from_pack(curr_date: str, look_back_days: int = 7, limit: int = 20) -> str:
    cfg = get_config()
    items = cfg.get("prepared_news_items", []) or []
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - timedelta(days=look_back_days)

    filtered = []
    for item in items:
        pub = item.get("published_at")
        if not pub:
            continue
        try:
            pub_dt = datetime.fromisoformat(pub)
            if start_dt <= pub_dt <= curr_dt + timedelta(days=1):
                filtered.append(item)
        except Exception:
            continue

    filtered.sort(key=lambda i: i.get("published_at") or "", reverse=True)
    title = f"## Prepared Global Evidence News, from {start_dt.strftime('%Y-%m-%d')} to {curr_date}"
    return _format_items(filtered[:limit], title)
