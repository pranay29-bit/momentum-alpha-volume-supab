"""
scanner/sme_scan.py
--------------------
SME Momentum + SME Elite scan — the same Minervini trend-template pipeline
used for the mainboard scan (scanner/main.py), run against the SME universe
(data/SME_Stocks.csv: NSE Emerge + BSE SME listings) instead of NSE_Stocks.csv.

  - "SME Momentum" = SME stocks passing all 8 Minervini trend-template
    conditions (identical rule set to the mainboard Momentum scan).
  - "SME Elite"     = SME Momentum passes that are ALSO trading above their
    10-period EMA (identical rule to the mainboard Elite scan).

RS percentile is computed WITHIN the SME universe only — ranking a micro-cap
SME stock's 12-month return against the full NSE mainboard would be an
apples-to-oranges comparison, so cond8_rs_at_least_70 uses its own SME-only
percentile rank, exactly mirroring how the mainboard scan ranks its own
universe.

CAVEAT: yfinance has materially patchier price-history coverage for SME/
NSE-Emerge/BSE-SME micro-caps than for mainboard NSE stocks. Expect a lower
fetch-success rate here than the main scan — that's a real limitation of the
data source, not a bug in this pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .data_loader import download_all, load_sme_symbols, load_sme_symbol_metadata
from .nse_client import enrich_with_market_caps, overlay_price_band_from_cache
from .dashboard import build_passing_dashboard, build_passing_ema10_dashboard

logger = logging.getLogger(__name__)

COND_COLS = [
    "cond1_price_above_150_200",
    "cond2_ma150_above_ma200",
    "cond3_ma200_trending_up_1m",
    "cond4_ma50_above_150_200",
    "cond5_price_above_ma50",
    "cond6_30pct_above_52w_low",
    "cond7_within_25pct_52w_high",
    "cond8_rs_at_least_70",
]


def run_sme_scan(
    out_dir: Path,
    today_str: str,
    known_symbols: set[str] | None = None,
) -> dict:
    """
    Run the full SME Momentum + SME Elite pipeline and write CSVs + HTML
    dashboards into `out_dir` (the same dated docs/ folder used by the
    mainboard scan). Returns a dict with the resulting DataFrames so
    scanner/main.py can wire up homepage hub cards / KPIs if useful.

    Fails soft: any stage that raises is logged and skipped, returning
    empty DataFrames rather than aborting the whole run — an SME data
    problem should never take down the mainboard scan.
    """
    result = {
        "sme_full": pd.DataFrame(),
        "sme_momentum": pd.DataFrame(),
        "sme_elite": pd.DataFrame(),
    }

    # ── 1. Symbols + metadata ────────────────────────────────────────────────
    try:
        sme_symbols = load_sme_symbols()
        sme_meta    = load_sme_symbol_metadata()
        logger.info("[SME] Loaded %d SME symbols.", len(sme_symbols))
    except Exception as exc:
        logger.warning("[SME] Could not load SME symbol list: %s", exc)
        return result

    if not sme_symbols:
        logger.warning("[SME] SME symbol list is empty — skipping SME scan.")
        return result

    # ── 2. Download + indicators (reuses the same yfinance batch downloader,
    #    trend-template evaluator, and BSE fallback as the mainboard scan) ──
    sme_df = download_all(sme_symbols, meta=sme_meta)
    if sme_df.empty:
        logger.warning("[SME] No usable SME price data collected — skipping SME scan.")
        return result
    logger.info("[SME] Collected rows for %d SME symbols.", len(sme_df))
    result["sme_full"] = sme_df

    full_path = out_dir / f"sme_full_results_{today_str}.csv"
    sme_df.to_csv(full_path, index=False)
    logger.info("[SME] Full results → %s", full_path)

    # ── 3. RS percentile computed WITHIN the SME universe only ────────────────
    sme_df["rs_percentile"]        = sme_df["12m_return_pct"].rank(pct=True) * 100.0
    sme_df["cond8_rs_at_least_70"] = sme_df["rs_percentile"] >= 70.0
    sme_df["all_conditions_met"]   = sme_df[COND_COLS].all(axis=1)

    # ── 4. SME Momentum (all 8 Minervini conditions passing) ──────────────────
    sme_momentum = sme_df[sme_df["all_conditions_met"]].copy()

    if not sme_momentum.empty:
        try:
            sme_momentum = enrich_with_market_caps(sme_momentum)
            sme_momentum = overlay_price_band_from_cache(sme_momentum)
        except Exception as exc:
            logger.warning("[SME] Market-cap enrichment failed (non-fatal): %s", exc)

    momentum_path = out_dir / f"sme_momentum_stocks_{today_str}.csv"
    sme_momentum.to_csv(momentum_path, index=False)
    logger.info("[SME] SME Momentum passes (%d) → %s", len(sme_momentum), momentum_path)
    result["sme_momentum"] = sme_momentum

    # ── 5. SME Elite (SME Momentum + Close > EMA10) ───────────────────────────
    if not sme_momentum.empty and "cond9_price_above_ema10" in sme_momentum.columns:
        sme_elite = (
            sme_momentum[sme_momentum["cond9_price_above_ema10"]]
            .sort_values("rs_percentile", ascending=False)
            .copy()
        )
    else:
        sme_elite = pd.DataFrame()

    elite_path = out_dir / f"sme_elite_stocks_{today_str}.csv"
    sme_elite.to_csv(elite_path, index=False)
    logger.info("[SME] SME Elite passes (%d) → %s", len(sme_elite), elite_path)
    result["sme_elite"] = sme_elite

    # ── 6. HTML dashboards ─────────────────────────────────────────────────────
    known = known_symbols or set()

    if not sme_momentum.empty:
        try:
            build_passing_dashboard(
                sme_momentum,
                out_dir / f"sme_momentum_dashboard_{today_str}.html",
                today_str,
                known_symbols=known,
                page_title="SME Momentum Scan",
                brand_name="Alpha Momentum · SME Scanner",
                heading="SME Momentum Scan",
                subheading="All 8 Minervini conditions passing · NSE Emerge + BSE SME",
                nav_active="sme_momentum",
                csv_filename=f"sme_momentum_stocks_{today_str}.csv",
                full_csv_filename=f"sme_full_results_{today_str}.csv",
                tv_export_filename=f"tradingview_sme_momentum_{today_str}.txt",
            )
        except Exception as exc:
            logger.warning("[SME] Could not build SME Momentum dashboard: %s", exc)

    if not sme_elite.empty:
        try:
            build_passing_ema10_dashboard(
                sme_elite,
                out_dir / f"sme_elite_dashboard_{today_str}.html",
                today_str,
                known_symbols=known,
                page_title="SME Elite Scan",
                brand_name="Alpha Momentum · SME Elite Filter",
                heading="SME Elite Scan",
                subheading="All 8 Minervini conditions + Close &gt; 10-period EMA · NSE Emerge + BSE SME",
                nav_active="sme_elite",
                csv_filename=f"sme_elite_stocks_{today_str}.csv",
                tv_export_filename=f"tradingview_sme_elite_{today_str}.txt",
            )
        except Exception as exc:
            logger.warning("[SME] Could not build SME Elite dashboard: %s", exc)

    return result
