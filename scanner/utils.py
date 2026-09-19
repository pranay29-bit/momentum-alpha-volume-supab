"""
scanner/utils.py
----------------
Shared helpers used across the scanner modules.
"""

from __future__ import annotations

import numpy as np


def safe_float(val) -> float:
    """Convert any value to float, returning np.nan on failure."""
    try:
        if isinstance(val, str):
            val = val.replace(",", "")
        return float(val)
    except Exception:
        return np.nan


def fmt_cr(val) -> str:
    """Format a ₹ Crore figure with automatic Lakh-Crore suffix."""
    try:
        v = float(val)
        if np.isnan(v):
            return "N/A"
        if v >= 1_00_000:
            return f"₹{v / 1_00_000:.2f}L Cr"
        return f"₹{v:,.0f} Cr"
    except Exception:
        return "N/A"


def tv_url(symbol_ns: str) -> str:
    """Build a TradingView chart URL for an NSE symbol."""
    sym = symbol_ns.replace(".NS", "").strip()
    return f"https://www.tradingview.com/chart/?symbol=NSE%3A{sym}"
