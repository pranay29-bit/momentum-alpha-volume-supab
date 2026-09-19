"""
scanner/minervini_rank.py
--------------------------
Composite ranking system for Stage-1 (Trend Template) stocks, blending
Mark Minervini's SEPA/VCP approach with William O'Neil's CANSLIM — which
isn't a stretch, since Minervini's own approach is explicitly built on top
of O'Neil's (he started out at William O'Neil + Co.). Where the two
frameworks overlap, one pillar covers both; where O'Neil is genuinely
distinct, it gets its own pillar rather than being silently absorbed.

This module does NOT re-filter the Trend Template gate — it assumes you're
handing it stocks that already cleared the 8-condition gate (your existing
`passing` DataFrame). It applies one more hard gate of its own (liquidity /
market cap), then ranks the survivors against each other using six weighted
technical pillars, plus a market condition overlay applied to the final
score.

Full rationale / formulas: see the accompanying ranking spec doc. Short
version of the six pillars + weights (technical-only mode, no fundamentals
data source wired up yet):

    RS / Leadership Quality      20%   (Minervini RS + O'Neil "L")
    VCP Base Quality             20%   (Minervini)
    Volume / Supply-Demand       18%   (Minervini + O'Neil "S")
    New-High Leadership          12%   (O'Neil "N" — buy strength, not weakness)
    Entry Point Proximity        15%   (Minervini — never chase)
    Industry Group Leadership    15%   (Minervini + O'Neil "L")

O'Neil's CANSLIM "C" (current earnings), "A" (annual earnings), and "I"
(institutional sponsorship) still have no data source in this pipeline —
same honest gap as Minervini's fundamentals pillar — so they stay out
rather than being faked. "M" (market direction) is the existing market
condition overlay, applied to the final score for both frameworks.

Gate #2 — minimum market cap: stocks below `MIN_MARKET_CAP_CR` (₹1,000 Cr
by default) are excluded from ranking entirely, same "gate first, rank
survivors" philosophy as the Trend Template itself. A stock with unknown
(NaN) market cap is also excluded rather than assumed to pass — we don't
smuggle a stock through the floor just because its market-cap fetch failed
that day.

All of this is computed from data the project already collects and
archives daily (docs/<date>/full_results_<date>.csv) — no new data
source, no extra network calls. VCP, Volume/Supply-Demand, and New-High
Leadership pillars reconstruct a trailing multi-day series per symbol by
reading back through the last N daily archive snapshots.

Public entry point:

    rank_stocks(passing_df, all_gated_df, docs_dir, today_str,
                sentiment=None, nnh_stats=None, history_lookback_days=90,
                min_market_cap_cr=1000.0)
        -> pd.DataFrame (copy of passing_df, filtered to cap-eligible rows
                         + new score columns, sorted descending by
                         minervini_score)
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ── Tunable constants (spec-driven, not hardcoded magic numbers) ─────────────

PILLAR_WEIGHTS = {
    "rs":      0.20,
    "vcp":     0.20,
    "volume":  0.18,
    "newhigh": 0.12,
    "entry":   0.15,
    "group":   0.15,
    # "fundamentals": 0.00   # held at 0 until a fundamentals data source exists
}

MIN_MARKET_CAP_CR = 1000.0   # Gate #2 — below this, a stock isn't ranked at all

MARKET_MULTIPLIERS = {
    "confirmed_uptrend": 1.00,
    "under_pressure":    0.85,
    "correction":        0.65,
    "unknown":           1.00,   # no penalty if sentiment data is unavailable
}
NNH_BEARISH_MULTIPLIER = 0.90

GRADE_BANDS = [
    (85, "A+"),
    (70, "A"),
    (55, "B"),
    (40, "C"),
    (0,  "D"),
]

RS_TREND_LOOKBACK_DAYS   = 20   # ~1 trading month
VCP_MIN_HISTORY_ROWS     = 15   # below this, VCP score falls back to neutral
VCP_SWING_WINDOW         = 3    # ± days to confirm a local high/low
ENTRY_PIVOT_LOOKBACK     = 15   # trailing days (excluding today) used as pivot proxy
ACC_DIST_LOOKBACK_ROWS   = 20   # trailing rows scanned for accumulation/distribution
NEW_HIGH_RECENCY_LOOKBACK = 10  # trailing rows scanned for "how many days since a new 52w high"


# ── Small numeric helpers ──────────────────────────────────────────────────────

def _clamp(v: float, lo: float, hi: float) -> float:
    if pd.isna(v):
        return lo
    return max(lo, min(hi, v))


def _safe(v) -> bool:
    try:
        return v is not None and not (isinstance(v, float) and np.isnan(v))
    except Exception:
        return False


def _grade(score: float) -> str:
    for threshold, label in GRADE_BANDS:
        if score >= threshold:
            return label
    return "D"


# ── 1. Trailing per-symbol history reconstruction (from daily archive) ───────

_HIST_COLUMNS = [
    "date", "close", "52w_high", "rs_percentile", "12m_return_pct",
    "relative_volume", "volume_signal", "inside_bar", "is_52w_high",
]


def load_trailing_history(
    docs_dir: str | Path,
    symbols: set[str],
    today_str: str,
    lookback_days: int = 90,
) -> dict[str, pd.DataFrame]:
    """
    Reconstruct a trailing multi-day time series per symbol by reading back
    through the last `lookback_days` dated archive folders under `docs_dir`
    (docs/<YYYY-MM-DD>/full_results_<YYYYMMDD>.csv), which the daily scan
    already writes. Returns {symbol: DataFrame sorted ascending by date}.

    This does NOT include today's row — callers should append the current
    day's row themselves (it's already in memory as part of `passing`/`df`
    and doesn't need to be re-read from disk).
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

    per_symbol_frames: dict[str, list[pd.DataFrame]] = {s: [] for s in symbols}

    for dated_dir in dated_dirs:
        slug = dated_dir.name.replace("-", "")
        csv_path = dated_dir / f"full_results_{slug}.csv"
        if not csv_path.exists():
            continue
        try:
            cols_available = pd.read_csv(csv_path, nrows=0).columns
            usecols = [c for c in _HIST_COLUMNS + ["symbol"] if c in cols_available]
            day_df = pd.read_csv(csv_path, usecols=usecols)
        except Exception as exc:
            logger.debug("Could not read history file %s: %s", csv_path, exc)
            continue

        if "symbol" not in day_df.columns:
            continue
        day_df = day_df[day_df["symbol"].isin(symbols)]
        if day_df.empty:
            continue

        for sym, row in day_df.groupby("symbol"):
            per_symbol_frames.setdefault(sym, []).append(row)

    out: dict[str, pd.DataFrame] = {}
    for sym, frames in per_symbol_frames.items():
        if not frames:
            out[sym] = pd.DataFrame(columns=_HIST_COLUMNS)
            continue
        merged = pd.concat(frames, ignore_index=True)
        if "date" in merged.columns:
            merged = merged.sort_values("date").reset_index(drop=True)
        out[sym] = merged

    return out


