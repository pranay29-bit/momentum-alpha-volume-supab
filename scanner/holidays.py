"""
scanner/holidays.py
--------------------
NSE non-trading day calendar.

Source for the weekday holidays: NSE Circular NSE/CMTR/71775 (Dec 12, 2025)
  https://nsearchives.nseindia.com/content/circulars/CMTR71775.pdf
Every Saturday and Sunday of 2026 is also included, since NSE is closed
on both regardless of the circular's holiday list.

`data/nse_holidays_2026.csv` has columns: date, day, description.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

logger = logging.getLogger(__name__)

HOLIDAYS_PATH = DATA_DIR / "nse_holidays_2026.csv"


def load_holidays(path: Path = HOLIDAYS_PATH) -> set[date]:
    """Return the set of non-trading dates (holidays + weekends) as date objects."""
    if not path.exists():
        logger.warning("Holiday calendar not found at %s — falling back to weekend-only check.", path)
        return set()
    df = pd.read_csv(path, parse_dates=["date"])
    return set(df["date"].dt.date)


def is_market_holiday(d: date, holidays: set[date] | None = None) -> bool:
    """True if *d* is a non-trading day (weekend or NSE holiday)."""
    if holidays is None:
        holidays = load_holidays()
    if holidays:
        return d in holidays
    # Fallback if the calendar file is missing: weekend-only check.
    return d.weekday() >= 5


def last_trading_day(d: date, holidays: set[date] | None = None, max_lookback: int = 14) -> date:
    """
    Walk backwards from *d* (inclusive) until a non-holiday date is found.
    Used so that when the scanner runs on a weekend/holiday, the data and
    output folder keep using the date of the last real trading session
    instead of advancing to a day with no market activity.
    """
    if holidays is None:
        holidays = load_holidays()
    cursor = d
    for _ in range(max_lookback):
        if not is_market_holiday(cursor, holidays):
            return cursor
        cursor -= timedelta(days=1)
    logger.warning("Could not find a trading day within %d days of %s — using %s as-is.", max_lookback, d, d)
    return d
