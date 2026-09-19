"""
scanner/main.py
---------------
Orchestrates the full scan pipeline:
  1. Load symbols from CSV
  2. Download & compute indicators in batches (yfinance)
  3. Apply Minervini trend-template + RS-percentile filters
  4. Enrich passing stocks with NSE market-cap data
  5. Write CSV outputs and HTML dashboards to docs/
"""

from __future__ import annotations

import logging
import sys
import os
import json
from datetime import datetime
from pathlib import Path

import pandas as pd

from .config     import DOCS_DIR
from .data_loader import download_all, load_symbols
from .nse_client  import enrich_with_market_caps, overlay_price_band_from_cache
from .dashboard   import build_passing_dashboard, build_passing_ema10_dashboard, build_volume_action_dashboard, build_rocket_dashboard, build_industry_drilldown, build_new_rs_high_dashboard, build_stage4_dashboard, build_chartink_embed, build_ema_allocation_table
from .ema_allocation import compute_ema_allocation_all
from .result_calendar import get_result_date
from .indicators  import get_market_sentiment
from . import net_new_highs as nnh
from . import minervini_rank as mrank
from . import new_rs_high
from . import stage4_breakdown
from . import holidays as nse_holidays
from . import sme_scan

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
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

def run() -> None:
    today        = datetime.today()
    today_str    = today.strftime("%Y%m%d")
    date_display = today.strftime("%Y-%m-%d")

    # ── Weekend / holiday guard ───────────────────────────────────────────────
    # NSE is closed on Saturdays, Sundays, and the official 2026 holiday list
    # (see scanner/holidays.py + data/nse_holidays_2026.csv). The scan still
    # RUNS on these days (so a daily cron/GitHub Action never errors out),
    # but the "date" used for the data/output folder is pinned to the most
    # recent real trading day — so weekend/holiday runs never advance the
    # date or overwrite a trading day's data with empty/duplicate data.
    holidays_set = nse_holidays.load_holidays()
    is_holiday   = nse_holidays.is_market_holiday(today.date(), holidays_set)
    if is_holiday:
        trading_date = nse_holidays.last_trading_day(today.date(), holidays_set)
        logger.warning(
            "Today is %s (%s) — NSE market holiday/weekend. "
            "Re-using last trading day %s; data will not be updated for a new date.",
            today.strftime("%A"), today.strftime("%Y-%m-%d"), trading_date,
        )
        today_str    = trading_date.strftime("%Y%m%d")
        date_display = trading_date.strftime("%Y-%m-%d")
    else:
        today_str    = today.strftime("%Y%m%d")
        date_display = today.strftime("%Y-%m-%d")

    # ── Output directory (GitHub Pages root + dated sub-folder) ──────────────
    out_dir = Path(DOCS_DIR) / date_display
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Output directory: %s", out_dir)

    # ── 1. Symbols ────────────────────────────────────────────────────────────
    symbols = load_symbols()
    logger.info("Loaded %d symbols.", len(symbols))

    # ── 2. Download + indicators ──────────────────────────────────────────────
    df = download_all(symbols)
    if df.empty:
        logger.error("No valid data collected. Aborting.")
        sys.exit(1)
    logger.info("Collected rows for %d symbols.", len(df))

    latest_data_date = pd.to_datetime(df["date"]).max().date()
    today = datetime.today().date()

    logger.info("Latest market data: %s", latest_data_date)

    if latest_data_date < today:
      logger.warning(
        "Market data is stale! Latest=%s Today=%s",
        latest_data_date,
        today,
    )

    # ── 3. RS percentile + condition flags ────────────────────────────────────
    df["rs_percentile"]        = df["12m_return_pct"].rank(pct=True) * 100.0
    df["cond8_rs_at_least_70"] = df["rs_percentile"] >= 70.0
    df["all_conditions_met"]   = df[COND_COLS].all(axis=1)

    # ── 4. Save full results ──────────────────────────────────────────────────
    full_path = out_dir / f"full_results_{today_str}.csv"
    df.to_csv(full_path, index=False)
    logger.info("Full results → %s", full_path)

    # ── 5. Passing stocks ─────────────────────────────────────────────────────
    
    # 1. Catch our exact indices so they don't get deleted by the momentum filter
    is_index = df["symbol"].isin(["^CNXSC", "NIFTYSMLCAP250.NS"])
    
    # 2. Keep the stock if it meets all conditions OR if it is one of our indices
    passing = df[df["all_conditions_met"] | is_index].copy()

    if not passing.empty:
        passing = enrich_with_market_caps(passing)
        # Overlay price_band from data/market_cap_cache.csv (populated by
        # scripts/update_price_band_cache.py from your daily NSE file), so
        # your uploaded band data takes priority over the live NSE fetch
        # above without losing traded_value_cr / traded_volume / TV%MC,
        # which only the live call above provides.
        passing = overlay_price_band_from_cache(passing)

    passing_path = out_dir / f"passing_stocks_{today_str}.csv"
    passing.to_csv(passing_path, index=False)
    logger.info("Passing stocks (%d) → %s", len(passing), passing_path)

    # ── 6. Passing + above EMA10 ──────────────────────────────────────────────
    if not passing.empty and "cond9_price_above_ema10" in passing.columns:
        passing_ema10 = (
            passing[passing["cond9_price_above_ema10"]]
            .sort_values("rs_percentile", ascending=False)
            .copy()
        )
    else:
        passing_ema10 = pd.DataFrame()

    ema10_path = out_dir / f"passing_ema10_{today_str}.csv"
    passing_ema10.to_csv(ema10_path, index=False)
    logger.info("Passing+EMA10 stocks (%d) → %s", len(passing_ema10), ema10_path)

    # ── 7. Fresh crossovers ───────────────────────────────────────────────────
    fresh      = df[df["fresh_ma12_cross_today"]].copy()
    fresh_path = out_dir / f"fresh_crossovers_{today_str}.csv"
    fresh.to_csv(fresh_path, index=False)
    logger.info("Fresh crossovers (%d) → %s", len(fresh), fresh_path)

    # ── 8. Volume Action ──────────────────────────────────────────────────────
    volume_action = df[df["volume_signal"] == "ppv"].copy()
    volume_action_path = out_dir / f"volume_action_{today_str}.csv"
    volume_action.to_csv(volume_action_path, index=False)

    # ── 8b. Rocket Stocks (passing + inside bar) ──────────────────────────────
    if "inside_bar" in passing.columns:
        rocket = passing[passing["inside_bar"] == True].copy()
    else:
        rocket = pd.DataFrame()
    rocket_path = out_dir / f"rocket_stocks_{today_str}.csv"
    rocket.to_csv(rocket_path, index=False)
    logger.info("Rocket stocks (%d) → %s", len(rocket), rocket_path)

  

    # ── 9. HTML Dashboards ────────────────────────────────────────────────────

    # ── Compute known_symbols: all symbols seen in the past 10 calendar days ──
    # Used by each dashboard to highlight stocks appearing for the first time.
    known_symbols: set[str] = set()
    history:       list[dict] = []
    docs_root = Path(DOCS_DIR)
    dated_dirs_sorted = sorted(
        [d for d in docs_root.iterdir()
         if d.is_dir() and d.name.replace("-", "").isdigit()
         and len(d.name.replace("-", "")) == 8],
        key=lambda d: d.name,
    )
    for dated_dir in dated_dirs_sorted:
        dir_slug = dated_dir.name.replace("-", "")
        if dir_slug == today_str:
            continue
        # ── history for elite chart ──────────────────────────────────────────
        elite_csv = dated_dir / f"passing_ema10_{dir_slug}.csv"
        if elite_csv.exists():
            try:
                # Skip weekends — NSE is closed Saturday/Sunday
                _dir_dt = datetime.strptime(dir_slug, "%Y%m%d")
                if _dir_dt.weekday() >= 5:
                    logger.debug("Skipping weekend date %s from elite history", dir_slug)
                else:
                    hist_df = pd.read_csv(elite_csv)
                    mc = float(hist_df["total_market_cap_cr"].dropna().sum()) \
                         if "total_market_cap_cr" in hist_df.columns else 0.0
                    tv = float(hist_df["traded_value_cr"].dropna().sum()) \
                         if "traded_value_cr"     in hist_df.columns else 0.0
                    history.append({
                        "date":            dir_slug,
                        "count":           len(hist_df),
                        "market_cap_cr":   mc,
                        "traded_value_cr": tv,
                    })
            except Exception as exc:
                logger.warning("Could not read elite history from %s: %s", elite_csv, exc)
        # ── known_symbols: scan passing_stocks CSVs from last 10 days ────────
        try:
            from datetime import datetime as _dt, timedelta as _td
            dir_date   = _dt.strptime(dir_slug, "%Y%m%d").date()
            today_date = _dt.strptime(today_str, "%Y%m%d").date()
            if (today_date - dir_date).days <= 10:
                for csv_name in [f"passing_stocks_{dir_slug}.csv",
                                 f"passing_ema10_{dir_slug}.csv",
                                 f"volume_action_{dir_slug}.csv",
                                 f"sme_momentum_stocks_{dir_slug}.csv",
                                 f"sme_elite_stocks_{dir_slug}.csv"]:
                    csv_p = dated_dir / csv_name
                    if csv_p.exists():
                        hist_df2 = pd.read_csv(csv_p)
                        if "symbol" in hist_df2.columns:
                            known_symbols.update(
                                str(s).removesuffix(".NS").removesuffix(".BO")
                                for s in hist_df2["symbol"].dropna()
                            )
        except Exception as exc:
            logger.warning("Could not load known_symbols from %s: %s", dated_dir, exc)

    logger.info("Known symbols (last 10 days): %d", len(known_symbols))

    if not passing.empty:
        build_passing_dashboard(
            passing,
            out_dir / f"dashboard_{today_str}.html",
            today_str,
            known_symbols=known_symbols,
        )
        build_rocket_dashboard(
            passing,
            out_dir / f"rocket_dashboard_{today_str}.html",
            today_str,
            known_symbols=known_symbols,
        )

    # ── New RS High — full-universe scan (not just Stage-1-gated stocks) ──────
    logger.info("Scanning for New RS High stocks…")
    try:
        new_rs_df = new_rs_high.compute_new_rs_highs(df, DOCS_DIR, today_str)
    except Exception as exc:
        logger.warning("Could not compute New RS High: %s", exc)
        new_rs_df = pd.DataFrame()
    build_new_rs_high_dashboard(
        new_rs_df,
        out_dir / f"newrshigh_dashboard_{today_str}.html",
        today_str,
        universe_size=len(df),
        known_symbols=known_symbols,
    )

    # ── Stage 4 Breakdown — large-caps below MA50 (full universe scan) ────────
    logger.info("Scanning for Stage 4 Breakdown stocks…")
    try:
        stage4_df = stage4_breakdown.compute_stage4_breakdown(df)
    except Exception as exc:
        logger.warning("Could not compute Stage 4 Breakdown: %s", exc)
        stage4_df = pd.DataFrame()
    build_stage4_dashboard(
        stage4_df,
        out_dir / f"stage4_dashboard_{today_str}.html",
        today_str,
        universe_size=len(df),
        known_symbols=known_symbols,
    )

    if not passing_ema10.empty:
        build_passing_ema10_dashboard(
            passing_ema10,
            out_dir / f"elite_dashboard_{today_str}.html",
            today_str,
            history=history,
            known_symbols=known_symbols,
        )

    if not volume_action.empty:
        build_volume_action_dashboard(
            volume_action,
            out_dir / f"volume_dashboard_{today_str}.html",
            today_str,
            known_symbols=known_symbols,
        )

    # ── 9c. SME Momentum + SME Elite (NSE Emerge / BSE SME universe) ──────────
    # Runs as a fully separate pipeline against data/SME_Stocks.csv — any
    # failure here is caught and logged inside run_sme_scan() itself, so a
    # bad SME data day never breaks the mainboard scan above.
    logger.info("Running SME Momentum + SME Elite scan…")
    try:
        sme_results = sme_scan.run_sme_scan(
            out_dir,
            today_str,
            known_symbols=known_symbols,
        )
    except Exception as exc:
        logger.warning("SME scan failed entirely (non-fatal): %s", exc)
        sme_results = {"sme_momentum": pd.DataFrame(), "sme_elite": pd.DataFrame()}

    # ── 9b. Market Sentiment (NIFTY SMALLCAP 250 index) ──────────────────────
    logger.info("Fetching market sentiment (NIFTY SMALLCAP 250)…")
    sentiment = get_market_sentiment()
    logger.info("Market sentiment:\n%s", json.dumps(sentiment, indent=2, default=str))

    # ── 9c. Net New Highs (market breadth) ────────────────────────────────────
    logger.info("Computing Net New Highs breadth…")
    nnh_stats = nnh.run(df, date_display)

    # ── 9d. Minervini composite ranking ───────────────────────────────────────
    if not passing.empty:
        logger.info("Ranking %d passing stocks…", len(passing))
        ranked = mrank.rank_stocks(
            passing,                 # score these
            passing,                 # relative industry-group strength computed against this same set
            DOCS_DIR,
            today_str,
            sentiment=sentiment,
            nnh_stats=nnh_stats,
        )
        ranked_path = out_dir / f"ranked_stocks_{today_str}.csv"
        ranked.to_csv(ranked_path, index=False)
        logger.info("Ranked stocks → %s (top: %s, grade %s)",
                     ranked_path,
                     ranked.iloc[0]["symbol"] if len(ranked) else "n/a",
                     ranked.iloc[0]["grade"] if len(ranked) else "n/a")

        # Merge the score/grade columns back onto `passing` (by symbol) so
        # anything downstream that already receives `passing` — the Industry
        # Breakdown widget, future dashboard work, etc. — has access to them
        # without needing a second lookup.
        score_cols = ["rank", "rs_score", "vcp_score", "volume_score", "entry_score",
                      "group_score", "entry_status", "minervini_score", "grade",
                      "market_state", "market_multiplier"]
        passing = passing.merge(
            ranked[["symbol"] + score_cols], on="symbol", how="left"
        )
    else:
        ranked = pd.DataFrame()

    # ── 10. Update docs/index.html  (GitHub Pages landing page) ───────────────
    _update_index(today_str, out_dir, len(passing), len(passing_ema10), sentiment=sentiment, nnh_stats=nnh_stats, passing=passing)

    # ── 10. Console summary ───────────────────────────────────────────────────
    logger.info("── SUMMARY ──────────────────────────────")
    logger.info("  Total scanned   : %d", len(df))
    logger.info("  Passing (8 cond): %d", len(passing))
    logger.info("  Passing + EMA10 : %d", len(passing_ema10))
    logger.info("  Fresh crossovers: %d", len(fresh))
    logger.info("  Volume action   : %d", len(volume_action))

