"""
scanner/nse_client.py
---------------------
Handles fetching market-cap, free-float, traded-value, and price-band data.

Primary source for market-cap/traded-value: yfinance fast_info (with .info
as a secondary fallback). Yahoo Finance is frequently rate-limited or
blocked from shared/CI IPs (GitHub Actions runners in particular), which
previously showed up as every stock's market-cap / traded-value coming
back "N/A" with no retry and no alternate source. This version adds:

  1. A short retry-with-backoff around the yfinance fetch, since most
     Yahoo blocks are transient (a 429/999 that succeeds a few seconds
     later), rather than permanent.
  2. A same-day fallback to NSE India's own quote API (via `jugaad_data`,
     already a project dependency) when yfinance still comes back empty.
     This uses NSE's official `NSE_HEADERS`/`NSE_REQUEST_DELAY` config
     (previously defined but unused) and is inherently more reliable for
     NSE-listed symbols since it's the exchange's own data, not a
     third-party scrape.

Price band (the circuit filter %, e.g. "5%", "10%", "20%", "No Band") is
NSE-exclusive data — yfinance has no equivalent field at all — so it's
always fetched from NSE's own quote-equity API (the same one
https://www.nseindia.com/get-quotes/equity?symbol=... uses), regardless of
whether yfinance already answered the market-cap questions. To avoid
doubling NSE requests, the market-cap NSE-fallback path and the price-band
fetch share a single NSE quote call per symbol whenever both are needed in
the same run.

Both paths are independent and defensive — if one fails/errors for any
reason, the other still has a chance to fill in the numbers, and if both
fail the columns are NaN/"—" (rendered as "N/A"/"—" in the dashboards)
exactly as before, so this is purely additive resilience with no new hard
dependency on either source being up.
"""

from __future__ import annotations

import logging
import random
import time

import numpy as np
import pandas as pd
import yfinance as yf

from .config import NSE_REQUEST_DELAY, DATA_DIR

logger = logging.getLogger(__name__)

_EMPTY = {
    "total_market_cap_cr":  np.nan,
    "traded_value_cr":      np.nan,
    "traded_volume":        np.nan,
    "traded_val_pct_mc":    np.nan,
    "price_band":           "—",
}

_CR = 10_000_000.0   # 1 Crore = 10,000,000
_PRICE_BAND_EMPTY = "—"

# ── NSE fallback client (lazy singleton — only created if actually needed) ────
_nse_live = None


def _get_nse_live():
    """Lazily create (and cache) a single jugaad_data NSELive client."""
    global _nse_live
    if _nse_live is None:
        try:
            from jugaad_data.nse import NSELive
            _nse_live = NSELive()
        except Exception as exc:
            logger.warning("Could not initialize NSE fallback client: %s", exc)
            _nse_live = False   # sentinel: don't keep retrying construction
    return _nse_live or None


def _nse_quote_raw(symbol_ns: str) -> dict | None:
    """
    Single raw NSE quote-equity fetch, shared by the market-cap NSE-fallback
    path and the price-band fetch so a symbol needing both only costs one
    NSE request, not two.
    """
    nse = _get_nse_live()
    if nse is None:
        return None
    sym = symbol_ns.replace(".NS", "").strip().upper()
    try:
        q = nse.stock_quote(sym)
        return q if isinstance(q, dict) else None
    except Exception as exc:
        logger.debug("NSE quote fetch failed for %s: %s", sym, exc)
        return None


def _extract_price_band(quote: dict) -> str:
    """
    Pull the circuit price band out of an NSE quote-equity response, e.g.
    "5%", "10%", "20%", or "No Band" for band-exempt stocks (usually
    F&O-eligible large caps). Normalized to a clean display string.
    """
    try:
        price_info = quote.get("priceInfo", {}) or {}
        band = price_info.get("pPriceBand")
        if not band:
            return _PRICE_BAND_EMPTY
        band = str(band).strip()
        if band.lower() in ("no band", "none", ""):
            return "No Band"
        return band
    except Exception:
        return _PRICE_BAND_EMPTY