def _append_today(hist: pd.DataFrame, today_row: pd.Series) -> pd.DataFrame:
    """Append the current day's values (already in memory) to a symbol's trailing history."""
    today_dict = {c: today_row.get(c, np.nan) for c in _HIST_COLUMNS}
    today_df = pd.DataFrame([today_dict])
    if hist.empty:
        return today_df
    return pd.concat([hist, today_df], ignore_index=True)


# ── 2. Pillar 1 — RS Quality (25%) ─────────────────────────────────────────────

def score_rs(hist: pd.DataFrame) -> float:
    """
    RS level (70%) + RS trend over ~20 trading days (20%) + RS-vs-price
    new-high sync bonus (10%). `hist` must include today's row as the last one.
    """
    if hist.empty:
        return 0.0
    today = hist.iloc[-1]
    rs_pct = today.get("rs_percentile", np.nan)
    if pd.isna(rs_pct):
        return 0.0

    level = _clamp((rs_pct - 70.0) / 30.0 * 100.0, 0, 100)

    trend = 0.0
    if len(hist) > RS_TREND_LOOKBACK_DAYS:
        past_rs = hist.iloc[-(RS_TREND_LOOKBACK_DAYS + 1)].get("rs_percentile", np.nan)
        if pd.notna(past_rs):
            trend = _clamp(rs_pct - past_rs, 0, 15) / 15.0 * 100.0

    sync = 0.0
    if "12m_return_pct" in hist.columns:
        window = hist["12m_return_pct"].tail(60).dropna()
        cur_ret = today.get("12m_return_pct", np.nan)
        if not window.empty and pd.notna(cur_ret) and window.max() > 0:
            near_own_high = cur_ret >= 0.95 * window.max()
            price_near_52w_high = bool(today.get("is_52w_high", False))
            if not price_near_52w_high and pd.notna(today.get("close")) and pd.notna(today.get("52w_high")):
                if today["52w_high"] > 0:
                    price_near_52w_high = today["close"] >= 0.95 * today["52w_high"]
            if near_own_high and price_near_52w_high:
                sync = 100.0

    return 0.7 * level + 0.2 * trend + 0.1 * sync