# ── Landing-page updater ──────────────────────────────────────────────────────

def _update_index(
    today_str: str,
    out_dir: Path,
    n_passing: int,
    n_elite: int,
    sentiment: dict | None = None,
    nnh_stats: dict | None = None,
    passing: pd.DataFrame | None = None,
) -> None:
    """Regenerate docs/index.html with a link to today's dashboards."""
    docs_root  = Path(DOCS_DIR)
    index_path = docs_root / "index.html"

    # Detect repo sub-path from environment (set by GitHub Actions)
    repo        = os.environ.get("GITHUB_REPOSITORY", "")
    repo_name   = repo.split("/")[-1] if "/" in repo else ""
    base        = f"/{repo_name}" if repo_name else ""

    dated_dirs = sorted(
        [d for d in docs_root.iterdir() if d.is_dir() and d.name[:4].isdigit()],
        reverse=True,
    )

    # Group dated_dirs by month for the index page
    from collections import OrderedDict
    months_map: dict[str, list] = OrderedDict()
    for d in dated_dirs:
        try:
            dt = datetime.strptime(d.name, "%Y-%m-%d")
            month_key = dt.strftime("%B %Y")
        except ValueError:
            month_key = "Other"
        months_map.setdefault(month_key, []).append(d)

    month_groups_html = ""
    for month_key, month_dirs in months_map.items():
        table_rows = ""
        for d in month_dirs:
            date_label = d.name
            try:
                date_label = datetime.strptime(d.name, "%Y-%m-%d").strftime("%d %b %Y")
            except ValueError:
                pass

            slug         = d.name.replace("-", "")
            passing_link = f"{d.name}/dashboard_{slug}.html"
            elite_link   = f"{d.name}/elite_dashboard_{slug}.html"

            table_rows += f"""
        <tr>
          <td class="date-cell">{date_label}</td>
          <td><a href="{passing_link}" class="btn-link">📊 Momentum</a></td>
          <td><a href="{elite_link}"   class="btn-link green">⚡ Elite</a></td>
          <td><a href="{d.name}/volume_dashboard_{slug}.html" class="btn-link blue">🔵 Volume</a></td>
          <td><a href="{d.name}/rocket_dashboard_{slug}.html" class="btn-link amber">🚀 Rocket</a></td>
        </tr>"""

        month_groups_html += f"""
  <div class="month-group">
    <button class="month-accordion" onclick="toggleMonth(this)" aria-expanded="true">
      <span class="month-acc-label">{month_key}</span>
      <span class="month-acc-meta">{len(month_dirs)} scan{'s' if len(month_dirs) != 1 else ''}</span>
      <span class="month-acc-chevron">&#8963;</span>
    </button>
    <div class="month-body open">
      <div class="tbl-wrap">
      <table class="history-table">
        <thead>
          <tr>
            <th>Date</th>
            <th>Minervini Trend Template</th>
            <th>Above EMA10 (Elite)</th>
            <th>Volume Action</th>
            <th>Rocket Stocks</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
      </div>
    </div>
  </div>"""

    # ── Build Industry Group → Industry → Stock drill-down widget ─────────────
    today_date_display = dated_dirs[0].name if dated_dirs else datetime.today().strftime("%Y-%m-%d")
    today_slug          = today_date_display.replace("-", "")
    today_dashboard_link = f"{today_date_display}/dashboard_{today_slug}.html"
    try:
        industry_html = build_industry_drilldown(
            passing if passing is not None else pd.DataFrame(),
            today_date_display,
            dashboard_link=today_dashboard_link,
        )
    except Exception as exc:
        logger.warning("Could not build industry drill-down widget: %s", exc)
        industry_html = ""

    # ── Build the Chartink live-screener embed widget ──────────────────────────
    try:
        chartink_html = build_chartink_embed(
            chartink_url="https://chartink.com/dashboard/160633",
            title="Atlas MB — Chartink Screener",
        )
    except Exception as exc:
        logger.warning("Could not build Chartink embed widget: %s", exc)
        chartink_html = ""

    # ── Build the EMA-Based Allocation Model scorecard (Nifty 50 / Midcap
    #    Select / Smallcap 100) ─────────────────────────────────────────────
    try:
        ema_alloc_results = compute_ema_allocation_all(history_days=20)
        ema_alloc_html = build_ema_allocation_table(ema_alloc_results)
    except Exception as exc:
        logger.warning("Could not build EMA allocation widget: %s", exc)
        ema_alloc_html = ""

    # ── Build the "Dashboards" hub — every scanner + tool, arranged as cards ───
    if dated_dirs:
        _elite_link  = f"{today_date_display}/elite_dashboard_{today_slug}.html"
        _volume_link = f"{today_date_display}/volume_dashboard_{today_slug}.html"
        _rocket_link = f"{today_date_display}/rocket_dashboard_{today_slug}.html"
        _newrshigh_link = f"{today_date_display}/newrshigh_dashboard_{today_slug}.html"
        _stage4_link = f"{today_date_display}/stage4_dashboard_{today_slug}.html"
        _sme_momentum_link = f"{today_date_display}/sme_momentum_dashboard_{today_slug}.html"
        _sme_elite_link    = f"{today_date_display}/sme_elite_dashboard_{today_slug}.html"
    else:
        _elite_link = _volume_link = _rocket_link = _newrshigh_link = _stage4_link = today_dashboard_link
        _sme_momentum_link = _sme_elite_link = today_dashboard_link

    hub_cards = [
        dict(icon="📊", accent="indigo", accent2="blue", title="Momentum Dashboard",
             desc="Every stock passing all 8 Minervini trend-template conditions today.",
             link=today_dashboard_link),
        dict(icon="⚡", accent="emerald", accent2="blue", title="Elite Dashboard",
             desc="Momentum passes also trading above EMA10 — the highest-conviction setups.",
             link=_elite_link),
        dict(icon="🔵", accent="blue", accent2="indigo", title="Volume Action",
             desc="Pocket pivots and unusual volume signals across the NSE universe.",
             link=_volume_link),
        dict(icon="🚀", accent="amber", accent2="red", title="Rocket Stocks",
             desc="Momentum passes coiling inside a tight daily inside-bar, ready to fire.",
             link=_rocket_link),
        dict(icon="🔥", accent="rose", accent2="amber", title="New RS High",
             desc="Relative Strength hitting a fresh multi-month high across the full NSE universe — leadership emerging, even before price confirms it.",
             link=_newrshigh_link),
        dict(icon="📉", accent="red", accent2="navy", title="Stage 4 Breakdown",
             desc="Large-caps (≥ ₹50,000 Cr) currently trading below their 50-day MA — a risk radar, not a buy list.",
             link=_stage4_link),
        dict(icon="🏷️", accent="violet", accent2="blue", title="SME Momentum Scan",
             desc="The same 8 Minervini trend-template conditions, applied to the NSE Emerge + BSE SME universe.",
             link=_sme_momentum_link),
        dict(icon="🏷️", accent="violet", accent2="emerald", title="SME Elite Scan",
             desc="SME Momentum passes also trading above EMA10 — the highest-conviction SME setups.",
             link=_sme_elite_link),
        dict(icon="⭐", accent="amber", accent2="emerald", title="Watchlist",
             desc="Stocks you've starred across every dashboard, with live current price, day change, and industry data.",
             link="watchlist.html"),
    ]

    hub_cards_html = ""
    for c in hub_cards:
        hub_cards_html += f"""
      <a class="hub-card" href="{c['link']}" style="--accent:var(--{c['accent']});--accent2:var(--{c['accent2']});--accent-lt:var(--{c['accent']}-lt);--accent-mid:var(--{c['accent']}-mid)">
        <div class="hub-icon">{c['icon']}</div>
        <div class="hub-title">{c['title']}</div>
        <div class="hub-desc">{c['desc']}</div>
        <div class="hub-cta">Open dashboard <span class="hub-cta-arrow">&#8594;</span></div>
      </a>"""

    hub_html = f"""
<div class="hub-section">
  <div class="hub-header">
    <div>
      <div class="hub-eyebrow"><span class="hub-dot"></span>DASHBOARDS</div>
      <h2 class="hub-heading">Everything, in one place</h2>
      <p class="hub-sub">Live scan results for {today_date_display} &middot; refreshed daily at 18:00 IST</p>
    </div>
  </div>
  <div class="hub-grid">{hub_cards_html}
  </div>
</div>"""

    # ── Tools — kept visually separate; these are utilities, not scan dashboards ──
    tool_cards = [
        dict(icon="📐", accent="violet", accent2="indigo", title="Position Size Calculator",
             desc="Size your next trade against account risk and stop-loss distance.",
             link="position-size.html"),
        dict(icon="📈", accent="navy", accent2="indigo", title="Position Tracker",
             desc="Track open positions, targets, and stops in one place.",
             link="position-tracker.html"),
    ]

    tool_cards_html = ""
    for c in tool_cards:
        tool_cards_html += f"""
      <a class="hub-card tool-card" href="{c['link']}" style="--accent:var(--{c['accent']});--accent2:var(--{c['accent2']});--accent-lt:var(--{c['accent']}-lt);--accent-mid:var(--{c['accent']}-mid)">
        <div class="hub-icon">{c['icon']}</div>
        <div class="hub-title">{c['title']}</div>
        <div class="hub-desc">{c['desc']}</div>
        <div class="hub-cta">Open tool <span class="hub-cta-arrow">&#8594;</span></div>
      </a>"""

    tools_html = f"""
<div class="hub-section tools-section">
  <div class="hub-header">
    <div>
      <div class="hub-eyebrow" style="color:var(--violet)"><span class="hub-dot" style="background:var(--violet);box-shadow:0 0 0 3px var(--violet-lt)"></span>TOOLS</div>
      <h2 class="hub-heading">Trade planning &amp; tracking</h2>
      <p class="hub-sub">Utilities to size and manage your trades — not scan results</p>
    </div>
  </div>
  <div class="hub-grid tools-grid">{tool_cards_html}
  </div>
</div>"""

    # ── Market Sentiment is currently not shown on the homepage (removed for
    #    now — NNH/Market Breadth leads instead). _build_sentiment_html() is
    #    still defined below so it's a one-line change to bring it back.
    nnh_html = nnh.build_html(nnh_stats or {})

    # ── Shared cross-page nav bar — identical component on every page ─────────
    site_nav_html = f"""
<nav class="site-nav">
  <a href="index.html" class="btn-link navy is-active">🏠 Home</a>
  <a href="{today_dashboard_link}" class="btn-link indigo">📊 Momentum</a>
  <a href="{_elite_link}" class="btn-link green">⚡ Elite</a>
  <a href="{_volume_link}" class="btn-link blue">🔵 Volume</a>
  <a href="{_rocket_link}" class="btn-link amber">🚀 Rocket</a>
  <a href="{_newrshigh_link}" class="btn-link rose">🔥 New RS High</a>
  <a href="{_stage4_link}" class="btn-link red">📉 Stage 4</a>
  <a href="{_sme_momentum_link}" class="btn-link violet">🏷️ SME Momentum</a>
  <a href="{_sme_elite_link}" class="btn-link violet">🏷️ SME Elite</a>
  <a href="watchlist.html" class="btn-link gold">⭐ Watchlist</a>
  <a href="position-size.html" class="btn-link violet">📐 Position Size</a>
  <a href="position-tracker.html" class="btn-link navy">📈 Position Tracker</a>
</nav>"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Alpha Momentum \u2014 NSE Trend Scanner</title>
<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700&family=DM+Mono:wght@400;500&display=swap" rel="stylesheet"/>
<style>
  :root{{
    --bg:#ffffff;--surface:#fff;--surface-2:#fbfbfe;--surface2:#fbfbfe;
    --border:#e5e8f0;--border2:#d4d9e8;--border-2:#d4d9e8;
    --text:#0d1426;--muted:#5b6178;--subtle:#9499b3;--subtle-lt:#f3f4f7;--subtle-mid:#d7d9e2;
    --navy:#0f1b3d;--navy2:#16234a;--navy-2:#16234a;--navy-lt:#eef1f8;--navy-mid:#c9d0e3;
    --indigo:#4f46e5;--indigo-lt:#eef0fd;--indigo-mid:#c7d2fe;
    --emerald:#059669;--emerald-lt:#ecfdf5;--emerald-mid:#a7f3d0;
    --blue:#2563eb;--blue-lt:#eff6ff;--blue-mid:#bfdbfe;
    --amber:#b45309;--amber-lt:#fffbeb;--amber-mid:#fde68a;
    --violet:#7c3aed;--violet-lt:#f5f3ff;--violet-mid:#ddd6fe;
    --rose:#e11d48;--rose-lt:#fff1f2;--rose-mid:#fecdd3;
    --slate:#475569;--slate-lt:#f1f5f9;--slate-mid:#cbd5e1;
    --red:#dc2626;--red-lt:#fef2f2;--red-mid:#fca5a5;
    --sans:'Outfit',system-ui,-apple-system,sans-serif;--mono:'DM Mono','SF Mono','Courier New',monospace;
    --radius:12px;--radius-sm:8px;--shadow-sm:0 1px 2px rgba(15,23,42,.04);--shadow-md:0 4px 16px -4px rgba(15,23,42,.08),0 1px 3px rgba(15,23,42,.04);
  }}
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0;}}
  html{{font-size:14px;-webkit-font-smoothing:antialiased;}}
  body{{background:var(--bg);color:var(--text);font-family:var(--sans);line-height:1.6;}}
  .topbar{{height:5px;background:linear-gradient(90deg,var(--navy) 0%,var(--indigo) 40%,var(--blue) 65%,var(--emerald) 100%);}}
  header{{background:var(--surface);border-bottom:1px solid var(--border);
          padding:1.65rem 2.5rem;text-align:center;position:relative;overflow:hidden;}}
  header::before{{content:'';position:absolute;top:-65%;left:50%;transform:translateX(-50%);
                  width:900px;height:260px;pointer-events:none;
                  background:
                    radial-gradient(ellipse 260px 180px at 20% 60%,var(--indigo-lt) 0%,rgba(255,255,255,0) 72%),
                    radial-gradient(ellipse 260px 180px at 50% 40%,var(--emerald-lt) 0%,rgba(255,255,255,0) 72%),
                    radial-gradient(ellipse 260px 180px at 80% 60%,var(--blue-lt) 0%,rgba(255,255,255,0) 72%);
                  opacity:.9;}}
  .header-row{{display:flex;align-items:center;justify-content:space-between;
              gap:1.5rem;flex-wrap:wrap;text-align:left;position:relative;}}
  .header-titles{{flex:1;min-width:220px;text-align:center;}}
  .top-buttons{{display:flex;gap:.65rem;flex-wrap:wrap;}}
  .brand-name-idx{{font-family:var(--mono);font-size:.66rem;font-weight:600;
                   letter-spacing:.16em;text-transform:uppercase;color:var(--navy);
                   display:flex;align-items:center;justify-content:center;gap:.5rem;margin-bottom:.7rem;
                   position:relative;}}
  .brand-dot{{width:7px;height:7px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-lt);}}
  header h1{{font-family:var(--sans);font-size:clamp(1.45rem,2.6vw,1.9rem);font-weight:800;
             letter-spacing:-.025em;margin-bottom:.22rem;position:relative;
             background:linear-gradient(90deg,var(--navy) 0%,var(--indigo) 50%,var(--emerald) 100%);
             -webkit-background-clip:text;background-clip:text;color:transparent;}}
  header p{{color:var(--muted);font-size:.82rem;font-family:var(--mono);position:relative;}}

  .container{{max-width:1120px;margin:2rem auto;padding:0 1.5rem;}}
  h2.section-title{{font-family:var(--sans);font-size:1.05rem;font-weight:700;
                    letter-spacing:-.01em;margin-bottom:1rem;color:var(--text);}}

  /* ── Dashboards hub ── */
  .hub-section{{max-width:1120px;margin:2.2rem auto 2.4rem;padding:0 1.5rem;}}
  .hub-header{{margin-bottom:1.1rem;}}
  .hub-eyebrow{{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);font-size:.62rem;
               font-weight:700;letter-spacing:.14em;color:var(--indigo);margin-bottom:.4rem;}}
  .hub-dot{{width:6px;height:6px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-lt);}}
  .hub-heading{{font-family:var(--sans);font-size:1.3rem;font-weight:700;letter-spacing:-.02em;
               color:var(--text);margin-bottom:.3rem;}}
  .hub-sub{{font-family:var(--sans);font-size:.8rem;color:var(--muted);}}
  .hub-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem;}}
  .hub-card{{
    position:relative;display:flex;flex-direction:column;gap:.6rem;
    background:linear-gradient(165deg,var(--accent-mid) -40%,var(--accent-lt) 15%,var(--surface) 78%);
    border:1px solid var(--border);border-radius:var(--radius);
    padding:1.35rem 1.4rem 1.25rem;box-shadow:var(--shadow-sm);
    text-decoration:none;color:inherit;overflow:hidden;
    transition:transform .18s ease,box-shadow .18s ease,border-color .18s ease;
  }}
  .hub-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:4px;
                     background:linear-gradient(90deg,var(--accent),var(--accent2,var(--accent)));}}
  .hub-card:hover{{transform:translateY(-3px);border-color:var(--accent-mid);
                   box-shadow:var(--shadow-sm),0 14px 30px -14px var(--accent);}}
  .hub-tag{{position:absolute;top:.9rem;right:1rem;font-family:var(--mono);font-size:.56rem;font-weight:700;
           letter-spacing:.08em;color:var(--subtle);background:var(--surface-2);border:1px solid var(--border);
           border-radius:999px;padding:.15rem .5rem;}}
  .hub-icon{{width:38px;height:38px;border-radius:10px;display:flex;align-items:center;justify-content:center;
            font-size:1.1rem;background:linear-gradient(135deg,var(--accent-lt),var(--accent-mid));
            box-shadow:0 3px 10px -3px var(--accent-mid);}}
  .hub-title{{font-family:var(--sans);font-weight:700;font-size:.95rem;letter-spacing:-.01em;color:var(--text);}}
  .hub-desc{{font-family:var(--sans);font-size:.78rem;color:var(--muted);line-height:1.5;flex:1;}}
  .hub-cta{{font-family:var(--mono);font-size:.67rem;font-weight:600;letter-spacing:.03em;color:var(--accent);
           display:flex;align-items:center;gap:.3rem;transition:gap .18s ease;}}
  .hub-card:hover .hub-cta{{gap:.5rem;}}
  .hub-cta-arrow{{transition:transform .18s ease;display:inline-block;}}
  .hub-card:hover .hub-cta-arrow{{transform:translateX(3px);}}

  /* Tools section — visually secondary/compact vs. the main Dashboards grid */
  .tools-section{{margin-top:0;}}
  .tools-grid{{grid-template-columns:repeat(auto-fit,minmax(220px,1fr));max-width:640px;}}
  .tool-card{{padding:1.1rem 1.2rem 1.05rem;}}
  .tool-card .hub-icon{{width:34px;height:34px;font-size:1rem;}}
  .tool-card .hub-title{{font-size:.9rem;}}
  /* Month accordion */
  .month-group{{margin-bottom:1.1rem;}}
  .month-accordion{{
    width:100%;display:flex;align-items:center;gap:.75rem;
    background:var(--surface);border:1px solid var(--border);border-radius:10px;
    padding:.85rem 1.2rem;cursor:pointer;
    font-family:var(--sans);font-size:.92rem;font-weight:600;color:var(--text);
    letter-spacing:-.01em;text-align:left;
    transition:background .15s,box-shadow .15s;
    box-shadow:var(--shadow-sm);
  }}
  .month-accordion:hover{{background:var(--surface-2);box-shadow:var(--shadow-md);}}
  .month-accordion[aria-expanded="true"]{{
    border-bottom-left-radius:0;border-bottom-right-radius:0;
    border-bottom-color:transparent;
    background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo);
  }}
  .month-acc-label{{flex:1;}}
  .month-acc-meta{{font-family:var(--mono);font-size:.67rem;font-weight:500;
                   color:var(--subtle);letter-spacing:.04em;}}
  .month-accordion[aria-expanded="true"] .month-acc-meta{{color:var(--indigo);opacity:.7;}}
  .month-acc-chevron{{font-size:.8rem;transition:transform .3s cubic-bezier(.4,0,.2,1);display:inline-block;}}
  .month-accordion[aria-expanded="false"] .month-acc-chevron{{transform:rotate(180deg);}}
  .month-body{{
    overflow:hidden;
    max-height:2000px;
    transition:max-height .38s cubic-bezier(.4,0,.2,1), opacity .28s ease;
    opacity:1;
    border:1px solid var(--indigo-mid);
    border-top:none;
    border-bottom-left-radius:10px;border-bottom-right-radius:10px;
  }}
  .month-body:not(.open){{max-height:0;opacity:0;border-color:transparent;}}
  /* Scan history table */
  table.history-table{{width:100%;border-collapse:collapse;background:var(--surface);overflow:hidden;}}
  .history-table th{{font-size:.62rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;
      color:var(--subtle);padding:.7rem 1.1rem;text-align:left;
      background:var(--surface-2);border-bottom:1px solid var(--border);}}
  .history-table td{{padding:.85rem 1.1rem;border-bottom:1px solid var(--border);font-size:.85rem;}}
  .history-table tr:last-child td{{border-bottom:none;}}
  .history-table tr:hover td{{background:var(--surface-2);}}
  .date-cell{{font-family:var(--mono);font-weight:600;font-size:.8rem;color:var(--text);}}
  .btn-link{{display:inline-flex;align-items:center;gap:.35rem;padding:.32rem .9rem;border-radius:999px;
             font-family:var(--mono);font-size:.72rem;font-weight:600;
             background:var(--indigo-lt);border:1px solid var(--indigo-mid);color:var(--indigo);
             text-decoration:none;transition:background .14s,box-shadow .14s,transform .14s;letter-spacing:.03em;}}
  .btn-link:hover{{transform:translateY(-1px);box-shadow:0 3px 8px -3px rgba(15,23,42,.18);}}
  .btn-link:hover{{background:#dde2fb;}}
  .btn-link.green{{background:var(--emerald-lt);border-color:var(--emerald-mid);color:var(--emerald);}}
  .btn-link.green:hover{{background:#d7f8ea;}}
  .btn-link.amber{{background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber);}}
  .btn-link.amber:hover{{background:#fef3c7;}}
  .btn-link.blue{{background:var(--blue-lt);border-color:var(--blue-mid);color:var(--blue);}}
  .btn-link.blue:hover{{background:#dee9fd;}}
  .btn-link.violet{{background:var(--violet-lt);border-color:var(--violet-mid);color:var(--violet);}}
  .btn-link.violet:hover{{background:#ede7fd;}}
  .btn-link.navy{{background:var(--navy-lt);border-color:var(--navy-mid);color:var(--navy);}}
  .btn-link.navy:hover{{background:#e2e6f2;}}
  .btn-link.red{{background:var(--red-lt);border-color:var(--red-mid);color:var(--red);}}
  .btn-link.red:hover{{background:#fee2e2;}}
  .btn-link.rose{{background:var(--rose-lt);border-color:var(--rose-mid);color:var(--rose);}}
  .btn-link.rose:hover{{background:#ffe4e6;}}
  .btn-link.slate{{background:var(--slate-lt);border-color:var(--slate-mid);color:var(--slate);}}
  .btn-link.slate:hover{{background:#e2e8f0;}}
  .btn-link.gold{{background:#fff8e1;border-color:#f5d76e;color:#9a7500;}}
  .btn-link.gold:hover{{background:#fef0c2;}}
  .btn-link.is-active{{box-shadow:0 0 0 1px currentColor inset;font-weight:700;}}
  /* Shared cross-page nav bar — identical component on every generated page */
  .site-nav{{display:flex;flex-wrap:wrap;gap:.5rem;align-items:center;padding:.75rem 2.5rem;
            background:color-mix(in srgb, var(--surface-2) 88%, transparent);
            backdrop-filter:blur(10px) saturate(160%);-webkit-backdrop-filter:blur(10px) saturate(160%);
            border-bottom:1px solid var(--border);
            position:sticky;top:0;z-index:100;
            box-shadow:0 6px 18px -10px rgba(15,23,42,.18);}}
  footer{{text-align:center;padding:1.5rem;font-family:var(--mono);font-size:.68rem;
          color:var(--subtle);border-top:1px solid var(--border);
          background:var(--surface);letter-spacing:.04em;margin-top:3rem;}}

  /* ── Net New Highs (uses the same .sentiment-card component) ── */
  .sentiment-section{{max-width:1120px;margin:0 auto 2rem;padding:0 1.5rem;}}
  .sentiment-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1.1rem;margin-top:.85rem;}}
  .sentiment-card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
                   padding:1.3rem 1.5rem;box-shadow:var(--shadow-sm);position:relative;overflow:hidden;}}
  .sentiment-card::before{{content:'';position:absolute;top:0;left:0;right:0;height:4px;
                           background:var(--card-accent,linear-gradient(90deg,var(--indigo),var(--blue)));}}
  .sentiment-card-header{{display:flex;align-items:center;justify-content:space-between;margin-bottom:.85rem;}}
  .sentiment-index-name{{font-weight:700;font-size:.9rem;letter-spacing:-.01em;}}
  .overall-badge{{display:inline-block;padding:.22rem .8rem;border-radius:999px;
                  font-family:var(--mono);font-size:.67rem;font-weight:600;letter-spacing:.06em;text-transform:uppercase;}}
  .overall-badge.bullish{{background:var(--emerald-lt);border:1px solid var(--emerald-mid);color:var(--emerald);}}
  .overall-badge.bearish{{background:var(--red-lt);border:1px solid #fca5a5;color:var(--red);}}
  .overall-badge.mixed{{background:var(--amber-lt);border:1px solid var(--amber-mid);color:var(--amber);}}
  .overall-badge.unavailable{{background:#f8fafc;border:1px solid #e2e8f0;color:var(--subtle);}}
  .ema-row{{display:flex;gap:.65rem;flex-wrap:wrap;}}
  .ema-pill{{display:flex;align-items:center;gap:.35rem;padding:.3rem .8rem;
             border-radius:999px;font-family:var(--mono);font-size:.73rem;font-weight:500;border:1px solid;}}
  .ema-pill.green{{background:var(--emerald-lt);border-color:var(--emerald-mid);color:var(--emerald);}}
  .ema-pill.red{{background:var(--red-lt);border-color:#fca5a5;color:var(--red);}}
  .ema-pill.na{{background:#f8fafc;border-color:#e2e8f0;color:var(--subtle);}}
  .ema-dot{{width:6px;height:6px;border-radius:50%;flex-shrink:0;}}
  .ema-dot.green{{background:var(--emerald);}}
  .ema-dot.red{{background:var(--red);}}
  .ema-dot.na{{background:var(--subtle);}}
  .close-val{{font-family:var(--mono);font-size:.77rem;color:var(--muted);margin-bottom:.7rem;}}
  .close-val strong{{color:var(--text);}}
  .sentiment-legend{{font-family:var(--mono);font-size:.67rem;color:var(--subtle);margin-top:.55rem;}}

  /* ── Horizontal scroll wrapper so history tables don't break mobile layout ── */
  .tbl-wrap{{overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:0 0 10px 10px;}}
  table.history-table{{min-width:560px;}}

  /* ── Mobile responsiveness ── */
  @media (max-width: 768px){{
    header{{padding:1.25rem 1.1rem;}}
    .header-row{{flex-direction:column;align-items:stretch;}}
    .header-titles{{text-align:center;}}
    .top-buttons{{width:100%;}}
    .top-buttons .btn-link{{flex:1;text-align:center;}}
    .site-nav{{padding:.65rem 1.1rem;}}
    .container{{margin:1.4rem auto;padding:0 1rem;}}
    .sentiment-section{{padding:0 1rem;}}
    .sentiment-grid{{grid-template-columns:1fr;}}
    .month-accordion{{padding:.75rem .9rem;gap:.5rem;font-size:.85rem;}}
    .month-acc-meta{{display:none;}}
    .history-table th, .history-table td{{padding:.6rem .75rem;font-size:.78rem;}}
    .btn-link{{padding:.24rem .65rem;font-size:.66rem;}}
    h2.section-title{{font-size:.95rem;}}
    .hub-section{{padding:0 1rem;}}
    .hub-heading{{font-size:1.1rem;}}
    .hub-grid{{grid-template-columns:1fr 1fr;}}
  }}
  @media (max-width: 480px){{
    html{{font-size:13px;}}
    header h1{{font-size:1.4rem;}}
    header p{{font-size:.72rem;}}
    .sentiment-card{{padding:1rem 1.1rem;}}
    .ema-row{{gap:.45rem;}}
    .ema-pill{{font-size:.68rem;padding:.26rem .65rem;}}
    .hub-grid{{grid-template-columns:1fr;}}
  }}
  </style>
</head>
<body>
<div class="topbar"></div>
<header>
  <div class="brand-name-idx"><div class="brand-dot"></div>Alpha Momentum</div>
  <div class="header-row">
    <div class="header-titles">
      <h1>NSE Trend Scanner</h1>
      <p>Daily Minervini trend-template scans · Free-float &amp; liquidity data · NSE India</p>
    </div>
  </div>
</header>

{site_nav_html}

{nnh_html}

{hub_html}

{tools_html}

{chartink_html}

{ema_alloc_html}

{industry_html}

<div class="container">
  <h2 class="section-title">Scan History</h2>
  {month_groups_html}
</div>
<footer>
  Data sourced from NSE India &amp; Yahoo Finance &nbsp;·&nbsp;
  Updated daily at 18:00 IST &nbsp;·&nbsp;
  For informational purposes only — not financial advice
</footer>
<script>
function toggleMonth(btn) {{
  const body = btn.nextElementSibling;
  const open = body.classList.contains('open');
  body.classList.toggle('open', !open);
  btn.setAttribute('aria-expanded', String(!open));
}}
</script>
</body>
</html>"""

    index_path.write_text(html, encoding="utf-8")
    logger.info("Index page updated → %s", index_path)


