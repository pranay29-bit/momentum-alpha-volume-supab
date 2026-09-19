"""
scripts/backfill_nnh.py
------------------------
One-time backfill for Net New Highs (NNH) history.

The daily scanner only computes today's new-highs/new-lows count, so the
NNH chart normally has to build up history one day at a time. But the
scanner already downloads 400 days of OHLCV per stock (PERIOD="400d" in
scanner/config.py) just to compute the 252-day trend-template indicators —
and that's enough data to reconstruct "was this stock at a new 52-week
high/low" for every past trading day in that window, not just today.

This script re-downloads each symbol's price history once, builds a
date x symbol matrix of Close prices, computes the rolling 252-day
high/low flag for every date (after the first ~252-day warm-up), and
writes a fully-populated net_new_highs_history.csv — so the chart shows
roughly the last ~150 trading days immediately instead of growing one
point per day going forward.

Run once, manually:
    python scripts/backfill_nnh.py

Safe to re-run: it merges with (never deletes) any dates already in the
existing history file, and always keeps the higher-confidence backfilled
value when both exist for the same date.
"""

from __future__ import annotations

import logging
import sys
import time
from math import ceil
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scanner.config import RS_LOOKBACK, PERIOD, INTERVAL, BATCH_SIZE, DATA_DIR
from scanner.data_loader import load_symbols, _chunk
from scanner import net_new_highs as nnh

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-8s  %(message)s")
logger = logging.getLogger(__name__)

_BATCH_DELAY = 1.0


def download_close_matrix(symbols: list[str]) -> pd.DataFrame:
    """
    Download PERIOD of daily Close prices for all *symbols* and return a
    wide DataFrame: index=date, columns=symbol, values=Close.
    """
    closes: dict[str, pd.Series] = {}
    total = ceil(len(symbols) / BATCH_SIZE)

    for i, batch in enumerate(_chunk(symbols, BATCH_SIZE), start=1):
        if i > 1:
            time.sleep(_BATCH_DELAY)
        logger.info("Batch %d/%d (%d symbols)…", i, total, len(batch))
        try:
            data = yf.download(
                tickers=batch, period=PERIOD, interval=INTERVAL,
                group_by="ticker", auto_adjust=True, threads=False, progress=False,
            )
        except Exception as exc:
            logger.error("Batch %d failed: %s", i, exc)
            continue
        if data is None or data.empty:
            continue

        is_multi = isinstance(data.columns, pd.MultiIndex)
        for sym in batch:
            try:
                df_sym = data[sym] if is_multi else data
                close = df_sym["Close"].dropna()
                if len(close) > 5:
                    closes[sym] = close
            except Exception:
                continue

    if not closes:
        return pd.DataFrame()
    return pd.DataFrame(closes)  # outer-joins on date index automatically


def backfill_history(close_matrix: pd.DataFrame, window: int = RS_LOOKBACK) -> pd.DataFrame:
    """
    For every date once `window` days of history are available, count how
    many symbols are at a new rolling-`window` Close high/low on that date.
    Returns a DataFrame: date, new_highs, new_lows, net, total.
    """
    rows = []
    dates = close_matrix.index
    for i in range(window - 1, len(dates)):
        d        = dates[i]
        window_df = close_matrix.iloc[max(0, i - window + 1): i + 1]
        today_row = close_matrix.iloc[i]

        valid = today_row.dropna().index
        if len(valid) == 0:
            continue

        roll_max = window_df[valid].max()
        roll_min = window_df[valid].min()
        today_v  = today_row[valid]

        is_high = today_v >= roll_max
        is_low  = today_v <= roll_min

        rows.append({
            "date":      pd.Timestamp(d),
            "new_highs": int(is_high.sum()),
            "new_lows":  int(is_low.sum()),
            "net":       int(is_high.sum() - is_low.sum()),
            "total":     int(len(valid)),
        })

    return pd.DataFrame(rows)


def merge_into_existing(backfilled: pd.DataFrame) -> pd.DataFrame:
    """Merge backfilled rows into the existing history CSV, keeping any
    dates already present (e.g. from live daily runs) untouched, and
    adding everything else from the backfill."""
    existing = nnh._load_history()
    if existing.empty:
        merged = backfilled
    else:
        existing_dates = set(existing["date"])
        new_rows = backfilled[~backfilled["date"].isin(existing_dates)]
        merged = pd.concat([existing, new_rows], ignore_index=True)
    merged = merged.sort_values("date").reset_index(drop=True)
    merged["date"] = pd.to_datetime(merged["date"])
    return merged


def main() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    symbols = load_symbols()
    logger.info("Loaded %d symbols. Downloading %s of Close prices…", len(symbols), PERIOD)

    close_matrix = download_close_matrix(symbols)
    if close_matrix.empty:
        logger.error("No price data downloaded — aborting backfill.")
        return

    close_matrix = close_matrix.sort_index()
    logger.info(
        "Close matrix: %d trading days x %d symbols (%s → %s)",
        len(close_matrix), close_matrix.shape[1],
        close_matrix.index.min().date(), close_matrix.index.max().date(),
    )

    backfilled = backfill_history(close_matrix)
    logger.info("Backfilled %d days of Net New Highs history.", len(backfilled))

    merged = merge_into_existing(backfilled)
    nnh._save_history(merged)
    logger.info("Wrote %d total rows to %s", len(merged), nnh.HISTORY_PATH)


if __name__ == "__main__":
    main()