# ── 3. Pillar 2 — VCP Quality (25%) ────────────────────────────────────────────

def _find_swings(closes: np.ndarray, window: int = VCP_SWING_WINDOW) -> list[tuple[int, str]]:
    """
    Return a list of (index, 'high'|'low') for local extrema in a close-price
    series, using a simple ±window comparison. This is a close-price proxy
    for swing highs/lows (no intraday High/Low is archived daily), which is
    a reasonable approximation for base/contraction structure.
    """
    n = len(closes)
    swings: list[tuple[int, str]] = []
    for i in range(window, n - window):
        seg = closes[i - window: i + window + 1]
        if closes[i] == seg.max() and closes[i] != seg.min():
            swings.append((i, "high"))
        elif closes[i] == seg.min() and closes[i] != seg.max():
            swings.append((i, "low"))
    # collapse consecutive same-type swings, keeping the most extreme
    cleaned: list[tuple[int, str]] = []
    for idx, kind in swings:
        if cleaned and cleaned[-1][1] == kind:
            prev_idx, _ = cleaned[-1]
            if kind == "high" and closes[idx] >= closes[prev_idx]:
                cleaned[-1] = (idx, kind)
            elif kind == "low" and closes[idx] <= closes[prev_idx]:
                cleaned[-1] = (idx, kind)
            # else keep the existing one, skip idx
        else:
            cleaned.append((idx, kind))
    return cleaned


def score_vcp(hist: pd.DataFrame) -> tuple[float, dict]:
    """
    Contraction count (25%) + tightening ratio (25%) + base depth (20%) +
    volume dry-up (20%) + pivot-area tightness (10%).

    Returns (score, detail_dict) — detail_dict is useful for debugging /
    displaying *why* a stock scored the way it did.
    """
    detail = {"contractions": 0, "insufficient_history": False}

    if len(hist) < VCP_MIN_HISTORY_ROWS or "close" not in hist.columns:
        detail["insufficient_history"] = True
        return 50.0, detail   # neutral, not punitive — we simply don't know yet

    closes = hist["close"].to_numpy(dtype=float)
    swings = _find_swings(closes)

    # Extract high→low legs (each is one "contraction")
    contractions: list[float] = []
    for a, b in zip(swings, swings[1:]):
        (idx_a, kind_a), (idx_b, kind_b) = a, b
        if kind_a == "high" and kind_b == "low":
            high, low = closes[idx_a], closes[idx_b]
            if high > 0:
                contractions.append((high - low) / high * 100.0)

    detail["contractions"] = len(contractions)
    recent = contractions[-4:]   # most recent legs = the active base

    # -- Contraction count: triangular, peak at 3 --
    n = len(recent)
    if n == 0:
        count_score = 30.0   # no clean base detected yet
    else:
        count_score = max(0.0, 100.0 - abs(n - 3) * 25.0)

    # -- Tightening ratio: fraction of consecutive legs that got tighter --
    if len(recent) >= 2:
        tighter = sum(1 for x, y in zip(recent, recent[1:]) if y < x)
        tightening_score = tighter / (len(recent) - 1) * 100.0
    else:
        tightening_score = 50.0   # not enough legs to judge

    # -- Base depth: shallower is better (use the deepest recent leg) --
    if recent:
        depth = max(recent)
        if depth <= 15:
            depth_score = 100.0
        elif depth >= 35:
            depth_score = 0.0
        else:
            depth_score = 100.0 * (35 - depth) / (35 - 15)
    else:
        depth_score = 50.0

    # -- Volume dry-up: final leg's avg relative_volume vs. baseline 1.0 --
    volume_score = 50.0
    if "relative_volume" in hist.columns:
        tail_vol = hist["relative_volume"].tail(8).dropna()
        if not tail_vol.empty:
            avg_rel_vol = tail_vol.mean()
            if avg_rel_vol <= 0.5:
                volume_score = 100.0
            elif avg_rel_vol >= 1.0:
                volume_score = 0.0
            else:
                volume_score = 100.0 * (1.0 - avg_rel_vol) / 0.5

    # -- Pivot-area tightness: inside bar in the last 3 sessions --
    pivot_score = 0.0
    if "inside_bar" in hist.columns:
        recent_inside = hist["inside_bar"].tail(3)
        if recent_inside.astype(bool).any():
            pivot_score = 100.0

    score = (
        0.25 * count_score +
        0.25 * tightening_score +
        0.20 * depth_score +
        0.20 * volume_score +
        0.10 * pivot_score
    )
    detail.update({
        "count_score": round(count_score, 1),
        "tightening_score": round(tightening_score, 1),
        "depth_score": round(depth_score, 1),
        "volume_dryup_score": round(volume_score, 1),
        "pivot_tightness_score": round(pivot_score, 1),
        "recent_leg_depths_pct": [round(x, 1) for x in recent],
    })
    return score, detail


