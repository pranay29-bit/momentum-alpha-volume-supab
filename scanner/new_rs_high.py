"""
scanner/new_rs_high.py
------------------------
"New RS High" scan — stocks whose Relative Strength (rs_percentile,
computed exactly the same way as the Momentum scan: a daily percentile
rank of 12-month return across the whole NSE universe) is making a fresh
trading-day high, not just a price high.

Why this is useful on its own, separate from the Trend Template gate:
a stock's relative strength often turns up *before* its price technically
clears every Trend Template condition (still under its 200-day MA, not
yet 30% above its 52-week low, etc). Catching RS turning up early — the
same "RS line leading price" idea O'Neil/IBD talk about — is exactly the
kind of signal that's most useful on names that AREN'T in the Momentum
dashboard yet. So this scans the FULL daily universe (same population as
Net New Highs), not just Stage-1-gated stocks.

"New RS high" = today's rs_percentile >= the highest rs_percentile that
symbol has had over the trailing LOOKBACK_DAYS trading days (today
excluded from the comparison window). Reconstructs that trailing history
from the daily archive (docs/<date>/full_results_<date>.csv), the same
way scanner/minervini_rank.py does — no new data source, no extra
network calls, just reading back through files the daily scan already
writes.

Public entry point:

    compute_new_rs_highs(df, docs_dir, today_str, lookback_days=60)
        -> pd.DataFrame (subset of df making a new RS high today, with
           two added columns: prior_rs_high, rs_high_history_days)
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

LOOKBACK_DAYS     = 60   # ~1 trading quarter
MIN_HISTORY_DAYS  = 10   # need at least this many archived days before we'll call something a "new high"


def _load_trailing_rs(
    docs_dir: str | Path,
    symbols: set[str],
    today_str: str,
    lookback_days: int = LOOKBACK_DAYS,
) -> dict[str, list[float]]:
    """
    Reconstruct trailing rs_percentile history per symbol by reading back
    through the last `lookback_days` dated archive folders under
    `docs_dir` (docs/<YYYY-MM-DD>/full_results_<YYYYMMDD>.csv). Returns
    {symbol: [rs_percentile, ...]} in chronological order, NOT including
    today (callers compare today's value against this separately).
    """
    docs_root = Path(docs_dir)
    if not docs_root.exists():
        return {}

    dated_dirs = sorted(
        [d for d in docs_root.iterdir()
         if d.is_dir() and d.name.replace("-", "").isdigit()
         and len(d.name.replace("-", "")) == 8
         and d.name.replace("-", "") != today_str],
        key=lambda d: d.name,
    )
    dated_dirs = dated_dirs[-lookback_days:]   # most recent N available (archive may be younger)

    per_symbol: dict[str, list[float]] = {}

    for dated_dir in dated_dirs:
        slug = dated_dir.name.replace("-", "")
        csv_path = dated_dir / f"full_results_{slug}.csv"
        if not csv_path.exists():
            continue
        try:
            cols_available = pd.read_csv(csv_path, nrows=0).columns
            if "symbol" not in cols_available or "rs_percentile" not in cols_available:
                continue
            day_df = pd.read_csv(csv_path, usecols=["symbol", "rs_percentile"])
        except Exception as exc:
            logger.debug("Could not read history file %s: %s", csv_path, exc)
            continue

        day_df = day_df[day_df["symbol"].isin(symbols)]
        if day_df.empty:
            continue

        for _, row in day_df.iterrows():
            rs = row["rs_percentile"]
            if pd.isna(rs):
                continue
            per_symbol.setdefault(row["symbol"], []).append(float(rs))

    return per_symbol


def compute_new_rs_highs(
    df: pd.DataFrame,
    docs_dir: str | Path,
    today_str: str,
    lookback_days: int = LOOKBACK_DAYS,
    min_history_days: int = MIN_HISTORY_DAYS,
) -> pd.DataFrame:
    """
    Return the subset of `df` (today's full scanned universe) whose
    rs_percentile is at or above its own highest value over the trailing
    `lookback_days` trading days — i.e. making a fresh RS high today.

    Symbols without at least `min_history_days` of archived history are
    skipped entirely (not enough evidence to call it a "new" high yet)
    rather than guessed at or force-included.

    Adds two columns to the returned rows:
        prior_rs_high         — highest rs_percentile in the trailing
                                 window (excluding today)
        rs_high_history_days  — how many archived days were actually
                                 available for that symbol (a "new high"
                                 confirmed over 55 days of history is much
                                 stronger evidence than one confirmed over
                                 the 10-day minimum)
    """
    if df is None or df.empty or "rs_percentile" not in df.columns:
        return pd.DataFrame()

    symbols = set(df["symbol"].dropna().unique())
    history = _load_trailing_rs(docs_dir, symbols, today_str, lookback_days)

    matches = []
    for _, row in df.iterrows():
        sym = row.get("symbol")
        today_rs = row.get("rs_percentile", np.nan)
        if pd.isna(today_rs):
            continue

        hist = history.get(sym, [])
        if len(hist) < min_history_days:
            continue

        prior_max = max(hist)
        if today_rs >= prior_max:
            r = row.copy()
            r["prior_rs_high"] = round(prior_max, 2)
            r["rs_high_history_days"] = len(hist)
            matches.append(r)

    if not matches:
        empty_cols = list(df.columns) + ["prior_rs_high", "rs_high_history_days"]
        return pd.DataFrame(columns=empty_cols)

    out = pd.DataFrame(matches).sort_values("rs_percentile", ascending=False).reset_index(drop=True)
    logger.info(
        "New RS High: %d/%d stocks making a fresh %d-day RS high today.",
        len(out), len(df), lookback_days,
    )
    return out
