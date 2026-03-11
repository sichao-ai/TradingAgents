from __future__ import annotations

from datetime import datetime
from io import StringIO

import pandas as pd
import requests


def _to_stooq_symbol(symbol: str) -> str:
    s = symbol.strip().upper()
    if not s:
        raise ValueError("symbol is empty")

    if "." in s:
        code, exch = s.split(".", 1)
        exch = exch.upper()
        if exch in {"HK", "US"}:
            return f"{code.lower()}.{exch.lower()}"
        if exch in {"SS", "SH", "SZ"}:
            # Stooq CN symbol support is inconsistent; use plain code as fallback.
            return code.lower()
        return s.lower()

    # Default plain ticker to US market format on Stooq.
    if s.isalpha():
        return f"{s.lower()}.us"

    return s.lower()


def get_stock_data_stooq(symbol: str, start_date: str, end_date: str) -> str:
    datetime.strptime(start_date, "%Y-%m-%d")
    datetime.strptime(end_date, "%Y-%m-%d")

    stooq_symbol = _to_stooq_symbol(symbol)
    url = "https://stooq.com/q/d/l/"
    resp = requests.get(url, params={"s": stooq_symbol, "i": "d"}, timeout=12)
    resp.raise_for_status()

    text = resp.text.strip()
    if not text:
        raise RuntimeError(f"stooq returned empty response for {symbol} ({stooq_symbol})")

    df = pd.read_csv(StringIO(text))
    if df.empty or "Date" not in df.columns:
        raise RuntimeError(f"stooq returned no usable data for {symbol} ({stooq_symbol})")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    df = df.dropna(subset=["Date"])
    start_ts = pd.to_datetime(start_date)
    end_ts = pd.to_datetime(end_date)
    df = df[(df["Date"] >= start_ts) & (df["Date"] <= end_ts)].copy()

    if df.empty:
        raise RuntimeError(
            f"stooq has no data in range for symbol '{symbol}' between {start_date} and {end_date}"
        )

    for col in ("Open", "High", "Low", "Close"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").round(2)

    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(df)}\n"
    header += f"# Data source: stooq ({stooq_symbol})\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    return header + df.to_csv(index=False)
