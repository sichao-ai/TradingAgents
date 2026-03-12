from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class SAClaim:
    cite: str
    title: str
    summary: str
    url: str
    quality_score: int
    polarity: str  # bull / bear / neutral


_POSITIVE_HINTS = (
    "beat",
    "upside",
    "growth",
    "outperform",
    "upgrade",
    "strong",
    "record",
    "expansion",
    "moat",
    "improve",
)

_NEGATIVE_HINTS = (
    "miss",
    "downside",
    "risk",
    "downgrade",
    "weak",
    "decline",
    "lawsuit",
    "headwind",
    "recession",
    "margin pressure",
)


def _classify_polarity(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    pos = sum(1 for k in _POSITIVE_HINTS if k in text)
    neg = sum(1 for k in _NEGATIVE_HINTS if k in text)
    if pos > neg:
        return "bull"
    if neg > pos:
        return "bear"
    return "neutral"


def parse_sa_claims(news_report: str) -> list[SAClaim]:
    claims: list[SAClaim] = []
    if not news_report:
        return claims

    # Parse sections like:
    # ### [SA1] title
    # Quality Score: 6
    # Summary: ...
    # URL: ...
    blocks = re.split(r"\n(?=###\s+\[SA(?:-G)?\d+\]\s+)", news_report)
    for block in blocks:
        m_head = re.search(r"###\s+\[(SA(?:-G)?\d+)\]\s+(.+)", block)
        if not m_head:
            continue
        cite = m_head.group(1).strip()
        title = m_head.group(2).strip()
        m_q = re.search(r"Quality Score:\s*(-?\d+)", block)
        m_sum = re.search(r"Summary:\s*(.+)", block)
        m_url = re.search(r"URL:\s*(https?://\S+)", block)

        quality = int(m_q.group(1)) if m_q else 0
        summary = m_sum.group(1).strip() if m_sum else ""
        url = m_url.group(1).strip() if m_url else ""

        claims.append(
            SAClaim(
                cite=cite,
                title=title,
                summary=summary,
                url=url,
                quality_score=quality,
                polarity=_classify_polarity(title, summary),
            )
        )
    return claims


def build_sa_claim_brief(news_report: str, role: str, max_claims: int = 6) -> str:
    claims = parse_sa_claims(news_report)
    if not claims:
        return "No structured Seeking Alpha claims available."

    claims = sorted(claims, key=lambda c: c.quality_score, reverse=True)
    role = (role or "").lower()

    if role == "bull":
        selected = [c for c in claims if c.polarity in {"bull", "neutral"}][:max_claims]
        opposite = [c for c in claims if c.polarity == "bear"][: max(1, max_claims // 2)]
    elif role == "bear":
        selected = [c for c in claims if c.polarity in {"bear", "neutral"}][:max_claims]
        opposite = [c for c in claims if c.polarity == "bull"][: max(1, max_claims // 2)]
    else:
        selected = claims[:max_claims]
        opposite = []

    lines: list[str] = [
        "Seeking Alpha Opinion Claims (Opinion Layer, must be fact-checked):",
    ]

    if selected:
        lines.append("Primary claims:")
        for c in selected:
            lines.append(
                f"- [{c.cite}] {c.title} | score={c.quality_score} | polarity={c.polarity} | "
                f"summary={c.summary[:180]} | url={c.url}"
            )

    if opposite:
        lines.append("Counter-side claims to address:")
        for c in opposite:
            lines.append(
                f"- [{c.cite}] {c.title} | score={c.quality_score} | polarity={c.polarity} | "
                f"summary={c.summary[:160]} | url={c.url}"
            )

    lines.append(
        "Citation rule: keep [SAx] tags when citing these claims; "
        "for any decisive statement, pair with at least one fact evidence from market/fundamentals."
    )
    return "\n".join(lines)