# ── 4. Pillar 3 — Volume / Supply-Demand (20%) ────────────────────────────────

def score_volume(hist: pd.DataFrame) -> float:
    """
    Pocket pivot recency (35%) + up/down "volume" ratio proxy (25%) +
    accumulation/distribution balance (25%) + relative volume on up days (15%).

    Note: only `relative_volume` (a normalized ratio, not absolute volume) is
    archived daily, so the up/down "volume" ratio and Acc/Dist counts are
    built from relative_volume weighted by daily price direction — a
    reasonable proxy given what's persisted, not literal share-volume totals.
    """
    if hist.empty or "close" not in hist.columns:
        return 0.0

    # -- Pocket pivot recency --
    pivot_score = 0.0
    if "volume_signal" in hist.columns:
        tail = hist["volume_signal"].tail(10).reset_index(drop=True)
        ppv_positions = [i for i, v in enumerate(tail) if v == "ppv"]
        if ppv_positions:
            days_ago = (len(tail) - 1) - ppv_positions[-1]
            pivot_score = max(0.0, 100.0 - days_ago * 10.0)

    # -- Up/down day classification for the remaining sub-scores --
    closes = hist["close"].to_numpy(dtype=float)
    rel_vol = hist["relative_volume"].to_numpy(dtype=float) if "relative_volume" in hist.columns else np.full(len(hist), np.nan)
    up_mask = np.zeros(len(hist), dtype=bool)
    down_mask = np.zeros(len(hist), dtype=bool)
    for i in range(1, len(hist)):
        if pd.notna(closes[i]) and pd.notna(closes[i - 1]):
            if closes[i] > closes[i - 1]:
                up_mask[i] = True
            elif closes[i] < closes[i - 1]:
                down_mask[i] = True

    updown_score = 50.0
    up_vol_sum = np.nansum(np.where(up_mask, rel_vol, 0.0))
    down_vol_sum = np.nansum(np.where(down_mask, rel_vol, 0.0))
    if down_vol_sum > 0:
        ratio = up_vol_sum / down_vol_sum
        updown_score = _clamp(ratio / 1.5 * 100.0, 0, 100)
    elif up_vol_sum > 0:
        updown_score = 100.0

    # -- Acc/Dist balance over trailing window --
    accdist_score = 50.0
    n_rows = len(hist)
    window_start = max(0, n_rows - ACC_DIST_LOOKBACK_ROWS)
    acc_days = sum(1 for i in range(window_start, n_rows) if up_mask[i] and pd.notna(rel_vol[i]) and rel_vol[i] > 1.0)
    dist_days = sum(1 for i in range(window_start, n_rows) if down_mask[i] and pd.notna(rel_vol[i]) and rel_vol[i] > 1.0)
    net = acc_days - dist_days
    accdist_score = _clamp((net + 10) / 20.0 * 100.0, 0, 100)

    # -- Relative volume specifically on up days --
    up_rel_vols = rel_vol[up_mask & ~np.isnan(rel_vol)]
    updays_relvol_score = 50.0
    if len(up_rel_vols) > 0:
        avg_up_relvol = up_rel_vols.mean()
        updays_relvol_score = _clamp(avg_up_relvol / 1.3 * 100.0, 0, 100)

    return (
        0.35 * pivot_score +
        0.25 * updown_score +
        0.25 * accdist_score +
        0.15 * updays_relvol_score
    )


