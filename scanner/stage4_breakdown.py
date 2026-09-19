"""
scanner/stage4_breakdown.py
------------------------------
"Stage 4 Breakdown" scan:

    today's close < MA50   (i.e. cond5_price_above_ma50 is False)
    AND market cap >= min_market_cap_cr   (₹50,000 Cr by default)

Scans the full daily universe (same population as Net New Highs and New
RS High) for the MA50 condition, then applies a large/mega-cap floor —
same threshold and same reasoning as Breakdown Stocks: this is meant as a
risk radar for big, liquid, widely-held names, not noise from illiquid
small caps whipsawing around their 50-day line.

How this differs from Breakdown Stocks:

    - Breakdown Stocks  = large-cap AND recently confirmed Momentum AND now < MA50
    - Stage 4 Breakdown = large-cap AND now < MA50 (no Momentum-history requirement)

So Stage 4 Breakdown is broader — it doesn't require the stock to have
recently cleared the full 8-condition Trend Template, just that it's a
large stock currently below its 50-day line.

The MA50 condition itself needs no historical archive reads and no
network calls (already computed daily for every stock). The market-cap
floor uses a persistent on-disk cache (see nse_client.enrich_with_market_
caps_cached) rather than fetching fresh every day — a below-MA50 day can
match hundreds of stocks across the full universe, and market cap barely
moves day to day for an already-large stock, so re-fetching all of them
every single run was the actual bottleneck that blew a ~10-15 min daily
workflow up to ~1 hour. After the first run repopulates the cache, only
genuinely new or 14-day-stale symbols cost a network call.

Public entry point:

    compute_stage4_breakdown(df, min_market_cap_cr=50000.0, enrich_fn=None)
        -> pd.DataFrame (subset of df below its 50-day MA AND at/above
           the market-cap floor, sorted worst-first by % below MA50,
           with `pct_below_ma50` and `total_market_cap_cr` columns)
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = {"symbol", "close", "MA50", "cond5_price_above_ma50"}
MIN_MARKET_CAP_CR = 50_000.0  # large/mega-cap floor — same threshold as Breakdown Stocks


def compute_stage4_breakdown(
    df: pd.DataFrame,
    min_market_cap_cr: float = MIN_MARKET_CAP_CR,
    enrich_fn=None,
) -> pd.DataFrame:
    """
    Return large-cap stocks (>= min_market_cap_cr) currently trading below
    their 50-day MA.

    `enrich_fn` defaults to `scanner.nse_client.enrich_with_market_caps_
    cached` — the disk-cached wrapper, not the always-fresh one — since
    this candidate set can be large (imported lazily to avoid a hard
    dependency at module load, and to make this trivially testable with
    a stub).
    """
    if df is None or df.empty:
        return pd.DataFrame()
    if not REQUIRED_COLUMNS.issubset(df.columns):
        logger.warning("Stage 4 Breakdown scan skipped — missing columns: %s",
                        REQUIRED_COLUMNS - set(df.columns))
        return pd.DataFrame()

    candidates = df[df["cond5_price_above_ma50"] == False].copy()  # noqa: E712
    if candidates.empty:
        return candidates

    candidates["pct_below_ma50"] = (
        (candidates["close"].astype(float) - candidates["MA50"].astype(float))
        / candidates["MA50"].astype(float) * 100.0
    )

    if enrich_fn is None:
        from .nse_client import enrich_with_market_caps_cached as enrich_fn

    enriched = enrich_fn(candidates)

    if "total_market_cap_cr" not in enriched.columns:
        logger.warning("Stage 4 Breakdown: market-cap enrichment returned no data — cannot apply the cap floor.")
        return pd.DataFrame()

    out = enriched[enriched["total_market_cap_cr"] >= min_market_cap_cr].copy()
    out = out.sort_values("pct_below_ma50", ascending=True).reset_index(drop=True)  # worst breaks first

    logger.info(
        "Stage 4 Breakdown: %d/%d below-MA50 stocks are large-cap (≥ ₹%.0f Cr).",
        len(out), len(candidates), min_market_cap_cr,
    )
    return out
