from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config import get_config


def _resolve_local_data_dir() -> Path:
    # Optional override for users who keep Wind exports elsewhere.
    env_dir = os.getenv("TRADINGAGENTS_LOCAL_DATA_DIR")
    if env_dir:
        return Path(env_dir).expanduser()

    cfg = get_config()
    return Path(cfg.get("data_cache_dir", "data")) / "wind"


def _find_csv_file(symbol: str) -> Path:
    data_dir = _resolve_local_data_dir()
    if not data_dir.exists():
        raise FileNotFoundError(
            f"Local data directory not found: {data_dir}. "
            "Set TRADINGAGENTS_LOCAL_DATA_DIR or create this folder."
        )

    patterns = [
        f"{symbol.upper()}.csv",
        f"{symbol.upper()}*.csv",
        f"{symbol.lower()}.csv",
        f"{symbol.lower()}*.csv",
    ]

    candidates = []
    for pattern in patterns:
        candidates.extend(data_dir.glob(pattern))

    if not candidates:
        raise FileNotFoundError(
            f"No CSV file found for symbol '{symbol}' under {data_dir}. "
            f"Expected file like {symbol.upper()}.csv"
        )

    candidates = sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename_map = {}
    lowered = {str(c).strip().lower(): c for c in df.columns}

    aliases = {
        "Date": ["date", "datetime", "time", "trade_date"],
        "Open": ["open", "open_price"],
        "High": ["high", "high_price"],
        "Low": ["low", "low_price"],
        "Close": ["close", "close_price"],
        "Adj Close": ["adj close", "adj_close", "adjusted_close", "adjclose"],
        "Volume": ["volume", "vol"],
    }

    for target, names in aliases.items():
        for name in names:
            if name in lowered:
                rename_map[lowered[name]] = target
                break

    df = df.rename(columns=rename_map)

    required = ["Date", "Open", "High", "Low", "Close"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"CSV missing required columns: {missing}. "
            "Please export with columns Date, Open, High, Low, Close, Volume."
        )

    if "Volume" not in df.columns:
        df["Volume"] = 0

    return df


def load_local_ohlcv(symbol: str) -> pd.DataFrame:
    csv_file = _find_csv_file(symbol)
    data = pd.read_csv(csv_file)
    data = _normalize_ohlcv_columns(data)

    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])
    data = data.sort_values("Date")

    # Keep the common OHLCV schema expected by existing tools.
    keep_cols = ["Date", "Open", "High", "Low", "Close", "Adj Close", "Volume"]
    existing = [c for c in keep_cols if c in data.columns]
    return data[existing].copy()


def get_stock_data_local(symbol: str, start_date: str, end_date: str) -> str:
    try:
        datetime.strptime(start_date, "%Y-%m-%d")
        datetime.strptime(end_date, "%Y-%m-%d")

        data = load_local_ohlcv(symbol)
        start_ts = pd.to_datetime(start_date)
        end_ts = pd.to_datetime(end_date)
        filtered = data[(data["Date"] >= start_ts) & (data["Date"] <= end_ts)].copy()

        if filtered.empty:
            return (
                f"No local data found for symbol '{symbol}' between {start_date} and {end_date}. "
                "Please verify your Wind CSV date range."
            )

        for col in ("Open", "High", "Low", "Close", "Adj Close"):
            if col in filtered.columns:
                filtered[col] = pd.to_numeric(filtered[col], errors="coerce").round(2)

        header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
        header += f"# Total records: {len(filtered)}\n"
        header += f"# Data source: local CSV (Wind export)\n"
        header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        return header + filtered.to_csv(index=False)
    except Exception as e:
        return (
            f"Error reading local CSV for {symbol}: {e}. "
            "Please place a Wind-exported CSV and verify columns Date, Open, High, Low, Close, Volume."
        )