# ── 5. Pillar 4 — Entry Point Proximity (15%) ─────────────────────────────────

def score_entry(hist: pd.DataFrame) -> tuple[float, str]:
    """
    Score how close a stock sits to its proper pivot (never chase).
    Pivot proxy = trailing ENTRY_PIVOT_LOOKBACK-day high, excluding today.
    Returns (score, entry_status) where entry_status is one of:
        "actionable", "watch — approaching pivot", "extended"
    """
    if hist.empty or "close" not in hist.columns or len(hist) < 2:
        return 50.0, "actionable"

    today_close = hist["close"].iloc[-1]
    prior = hist["close"].iloc[max(0, len(hist) - 1 - ENTRY_PIVOT_LOOKBACK):-1]
    if prior.empty or pd.isna(today_close):
        return 50.0, "actionable"

    pivot = prior.max()
    if pd.isna(pivot) or pivot <= 0:
        return 50.0, "actionable"

    extension_pct = (today_close - pivot) / pivot * 100.0

    if extension_pct <= 0:
        return 50.0, "watch — approaching pivot"
    if extension_pct <= 2:
        return 100.0, "actionable"
    if extension_pct <= 10:
        score = 100.0 - (extension_pct - 2) * 12.5
        return max(0.0, score), "actionable"
    return 0.0, "extended"


# ── 6. Pillar 5 — Industry Group Leadership (10–15%) ──────────────────────────

def score_new_high(hist: pd.DataFrame) -> float:
    """
    O'Neil's "N" — buy stocks making NEW highs, not stocks that look cheap
    because they're down. This is the one genuinely distinct CANSLIM signal
    not already covered by the RS/VCP/Entry pillars above:

      Recency (60%):  how many days since the stock last printed a new
                       52-week high (today counts as 0 days ago). Full marks
                       if it's today; decays to 0 over NEW_HIGH_RECENCY_LOOKBACK
                       trading days; 0 if no new high anywhere in the window.
      Proximity (40%): how close today's close is to its 52-week high.
                       Full marks at/through the high; scales down to 0 at the
                       Trend-Template gate's own floor (80% of the 52w high —
                       i.e. "within 25%"), so this pillar spans the entire
                       eligible band rather than clustering near 100 or 0.
    """
    if hist.empty or "close" not in hist.columns:
        return 0.0
    today = hist.iloc[-1]

    recency_score = 0.0
    if "is_52w_high" in hist.columns:
        tail = hist["is_52w_high"].tail(NEW_HIGH_RECENCY_LOOKBACK).reset_index(drop=True)
        hit_positions = [i for i, v in enumerate(tail) if bool(v)]
        if hit_positions:
            days_ago = (len(tail) - 1) - hit_positions[-1]
            recency_score = max(0.0, 100.0 - days_ago * (100.0 / NEW_HIGH_RECENCY_LOOKBACK))

    proximity_score = 0.0
    close = today.get("close", np.nan)
    high_52w = today.get("52w_high", np.nan)
    if pd.notna(close) and pd.notna(high_52w) and high_52w > 0:
        ratio = close / high_52w
        proximity_score = _clamp((ratio - 0.80) / 0.20 * 100.0, 0, 100)

    return 0.6 * recency_score + 0.4 * proximity_score


# ── 7. Pillar 6 — Industry Group Leadership (10–15%) ──────────────────────────

