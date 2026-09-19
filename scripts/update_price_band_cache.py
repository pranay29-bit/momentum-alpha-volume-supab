"""
update_price_band_cache.py
---------------------------
Updates data/market_cap_cache.csv's `price_band` column using your daily
NSE sec_list_DDMMYYYY.csv file (columns: Symbol,Series,Security Name,Band,Remarks).

Why this file: scanner/nse_client.py's enrich_with_market_caps_cached()
reuses whatever's in market_cap_cache.csv as long as it's <14 days old
(CACHE_MAX_AGE_DAYS), instead of live-fetching from NSE. By writing your
band values in here with today's date, the scanner will use YOUR data
directly on its next run instead of calling NSE itself.

This only touches the `price_band` and `last_updated` columns — it never
touches `total_market_cap_cr`, and it preserves existing rows for symbols
not present in today's file (so nothing gets wiped for stocks NSE didn't
list that day, e.g. a data gap).

Usage (from repo root):
    python scripts/update_price_band_cache.py incoming/sec_list_03082026.csv

Then commit + push as usual:
    git add data/market_cap_cache.csv
    git commit -m "band update 2026-08-03"
    git push
(or run.py / the daily_scan workflow next, and it'll pick these up)
"""

import argparse
import re
import pandas as pd
from pathlib import Path
from datetime import date, datetime

ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = ROOT / "data" / "market_cap_cache.csv"
_CACHE_COLUMNS = ["symbol", "total_market_cap_cr", "price_band", "last_updated"]
_PRICE_BAND_EMPTY = "—"


def parse_date_from_filename(filename: str) -> str:
    """sec_list_03082026.csv -> 2026-08-03"""
    match = re.search(r"(\d{2})(\d{2})(\d{4})", filename)
    if not match:
        raise ValueError(f"Could not parse date from filename '{filename}'. Pass --date YYYY-MM-DD instead.")
    dd, mm, yyyy = match.groups()
    return f"{yyyy}-{mm}-{dd}"


def band_to_label(band_value: str) -> str:
    """NSE's Band column -> the same string format dashboard.py expects, e.g. '20', 'No Band'."""
    s = str(band_value).strip()
    if s.lower() == "no band":
        return "No Band"
    return s


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_file", help="Path to NSE sec_list_DDMMYYYY.csv")
    parser.add_argument("--date", help="Override date as YYYY-MM-DD", default=None)
    args = parser.parse_args()

    date_str = args.date or parse_date_from_filename(Path(args.csv_file).name)
    print(f"Using date: {date_str}")

    src = pd.read_csv(args.csv_file)
    src.columns = [c.strip() for c in src.columns]

    if "Symbol" not in src.columns or "Band" not in src.columns:
        raise ValueError("Input file must have 'Symbol' and 'Band' columns.")

    # Build lookup: NSE symbol -> "SYMBOL.NS" (matches cache's format) -> band label
    updates = {}
    for _, r in src.iterrows():
        sym = str(r["Symbol"]).strip().upper()
        cache_key = f"{sym}.NS"
        updates[cache_key] = band_to_label(r["Band"])

    # Load existing cache (or create if missing)
    if CACHE_PATH.exists():
        cache = pd.read_csv(CACHE_PATH)
    else:
        cache = pd.DataFrame(columns=_CACHE_COLUMNS)

    cache = cache.set_index("symbol")

    updated_count, new_count = 0, 0
    for cache_key, band_label in updates.items():
        if cache_key in cache.index:
            cache.loc[cache_key, "price_band"] = band_label
            cache.loc[cache_key, "last_updated"] = date_str
            updated_count += 1
        else:
            # New symbol not previously in cache - add it with no market cap yet
            # (scanner will fill total_market_cap_cr on its own next live fetch, if needed)
            cache.loc[cache_key, "total_market_cap_cr"] = pd.NA
            cache.loc[cache_key, "price_band"] = band_label
            cache.loc[cache_key, "last_updated"] = date_str
            new_count += 1

    cache = cache.reset_index()
    cache.to_csv(CACHE_PATH, index=False)

    print(f"\nDone. {updated_count} existing symbols updated, {new_count} new symbols added.")
    print(f"Total symbols in cache: {len(cache)}")
    print("\nNext: git add data/market_cap_cache.csv && git commit -m 'band update' && git push")
    print("(or just run.py / the daily_scan workflow next - it will use these values)")


if __name__ == "__main__":
    main()