def _build_sentiment_html(sentiment: dict) -> str:
    """Build the Market Sentiment HTML section from the sentiment dict."""

    def _pill(label: str, above: bool | None, ema_val: float | None) -> str:
        if above is None:
            css  = "na"
            text = f"{label} N/A"
        else:
            css       = "green" if above else "red"
            direction = "above" if above else "below"
            val_str   = f" ({ema_val:,.0f})" if ema_val is not None else ""
            text      = f"Price {direction} {label}{val_str}"
        return f'<span class="ema-pill {css}"><span class="ema-dot {css}"></span>{text}</span>'

    overall = sentiment.get("overall", "unavailable")
    overall_label = {"bullish": "🟢 Bullish", "bearish": "🔴 Bearish",
                     "mixed":   "🟡 Mixed",   "unavailable": "⬜ N/A"}.get(overall, "⬜ N/A")

    # Only show one card — NIFTY SMALLCAP 250
    info      = sentiment.get("cnxsmallcap", {})
    close     = info.get("close")
    ema10     = info.get("ema10")
    ema20     = info.get("ema20")
    above10   = info.get("above_ema10")
    above20   = info.get("above_ema20")

    close_str = f"₹{close:,.2f}" if close is not None else "N/A"
    ema10_str = f"₹{ema10:,.2f}" if ema10 is not None else "N/A"
    pill10    = _pill("EMA10", above10, ema10)
    pill20    = _pill("EMA20", above20, ema20)

    cards_html = f"""
    <div class="sentiment-card">
      <div class="sentiment-card-header">
        <span class="sentiment-index-name">NIFTY Smallcap 250</span>
        <span class="overall-badge {overall}">{overall_label}</span>
      </div>
      <div class="close-val">Last Close: <strong>{close_str}</strong> &nbsp;·&nbsp; 10-EMA: <strong>{ema10_str}</strong></div>
      <div class="ema-row">
        {pill10}
        {pill20}
      </div>
      <div class="sentiment-legend">Green = price above EMA &nbsp;·&nbsp; Red = price below EMA</div>
    </div>"""

    return f"""
<div class="sentiment-section">
  <div style="display:flex;align-items:center;gap:1rem;margin-bottom:.25rem;">
    <h2 class="section-title" style="margin-bottom:0">Market Sentiment</h2>
    <span class="overall-badge {overall}">{overall_label}</span>
  </div>
  <p style="font-size:.8rem;color:var(--muted);margin-bottom:.75rem;">
    NIFTY Smallcap 250 index — price vs 10-EMA and 20-EMA &middot; updated each scan run.
  </p>
  <div class="sentiment-grid">
    {cards_html}
  </div>
</div>"""

if __name__ == "__main__":
    run()