def score_groups(gated_df: pd.DataFrame) -> pd.Series:
    """
    Percentile-rank each stock's industry group by the group's median
    rs_percentile among all groups currently represented in `gated_df`
    (the full set of Stage-1-gated stocks today, not just the symbol
    being scored — group strength is relative to today's whole universe).
    Returns a Series indexed the same as gated_df with each row's group score.
    """
    if gated_df.empty or "industry_group" not in gated_df.columns or "rs_percentile" not in gated_df.columns:
        return pd.Series(50.0, index=gated_df.index)

    group_medians = gated_df.groupby("industry_group")["rs_percentile"].median()
    if group_medians.empty:
        return pd.Series(50.0, index=gated_df.index)

    group_pct_rank = group_medians.rank(pct=True) * 100.0
    return gated_df["industry_group"].map(group_pct_rank).fillna(50.0)


# ── 8. Market condition overlay ────────────────────────────────────────────────

def market_state_and_multiplier(sentiment: dict | None, nnh_stats: dict | None) -> tuple[str, float]:
    """
    Derive a market state label + score multiplier from the existing
    Market Sentiment (NIFTY Smallcap 250 vs EMA10/EMA20) and Net New Highs
    bias, both already computed elsewhere in the pipeline.
    """
    if not sentiment:
        return "unknown", MARKET_MULTIPLIERS["unknown"]

    idx = sentiment.get("cnxsmallcap") or sentiment.get("niftysmlcap250") or {}
    above_10 = idx.get("above_ema10")
    above_20 = idx.get("above_ema20")

    if above_10 is None or above_20 is None:
        state = "unknown"
    elif above_10 and above_20:
        state = "confirmed_uptrend"
    elif above_20:
        state = "under_pressure"
    else:
        state = "correction"

    multiplier = MARKET_MULTIPLIERS.get(state, 1.0)

    if nnh_stats and nnh_stats.get("bias") == "bearish":
        multiplier *= NNH_BEARISH_MULTIPLIER

    return state, round(multiplier, 4)


# ── 9. Public entry point ──────────────────────────────────────────────────────