def _rnd(v, n=2):
    try:
        f = float(v)
        return round(f, n) if not np.isnan(f) else np.nan
    except Exception:
        return np.nan


def _is_complete(caps: dict) -> bool:
    """True if we already have both market cap and traded value."""
    mc = caps.get("total_market_cap_cr")
    tv = caps.get("traded_value_cr")
    return (mc is not None and not (isinstance(mc, float) and np.isnan(mc))) and \
           (tv is not None and not (isinstance(tv, float) and np.isnan(tv)))


# ── 1. yfinance (primary) ──────────────────────────────────────────────────────

def _fetch_from_yfinance(symbol_ns: str, attempts: int = 2) -> dict:
    """
    Fetch market-cap and traded-value for a single NSE symbol via yfinance.

    Strategy (most-reliable → least):
      1. yf.Ticker.fast_info  — lightweight, rarely blocked
      2. yf.Ticker.info       — full dict, occasionally rate-limited
      3. Last-resort: derive price × shares from history

    Retries up to `attempts` times with a short randomized backoff, since
    Yahoo rate-limit/blocks (HTTP 429/999) are usually transient.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            ticker = yf.Ticker(symbol_ns)

            # ── fast_info (preferred) ──────────────────────────────────────
            fi = ticker.fast_info
            market_cap_raw = getattr(fi, "market_cap", None)
            current_price  = getattr(fi, "last_price", None) or getattr(fi, "regular_market_price", None)
            volume         = getattr(fi, "three_month_average_volume", None)  # fallback
            day_volume     = getattr(fi, "regular_market_volume", None) or getattr(fi, "volume", None)
            if day_volume:
                volume = day_volume
            shares_outstanding = getattr(fi, "shares", None)

            # ── fall back to info dict if fast_info is sparse ──────────────
            if not market_cap_raw or not current_price:
                try:
                    info = ticker.info
                    market_cap_raw = market_cap_raw or info.get("marketCap")
                    current_price  = current_price  or info.get("currentPrice") or info.get("regularMarketPrice")
                    volume         = volume         or info.get("volume") or info.get("regularMarketVolume")
                    shares_outstanding = shares_outstanding or info.get("sharesOutstanding")
                except Exception as info_exc:
                    logger.debug("info fallback failed for %s: %s", symbol_ns, info_exc)

            # ── last resort: derive from recent history ────────────────────
            if not market_cap_raw or not current_price:
                try:
                    hist = ticker.history(period="5d", auto_adjust=True)
                    if not hist.empty:
                        current_price = current_price or float(hist["Close"].iloc[-1])
                        volume        = volume        or int(hist["Volume"].iloc[-1])
                except Exception:
                    pass

            total_mc_cr = (float(market_cap_raw) / _CR) if market_cap_raw else np.nan

            traded_value_cr = np.nan
            if volume and current_price:
                traded_value_cr = (float(volume) * float(current_price)) / _CR

            traded_val_pct_mc = np.nan
            if not np.isnan(traded_value_cr) and not np.isnan(total_mc_cr) and total_mc_cr > 0:
                traded_val_pct_mc = (traded_value_cr / total_mc_cr) * 100.0

            result = {
                "total_market_cap_cr": _rnd(total_mc_cr),
                "traded_value_cr":     _rnd(traded_value_cr),
                "traded_volume":       int(volume) if volume else np.nan,
                "traded_val_pct_mc":   _rnd(traded_val_pct_mc, 4),
            }

            if _is_complete(result) or attempt == attempts:
                return result

            # Incomplete but attempts remain — likely a transient block; retry.
            logger.debug("yfinance incomplete for %s (attempt %d/%d) — retrying",
                         symbol_ns, attempt, attempts)

        except Exception as exc:
            last_exc = exc
            logger.debug("yfinance fetch error for %s (attempt %d/%d): %s",
                         symbol_ns, attempt, attempts, exc)

        if attempt < attempts:
            time.sleep(0.6 * attempt + random.uniform(0, 0.4))   # short randomized backoff

    if last_exc:
        logger.warning("yfinance fetch failed for %s after %d attempts: %s", symbol_ns, attempts, last_exc)
    return _EMPTY.copy()


# ── 2. NSE India quote API (fallback for market cap, primary for price band) ──

def _fetch_from_nse(symbol_ns: str, quote: dict | None = None) -> dict:
    """
    Fetch market-cap, traded-value, and price-band straight from NSE
    India's own quote-equity API (via jugaad_data's NSELive), which
    mirrors what https://www.nseindia.com/get-quotes/equity?symbol=...
    shows.

    Market cap   = last traded price × issued share capital.
    Traded value = NSE's own reported total traded value for the day
                   (no need to derive it — NSE publishes it directly).
    Price band   = NSE's circuit filter %, straight from the same response.

    Pass an already-fetched `quote` dict (from `_nse_quote_raw`) to avoid
    making a second NSE request for a symbol that needs both market-cap
    fallback and price band.
    """
    q = quote if quote is not None else _nse_quote_raw(symbol_ns)
    if q is None:
        return _EMPTY.copy()

    try:
        price_info    = q.get("priceInfo", {}) or {}
        security_info = q.get("securityInfo", {}) or {}
        trade_info    = ((q.get("marketDeptOrderBook", {}) or {}).get("tradeInfo", {})) or {}

        last_price   = price_info.get("lastPrice")
        issued_size  = security_info.get("issuedSize")
        total_value  = trade_info.get("totalTradedValue")    # NSE reports this in ₹ Lakhs
        total_volume = trade_info.get("totalTradedVolume")

        total_mc_cr = np.nan
        if last_price and issued_size:
            total_mc_cr = (float(last_price) * float(issued_size)) / _CR

        traded_value_cr = np.nan
        if total_value:
            # NSE reports totalTradedValue in ₹ Lakhs — convert Lakhs → Crores.
            traded_value_cr = float(total_value) / 100.0

        traded_val_pct_mc = np.nan
        if not np.isnan(traded_value_cr) and not np.isnan(total_mc_cr) and total_mc_cr > 0:
            traded_val_pct_mc = (traded_value_cr / total_mc_cr) * 100.0

        return {
            "total_market_cap_cr": _rnd(total_mc_cr),
            "traded_value_cr":     _rnd(traded_value_cr),
            "traded_volume":       int(total_volume) if total_volume else np.nan,
            "traded_val_pct_mc":   _rnd(traded_val_pct_mc, 4),
            "price_band":          _extract_price_band(q),
        }

    except Exception as exc:
        sym = symbol_ns.replace(".NS", "").strip().upper()
        logger.debug("NSE quote parsing failed for %s: %s", sym, exc)
        return _EMPTY.copy()


# ── Public API ──────────────────────────────────────────────────────────────────

def fetch_market_cap(symbol_ns: str) -> dict:
    """
    Fetch market-cap, traded-value, and price-band for a single NSE symbol.

    Market cap / traded value: yfinance first (with retries); NSE fills in
    anything still missing.
    Price band: NSE-exclusive — always fetched from NSE regardless of how
    the market-cap fields turned out, sharing a single NSE quote call with
    the fallback above whenever both are needed.
    """
    result = _fetch_from_yfinance(symbol_ns)

    # We need an NSE hit if market cap is still incomplete, OR simply to
    # get the price band (which yfinance can never provide) — so this now
    # always makes exactly one NSE request per symbol.
    quote = _nse_quote_raw(symbol_ns)
    nse_result = _fetch_from_nse(symbol_ns, quote=quote)

    for key, val in nse_result.items():
        existing = result.get(key)
        existing_missing = existing is None or (isinstance(existing, float) and np.isnan(existing))
        if existing_missing and val is not None and not (isinstance(val, float) and np.isnan(val)):
            result[key] = val

    result.setdefault("price_band", _PRICE_BAND_EMPTY)
    time.sleep(NSE_REQUEST_DELAY)   # be polite to NSE's servers — we now always hit them once

    return result


def enrich_with_market_caps(passing_df: pd.DataFrame) -> pd.DataFrame:
    """
    Add market-cap / liquidity / price-band columns to *passing_df*.
    Iterates symbols using yfinance for market-cap/traded-value (falling
    back to NSE's own API per symbol when needed), and always queries NSE
    once per symbol for the price band (circuit filter %), since that data
    is NSE-exclusive. Returns an enriched copy.
    """
    if passing_df.empty:
        return passing_df

    logger.info("Fetching market-cap and price-band data for %d stocks…", len(passing_df))

    cols: dict[str, list] = {
        "total_market_cap_cr": [],
        "traded_value_cr":     [],
        "traded_volume":       [],
        "traded_val_pct_mc":   [],
        "price_band":          [],
    }

    for i, sym in enumerate(passing_df["symbol"], start=1):
        caps = fetch_market_cap(sym)
        if _is_complete(caps):
            logger.info("  [%d/%d] %s ✓ (band: %s)", i, len(passing_df), sym, caps.get("price_band", "—"))
        else:
            logger.debug("  [%d/%d] %s — still incomplete after all sources", i, len(passing_df), sym)
        for key in cols:
            cols[key].append(caps.get(key, np.nan if key != "price_band" else _PRICE_BAND_EMPTY))
        time.sleep(0.3)   # polite delay to avoid rate-limiting

    out = passing_df.copy()
    for key, values in cols.items():
        out[key] = values

    n_missing = out["total_market_cap_cr"].isna().sum()
    if n_missing:
        logger.warning(
            "%d/%d stocks still missing market-cap data after yfinance + NSE fallback "
            "(both sources may be temporarily unreachable from this network).",
            n_missing, len(out),
        )

    n_missing_band = (out["price_band"] == _PRICE_BAND_EMPTY).sum()
    if n_missing_band:
        logger.warning(
            "%d/%d stocks still missing price-band data (NSE may be temporarily unreachable).",
            n_missing_band, len(out),
        )

    return out


# ── Persistent market-cap cache ────────────────────────────────────────────────
#
# enrich_with_market_caps() above is fine for small, already-curated sets
# (like the Stage 1 Trend Template `passing` stocks — typically tens of
# names). It becomes a serious bottleneck for any dashboard that might
# match hundreds of stocks (e.g. Stage 4 Breakdown, which just checks
# "below MA50" across the full universe and can match 500-1000+ stocks on
# a weak market day) — fetching market cap fresh for every one of them,
# every single day, is what previously blew up a ~10-15 min daily workflow
# into ~1 hour.
#
# Market cap barely moves day to day for an already-large stock (shares
# outstanding is stable; price moves a few percent at most), so there's no
# need to re-fetch it daily. This cache stores each symbol's last-known
# market cap + price band on disk, and only re-fetches symbols that are
# missing or older than `max_age_days`. After the first run repopulates
# the cache, day-to-day runs only pay the network cost for genuinely new
# or stale symbols — typically a small fraction of the candidate set.
#
# IMPORTANT: only `total_market_cap_cr` and `price_band` are safe to reuse
# across days this way — both change slowly. `traded_value_cr`,
# `traded_volume`, and `traded_val_pct_mc` are point-in-time figures for
# THAT specific trading day, so they are intentionally NOT served from
# this cache (always NaN from the cached path) — any caller that needs
# today's actual traded value/volume should use enrich_with_market_caps()
# directly, not this cached wrapper.

CACHE_PATH = DATA_DIR / "market_cap_cache.csv"
CACHE_MAX_AGE_DAYS = 14  # market cap / price band are slow-changing — safe to reuse for ~2 weeks

_CACHE_COLUMNS = ["symbol", "total_market_cap_cr", "price_band", "last_updated"]


def _load_cache() -> pd.DataFrame:
    if CACHE_PATH.exists():
        try:
            cache = pd.read_csv(CACHE_PATH)
            cache["last_updated"] = pd.to_datetime(cache["last_updated"])
            return cache
        except Exception as exc:
            logger.warning("Could not read market-cap cache (%s) — starting fresh.", exc)
    empty = pd.DataFrame(columns=_CACHE_COLUMNS)
    empty["last_updated"] = pd.to_datetime(empty["last_updated"])
    return empty


def _save_cache(cache: pd.DataFrame) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    cache.to_csv(CACHE_PATH, index=False)


def enrich_with_market_caps_cached(
    df: pd.DataFrame,
    max_age_days: float = CACHE_MAX_AGE_DAYS,
) -> pd.DataFrame:
    """
    Like enrich_with_market_caps(), but backed by a persistent on-disk
    cache (data/market_cap_cache.csv). Only symbols missing from the
    cache, or whose cached value is older than `max_age_days`, are
    actually fetched over the network this run — everyone else reuses
    their last-known market cap / price band.

    Adds `total_market_cap_cr` and `price_band` columns only (NOT
    traded_value_cr/traded_volume/traded_val_pct_mc — see module notes
    above for why). Use enrich_with_market_caps() directly if you need
    today's actual traded value/volume, or need a fully fresh fetch for a
    small, already-curated set of stocks.
    """
    if df is None or df.empty:
        return df

    cache = _load_cache()
    now = pd.Timestamp.now().normalize()

    symbols_needed = set(df["symbol"].dropna().unique())
    cached_symbols = set(cache["symbol"]) if not cache.empty else set()

    stale_or_missing = []
    cache_by_symbol = cache.set_index("symbol") if not cache.empty else cache
    for sym in symbols_needed:
        if sym not in cached_symbols:
            stale_or_missing.append(sym)
            continue
        last_updated = cache_by_symbol.loc[sym, "last_updated"]
        if pd.isna(last_updated) or (now - last_updated).days >= max_age_days:
            stale_or_missing.append(sym)

    n_reused = len(symbols_needed) - len(stale_or_missing)
    logger.info(
        "Market-cap cache: %d/%d symbols need a fresh fetch (missing or older than %.0f days) — %d reused from cache.",
        len(stale_or_missing), len(symbols_needed), max_age_days, n_reused,
    )

    if stale_or_missing:
        to_fetch = df[df["symbol"].isin(stale_or_missing)]
        freshly_enriched = enrich_with_market_caps(to_fetch)
        fresh_rows = freshly_enriched[["symbol", "total_market_cap_cr", "price_band"]].copy()
        fresh_rows["last_updated"] = now

        cache = cache[~cache["symbol"].isin(stale_or_missing)]
        cache = pd.concat([cache, fresh_rows], ignore_index=True)
        _save_cache(cache)

    cache_lookup = cache.set_index("symbol")
    out = df.copy()
    out["total_market_cap_cr"] = out["symbol"].map(cache_lookup.get("total_market_cap_cr", pd.Series(dtype=float)))
    out["price_band"] = out["symbol"].map(cache_lookup.get("price_band", pd.Series(dtype=object))).fillna(_PRICE_BAND_EMPTY)

    return out


def overlay_price_band_from_cache(df: pd.DataFrame) -> pd.DataFrame:
    """
    Overwrite `price_band` in *df* using whatever is currently in
    data/market_cap_cache.csv, for any symbol present there — without
    touching total_market_cap_cr, traded_value_cr, traded_volume, or
    traded_val_pct_mc.

    Intended to run AFTER enrich_with_market_caps(), so a daily manual
    upload (via scripts/update_price_band_cache.py) takes priority over
    whatever price_band the live NSE fetch happened to return, while
    still keeping the live traded-value/volume figures from that call.

    Symbols not present in the cache are left with whatever price_band
    they already had (e.g. from the live fetch, or "—" if that was empty
    too).
    """
    if df is None or df.empty or "price_band" not in df.columns:
        return df

    cache = _load_cache()
    if cache.empty:
        return df

    band_lookup = cache.set_index("symbol")["price_band"]
    out = df.copy()
    overlay = out["symbol"].map(band_lookup)
    out["price_band"] = overlay.fillna(out["price_band"])
    return out