def rank_stocks(
    passing_df: pd.DataFrame,
    gated_df: pd.DataFrame | None,
    docs_dir: str | Path,
    today_str: str,
    sentiment: dict | None = None,
    nnh_stats: dict | None = None,
    history_lookback_days: int = 90,
    min_market_cap_cr: float = MIN_MARKET_CAP_CR,
) -> pd.DataFrame:
    """
    Rank Stage-1-gated stocks (`passing_df`) using the six weighted
    technical pillars + market overlay described in the ranking spec.

    Applies a second gate first: stocks with `total_market_cap_cr` below
    `min_market_cap_cr` (or missing/unknown market cap) are excluded from
    ranking entirely — never scored, never in the output — same "gate
    first, rank survivors" philosophy as the Trend Template itself.

    Parameters
    ----------
    passing_df : the stocks to gate-check and (if eligible) score & return.
    gated_df   : the full set of Stage-1-gated stocks for the day, used to
                 compute relative Industry Group Leadership. Pass the same
                 DataFrame as `passing_df` if you don't have a broader set.
    docs_dir   : project's docs/ root (dated archive folders live here).
    today_str  : today's date as YYYYMMDD (matches the archive folder naming).
    sentiment  : optional dict from indicators.get_market_sentiment().
    nnh_stats  : optional dict from net_new_highs.run()/compute_stats().
    min_market_cap_cr : minimum market cap (₹ Crore) to be eligible for
                 ranking. Default ₹1,000 Cr.

    Returns a copy of `passing_df` (filtered to cap-eligible rows only)
    with new columns:
        rs_score, vcp_score, volume_score, newhigh_score, entry_score,
        group_score, entry_status, minervini_score, grade, market_state,
        market_multiplier
    sorted descending by minervini_score.
    """
    if passing_df is None or passing_df.empty:
        return passing_df

    # ── Gate #2 — market cap floor (fail-closed on missing data) ──────────────
    if "total_market_cap_cr" in passing_df.columns:
        cap = pd.to_numeric(passing_df["total_market_cap_cr"], errors="coerce")
        eligible_mask = cap >= min_market_cap_cr
        n_below_cap = int(((cap < min_market_cap_cr) & cap.notna()).sum())
        n_unknown_cap = int(cap.isna().sum())
        if n_below_cap or n_unknown_cap:
            logger.info(
                "Market-cap gate (≥ ₹%.0f Cr): %d/%d eligible — excluded %d below cap, %d with unknown/missing cap",
                min_market_cap_cr, int(eligible_mask.sum()), len(passing_df), n_below_cap, n_unknown_cap,
            )
        passing_df = passing_df[eligible_mask].copy()
    else:
        logger.warning(
            "No 'total_market_cap_cr' column present — cannot apply the ₹%.0f Cr market-cap "
            "gate, so no stocks were ranked this run.", min_market_cap_cr,
        )
        passing_df = passing_df.iloc[0:0]

    if passing_df.empty:
        return passing_df

    gated_df = gated_df if gated_df is not None and not gated_df.empty else passing_df

    symbols = set(passing_df["symbol"].dropna().unique())
    history_map = load_trailing_history(docs_dir, symbols, today_str, history_lookback_days)

    group_scores = score_groups(gated_df)
    # Align group scores onto passing_df (gated_df may be a superset)
    group_score_by_symbol = dict(zip(gated_df["symbol"], group_scores))

    market_state, market_multiplier = market_state_and_multiplier(sentiment, nnh_stats)

    rs_scores, vcp_scores, volume_scores, newhigh_scores, entry_scores = [], [], [], [], []
    entry_statuses, vcp_details = [], []

    for _, row in passing_df.iterrows():
        sym = row.get("symbol")
        hist = history_map.get(sym, pd.DataFrame(columns=_HIST_COLUMNS))
        hist = _append_today(hist, row)

        rs = score_rs(hist)
        vcp, vcp_detail = score_vcp(hist)
        vol = score_volume(hist)
        newhigh = score_new_high(hist)
        entry, entry_status = score_entry(hist)

        rs_scores.append(round(rs, 1))
        vcp_scores.append(round(vcp, 1))
        volume_scores.append(round(vol, 1))
        newhigh_scores.append(round(newhigh, 1))
        entry_scores.append(round(entry, 1))
        entry_statuses.append(entry_status)
        vcp_details.append(vcp_detail)

    out = passing_df.copy()
    out["rs_score"]      = rs_scores
    out["vcp_score"]     = vcp_scores
    out["volume_score"]  = volume_scores
    out["newhigh_score"] = newhigh_scores
    out["entry_score"]   = entry_scores
    out["group_score"]   = out["symbol"].map(group_score_by_symbol).fillna(50.0)
    out["entry_status"]  = entry_statuses

    composite = (
        PILLAR_WEIGHTS["rs"]      * out["rs_score"] +
        PILLAR_WEIGHTS["vcp"]     * out["vcp_score"] +
        PILLAR_WEIGHTS["volume"]  * out["volume_score"] +
        PILLAR_WEIGHTS["newhigh"] * out["newhigh_score"] +
        PILLAR_WEIGHTS["entry"]   * out["entry_score"] +
        PILLAR_WEIGHTS["group"]   * out["group_score"]
    )

    out["market_state"]      = market_state
    out["market_multiplier"] = market_multiplier
    out["minervini_score"]   = (composite * market_multiplier).round(1)
    out["grade"]             = out["minervini_score"].apply(_grade)

    # Tie-break: liquidity, then VCP tightness, then entry proximity
    sort_cols = ["minervini_score"]
    ascending = [False]
    if "traded_value_cr" in out.columns and "total_market_cap_cr" in out.columns:
        with np.errstate(divide="ignore", invalid="ignore"):
            out["_liquidity_ratio"] = out["traded_value_cr"] / out["total_market_cap_cr"]
        sort_cols.append("_liquidity_ratio")
        ascending.append(False)
    sort_cols += ["vcp_score", "entry_score"]
    ascending += [False, False]

    out = out.sort_values(sort_cols, ascending=ascending).drop(columns=["_liquidity_ratio"], errors="ignore")
    out = out.reset_index(drop=True)
    out.insert(0, "rank", range(1, len(out) + 1))

    logger.info(
        "Ranked %d stocks — market_state=%s (×%.2f) — top: %s",
        len(out), market_state, market_multiplier,
        out.iloc[0]["symbol"] if len(out) else "n/a",
    )

    return out
