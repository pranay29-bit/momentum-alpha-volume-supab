"""
scanner/dashboard.py
--------------------
Generates self-contained HTML dashboards from scan results.

Public entry-points:
  • build_passing_dashboard        – all 8-condition passing stocks
  • build_passing_ema10_dashboard  – passing AND above EMA10 (elite view)
  • build_volume_action_dashboard  – pocket pivot / volume action
  • build_rocket_dashboard         – passing + inside bar
  • build_main_index               – GitHub Pages landing page

New-stock tracking:
  Each dashboard accepts `known_symbols: set[str]` — symbols that appeared
  in ANY scan in the past 10 days. Symbols NOT in that set are marked ✦ NEW
  in the dashboard with a distinct highlight.
"""

from __future__ import annotations

import html
import json
import logging
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import fmt_cr

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe(v) -> bool:
    try:
        return not np.isnan(float(v))
    except Exception:
        return False


def _r(v, n=2):
    try:
        f = float(v)
        return str(round(f, n)) if not np.isnan(f) else "null"
    except Exception:
        return "null"


def _strip_symbol(symbol_ns: str) -> str:
    """Strip the .NS or .BO Yahoo-suffix from a symbol, leaving the raw code."""
    s = str(symbol_ns).strip()
    if s.endswith(".NS"):
        return s[: -len(".NS")]
    if s.endswith(".BO"):
        return s[: -len(".BO")]
    return s


def _display_symbol(symbol_ns: str, name: str | None = None) -> str:
    """
    Human-readable label for a stock row. NSE-style alpha tickers (e.g.
    "JALAN") are shown as-is. Bare-numeric BSE scrip codes (e.g. "543286",
    used by many BSE SME listings that have no alpha ticker) are not
    meaningful to read, so fall back to the company name when one is
    available (from SME_Stocks.csv's "Name" column, merged in as `name`).
    """
    raw = _strip_symbol(symbol_ns)
    if raw.isdigit() and name and str(name).strip() and str(name).strip().lower() != "nan":
        return str(name).strip()
    return raw


def _tv_link(symbol_ns: str) -> str:
    """
    Build a TradingView chart link for a symbol. NSE-suffixed (.NS) symbols
    link to NSE:<SYM>; BSE-suffixed (.BO) symbols — used for BSE SME
    listings, which are commonly bare numeric scrip codes — link to
    BSE:<CODE> instead, since TradingView doesn't recognize BSE codes under
    the NSE: prefix.
    """
    s = str(symbol_ns).strip()
    if s.endswith(".BO"):
        sym = s[: -len(".BO")]
        return f"https://www.tradingview.com/chart/?symbol=BSE%3A{sym}"
    sym = s.replace(".NS", "").strip()
    return f"https://www.tradingview.com/chart/?symbol=NSE%3A{sym}"


# ── Shared assets ─────────────────────────────────────────────────────────────

_CDN_CHARTJS  = "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"
_GOOGLE_FONTS = (
    "https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;500;600;700"
    "&family=DM+Mono:wght@400;500&display=swap"
)

# ─── Design tokens ────────────────────────────────────────────────────────────
# Light-mode-first. Clean white with warm off-white accents.
# Accent palette: indigo (passing), emerald (elite), blue (volume), amber (rocket).

_BASE_CSS = """
:root {
  /* Surfaces */
  --bg:         #ffffff;
  --surface:    #ffffff;
  --surface-2:  #fbfbfe;
  --surface2:   #fbfbfe;
  --surface3:   #f1f3f9;
  --border:     #e5e8f0;
  --border-2:   #d4d9e8;
  --border2:    #d4d9e8;

  /* Text */
  --text:       #0d1426;
  --muted:      #5b6178;
  --subtle:     #9499b3;

  /* Brand accents */
  --navy:       #0f1b3d;
  --navy-2:     #16234a;
  --navy-lt:    #eef1f8;
  --navy-mid:   #c9d0e3;
  --indigo:     #4f46e5;
  --indigo-lt:  #eef0fd;
  --indigo-mid: #c7d2fe;
  --emerald:    #059669;
  --emerald-lt: #ecfdf5;
  --emerald-mid:#a7f3d0;
  --blue:       #2563eb;
  --blue-lt:    #eff6ff;
  --blue-mid:   #bfdbfe;
  --amber:      #b45309;
  --amber-lt:   #fffbeb;
  --amber-mid:  #fde68a;
  --red:        #dc2626;
  --red-lt:     #fef2f2;
  --red-mid:    #fca5a5;
  --violet:     #7c3aed;
  --violet-lt:  #f5f3ff;
  --violet-mid: #ddd6fe;
  --rose:       #e11d48;
  --rose-lt:    #fff1f2;
  --rose-mid:   #fecdd3;
  --slate:      #475569;
  --slate-lt:   #f1f5f9;
  --slate-mid:  #cbd5e1;

  /* NEW badge */
  --new-bg:     #fdf4ff;
  --new-border: #d946ef;
  --new-text:   #a21caf;
  --new-row:    #fdf4ff;

  /* Type — identical stack used site-wide (homepage, all dashboards, tools) */
  --sans:  'Outfit', system-ui, -apple-system, sans-serif;
  --mono:  'DM Mono', 'SF Mono', 'Courier New', monospace;

  /* Radii */
  --r:   8px;
  --rl:  12px;
  --radius:    12px;
  --radius-sm: 8px;
  --shadow-sm: 0 1px 2px rgba(15,23,42,.04);
  --shadow-md: 0 4px 16px -4px rgba(15,23,42,.08), 0 1px 3px rgba(15,23,42,.04);
  --rxl: 16px;
}

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html { font-size: 14px; }
body {
  background: var(--bg);
  color: var(--text);
  font-family: var(--sans);
  line-height: 1.6;
  min-height: 100vh;
}
a { color: inherit; text-decoration: none; }

/* ── Topbar ── */
.topbar {
  height: 3px;
  background: linear-gradient(90deg, var(--ACCENT1) 0%, var(--ACCENT2) 100%);
}

/* ── Header ── */
header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 1.6rem 2.5rem;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
  flex-wrap: wrap;
}
.hdr-left { display: flex; flex-direction: column; gap: .2rem; }
.brand {
  display: flex;
  align-items: center;
  gap: .5rem;
  margin-bottom: .25rem;
}
.brand-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
}
.brand-name {
  font-family: var(--mono);
  font-size: .65rem;
  font-weight: 500;
  letter-spacing: .14em;
  text-transform: uppercase;
  color: var(--muted);
}
header h1 {
  font-size: clamp(1.35rem, 2.5vw, 1.9rem);
  font-weight: 700;
  letter-spacing: -.03em;
  line-height: 1.1;
  color: var(--text);
}
.hdr-sub {
  font-size: .8rem;
  color: var(--muted);
  margin-top: .15rem;
}
.badge-row { display: flex; gap: .45rem; margin-top: .5rem; flex-wrap: wrap; }

/* ── Shared cross-page nav bar (identical component on every page) ── */
.site-nav {
  display: flex; flex-wrap: wrap; gap: .5rem; align-items: center;
  padding: .75rem 2.5rem;
  background: color-mix(in srgb, var(--surface-2) 88%, transparent);
  backdrop-filter: blur(10px) saturate(160%);
  -webkit-backdrop-filter: blur(10px) saturate(160%);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
  box-shadow: 0 6px 18px -10px rgba(15,23,42,.18);
}
.btn-link {
  display: inline-flex; align-items: center; gap: .35rem;
  padding: .32rem .9rem; border-radius: 999px;
  font-family: var(--mono); font-size: .72rem; font-weight: 600;
  background: var(--indigo-lt); border: 1px solid var(--indigo-mid); color: var(--indigo);
  text-decoration: none; transition: background .14s, box-shadow .14s, transform .14s; letter-spacing: .03em;
}
.btn-link:hover { transform: translateY(-1px); box-shadow: 0 3px 8px -3px rgba(15,23,42,.18); }
.btn-link:hover { background: #dde2fb; }
.btn-link.green   { background: var(--emerald-lt); border-color: var(--emerald-mid); color: var(--emerald); }
.btn-link.green:hover   { background: #d7f8ea; }
.btn-link.blue    { background: var(--blue-lt);    border-color: var(--blue-mid);    color: var(--blue); }
.btn-link.blue:hover    { background: #dee9fd; }
.btn-link.amber   { background: var(--amber-lt);   border-color: var(--amber-mid);   color: var(--amber); }
.btn-link.amber:hover   { background: #fef3c7; }
.btn-link.violet  { background: var(--violet-lt);  border-color: var(--violet-mid);  color: var(--violet); }
.btn-link.violet:hover  { background: #ede7fd; }
.btn-link.navy    { background: var(--navy-lt, #eef1f8); border-color: var(--navy-mid, #c9d0e3); color: var(--navy); }
.btn-link.navy:hover    { background: #e2e6f2; }
.btn-link.red     { background: var(--red-lt);     border-color: var(--red-mid);     color: var(--red); }
.btn-link.red:hover     { background: #fee2e2; }
.btn-link.rose    { background: var(--rose-lt);    border-color: var(--rose-mid);    color: var(--rose); }
.btn-link.rose:hover    { background: #ffe4e6; }
.btn-link.slate   { background: var(--slate-lt);   border-color: var(--slate-mid);   color: var(--slate); }
.btn-link.slate:hover   { background: #e2e8f0; }
.btn-link.gold    { background: #fff8e1; border-color: #f5d76e; color: #9a7500; }
.btn-link.gold:hover    { background: #fef0c2; }
.btn-link.is-active { box-shadow: 0 0 0 1px currentColor inset; font-weight: 700; }
.hdr-badge {
  font-size: .64rem;
  font-weight: 600;
  letter-spacing: .06em;
  text-transform: uppercase;
  border-radius: 999px;
  padding: .22rem .75rem;
  border: 1px solid;
}
.date-pill {
  font-family: var(--mono);
  font-size: .72rem;
  font-weight: 500;
  padding: .4rem 1.1rem;
  border-radius: 999px;
  border: 1px solid;
  white-space: nowrap;
  letter-spacing: .04em;
  align-self: flex-start;
  margin-top: .25rem;
}

/* ── CSV bar ── */
.csv-bar {
  display: flex;
  align-items: center;
  gap: .6rem;
  padding: .6rem 2.5rem;
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  flex-wrap: wrap;
}
.csv-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: .35rem .95rem;
  font-family: var(--mono);
  font-size: .7rem;
  font-weight: 500;
  border-radius: var(--r);
  border: 1px solid;
  cursor: pointer;
  transition: background .14s;
  letter-spacing: .03em;
}
.csv-primary {
  background: var(--emerald-lt);
  border-color: var(--emerald-mid);
  color: var(--emerald);
}
.csv-primary:hover { background: #d1fae5; }
.csv-secondary {
  background: var(--blue-lt);
  border-color: var(--blue-mid);
  color: var(--blue);
}
.csv-secondary:hover { background: #dbeafe; }
.csv-label {
  font-family: var(--mono);
  font-size: .67rem;
  color: var(--subtle);
  margin-left: auto;
}

/* ── KPI strip ── */
.kpi-strip {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  border-bottom: 1px solid var(--border);
}
.kpi {
  background: var(--surface);
  padding: 1.1rem 1.6rem;
  border-right: 1px solid var(--border);
  position: relative;
}
.kpi:last-child { border-right: none; }
.kpi::after {
  content: '';
  position: absolute;
  top: 0; left: 0; right: 0;
  height: 2px;
  border-radius: 0 0 2px 2px;
  background: var(--accent);
}
.kpi-lbl {
  font-size: .62rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .11em;
  color: var(--muted);
  margin-bottom: .35rem;
}
.kpi-val {
  font-size: clamp(1.1rem, 2vw, 1.65rem);
  font-weight: 700;
  letter-spacing: -.02em;
  line-height: 1;
  color: var(--accent);
}
.kpi-hint {
  font-size: .67rem;
  color: var(--subtle);
  margin-top: .25rem;
}

/* ── Charts area ── */
.charts-area {
  padding: 1.1rem 2.5rem;
  display: grid;
  gap: .85rem;
}
.chart-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--rl);
  padding: 1.1rem 1.3rem .9rem;
}
.chart-lbl {
  font-size: .62rem;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: .11em;
  color: var(--muted);
  margin-bottom: .8rem;
}
.chart-wrap { position: relative; height: 220px; }

/* ── Table section ── */
.table-sec { padding: 0 2.5rem 3rem; }
.tbl-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: .9rem 0 .7rem;
  flex-wrap: wrap;
  gap: .6rem;
}
.tbl-title {
  font-size: .62rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .12em;
  color: var(--muted);
}
.tbl-count { color: var(--ACCENT1); margin-left: .4rem; }
.controls { display: flex; align-items: center; gap: .85rem; flex-wrap: wrap; }
.search {
  background: var(--surface);
  border: 1px solid var(--border2);
  border-radius: var(--r);
  color: var(--text);
  font-family: var(--sans);
  font-size: .82rem;
  padding: .38rem .9rem;
  outline: none;
  width: 210px;
  transition: border-color .16s, box-shadow .16s;
}
.search::placeholder { color: var(--subtle); }
.search:focus {
  border-color: var(--ACCENT1);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ACCENT1) 12%, transparent);
}
.legend-row { display: flex; gap: .9rem; align-items: center; flex-wrap: wrap; }
.leg { display: flex; align-items: center; gap: .3rem; font-size: .68rem; color: var(--muted); }
.leg-dot { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; }

/* ── Table ── */
.tbl-outer {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--rl);
  /* Constrain height so the horizontal scrollbar sits right below the
     visible rows instead of at the very bottom of a (potentially very
     long) table — no need to scroll the whole page to reach it. */
  max-height: 72vh;
  overflow: auto;
}
table { width: 100%; border-collapse: collapse; font-size: .82rem; white-space: nowrap; }
thead tr {
  background: var(--surface2);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 2;
}
th {
  font-size: .62rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: .1em;
  color: var(--muted);
  padding: .68rem 1rem;
  text-align: left;
  cursor: pointer;
  user-select: none;
  transition: background .12s;
  white-space: nowrap;
}
th:hover { background: var(--surface3); }
th.r { text-align: right; } th.c { text-align: center; }
th .si { margin-left: .25rem; opacity: .3; font-style: normal; font-size: .65rem; }
th.sort-asc  .si::after { content: '▲'; opacity: 1; color: var(--ACCENT1); }
th.sort-desc .si::after { content: '▼'; opacity: 1; color: var(--ACCENT1); }
th:not(.sort-asc):not(.sort-desc) .si::after { content: '⇅'; }
.srow { border-bottom: 1px solid var(--border); transition: background .1s; }
.srow:last-child { border-bottom: none; }
.srow:hover { background: var(--bg); }
td { padding: .65rem 1rem; vertical-align: middle; }
td.r { text-align: right; } td.c { text-align: center; }

/* ── NEW-stock highlight row ── */
.srow.is-new { background: var(--new-row); }
.srow.is-new:hover { background: #fae8ff; }

/* ── Watchlist star toggle ── */
.star-th { width: 2rem; padding-left: .9rem !important; padding-right: 0 !important; }
.star-td { width: 2rem; text-align: center; padding-left: .9rem !important; padding-right: 0 !important; }
.wl-star {
  background: none;
  border: none;
  cursor: pointer;
  font-size: 1.05rem;
  line-height: 1;
  padding: 0;
  color: var(--subtle);
  transition: color .12s, transform .12s;
}
.wl-star:hover { color: #d4a017; transform: scale(1.15); }
.wl-star.is-active { color: #f5b800; }

/* ── Watchlist picker popover (which list to add this stock to?) ── */
.wl-picker {
  position: absolute;
  z-index: 9999;
  width: 240px;
  background: #fff;
  border: 1px solid var(--border, #e5e7eb);
  border-radius: 12px;
  box-shadow: 0 12px 32px rgba(15, 23, 42, .16), 0 2px 8px rgba(15, 23, 42, .08);
  padding: .65rem;
  font-family: var(--sans, "Outfit", sans-serif);
  animation: wlPickerIn .12s ease-out;
}
@keyframes wlPickerIn {
  from { opacity: 0; transform: translateY(-4px); }
  to   { opacity: 1; transform: translateY(0); }
}
.wl-picker-title {
  font-size: .72rem;
  font-weight: 600;
  color: var(--ink, #1f2937);
  padding: .15rem .3rem .5rem;
  border-bottom: 1px solid var(--border, #eef0f3);
  margin-bottom: .4rem;
}
.wl-picker-sym {
  font-family: var(--mono, monospace);
  color: #f5b800;
  background: #fff8e1;
  border-radius: 4px;
  padding: 0 .3rem;
}
.wl-picker-list { max-height: 220px; overflow-y: auto; display: flex; flex-direction: column; gap: .1rem; }
.wl-picker-empty { font-size: .72rem; color: var(--subtle, #8892a0); padding: .4rem .3rem; }
.wl-picker-row {
  display: flex;
  align-items: center;
  gap: .55rem;
  padding: .42rem .4rem;
  border-radius: 8px;
  cursor: pointer;
  font-size: .78rem;
  color: var(--ink, #1f2937);
  transition: background .1s;
}
.wl-picker-row:hover { background: #f6f7fb; }
.wl-picker-row.wl-picker-busy { opacity: .5; pointer-events: none; }
.wl-picker-row input[type="checkbox"] {
  width: 15px; height: 15px; accent-color: #f5b800; cursor: pointer; flex: none;
}
.wl-picker-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.wl-picker-new {
  display: flex;
  gap: .35rem;
  margin-top: .55rem;
  padding-top: .5rem;
  border-top: 1px solid var(--border, #eef0f3);
}
.wl-picker-input {
  flex: 1;
  min-width: 0;
  font-size: .74rem;
  padding: .38rem .55rem;
  border: 1px solid var(--border, #dfe3e8);
  border-radius: 7px;
  font-family: inherit;
}
.wl-picker-input:focus { outline: none; border-color: #f5b800; }
.wl-picker-create {
  font-size: .72rem;
  font-weight: 600;
  color: #1f2937;
  background: #ffe38f;
  border: 1px solid #f2c94c;
  border-radius: 7px;
  padding: .38rem .6rem;
  cursor: pointer;
  transition: filter .1s;
  flex: none;
}
.wl-picker-create:hover { filter: brightness(.96); }
.wl-picker-create:disabled { opacity: .5; cursor: default; }
@media (max-width: 640px) {
  .wl-picker { width: min(280px, calc(100vw - 24px)); }
}

/* ── Symbol tags ── */
.sym-tag {
  display: inline-flex;
  align-items: center;
  gap: .35rem;
  font-family: var(--mono);
  font-weight: 500;
  font-size: .72rem;
  padding: .2rem .6rem;
  border-radius: 6px;
  letter-spacing: .05em;
  border: 1px solid;
  transition: filter .12s;
}
.sym-tag:hover { filter: brightness(.93); }
.sym-new-star {
  font-size: .55rem;
  background: var(--new-bg);
  border: 1px solid var(--new-border);
  color: var(--new-text);
  border-radius: 999px;
  padding: .1rem .38rem;
  font-weight: 700;
  letter-spacing: .04em;
  white-space: nowrap;
}

/* ── Value pills ── */
.pill {
  display: inline-block;
  font-family: var(--mono);
  font-size: .71rem;
  font-weight: 500;
  padding: .18rem .55rem;
  border-radius: 999px;
  border: 1px solid transparent;
}
.pill-green  { background: var(--emerald-lt); border-color: var(--emerald-mid); color: var(--emerald); }
.pill-red    { background: var(--red-lt);     border-color: #fca5a5;            color: var(--red); }
.pill-rose   { background: var(--rose-lt);    border-color: var(--rose-mid);    color: var(--rose); }
.pill-slate  { background: var(--slate-lt);   border-color: var(--slate-mid);   color: var(--slate); }
.pill-amber  { background: var(--amber-lt);   border-color: var(--amber-mid);   color: var(--amber); }
.pill-indigo { background: var(--indigo-lt);  border-color: var(--indigo-mid);  color: var(--indigo); }
.pill-blue   { background: var(--blue-lt);    border-color: var(--blue-mid);    color: var(--blue); }
.pill-muted  { background: var(--surface2);   border-color: var(--border2);     color: var(--muted); }
.pill-violet { background: var(--violet-lt);  border-color: var(--violet-mid);  color: var(--violet); }

/* ── Volume bar ── */
.vol-wrap { display: flex; align-items: center; gap: .5rem; min-width: 130px; }
.vol-bg   { flex: 1; height: 4px; background: var(--surface3); border-radius: 99px; overflow: hidden; }
.vol-fill { height: 100%; border-radius: 99px; }
.vol-pct  { font-family: var(--mono); font-size: .7rem; font-weight: 600; min-width: 42px; text-align: right; }

/* ── IB badge ── */
.ib-badge {
  display: inline-block;
  font-family: var(--mono);
  font-size: .58rem;
  font-weight: 600;
  background: var(--amber-lt);
  border: 1px solid var(--amber-mid);
  color: var(--amber);
  padding: .1rem .4rem;
  border-radius: 4px;
  margin-left: .3rem;
  letter-spacing: .04em;
  vertical-align: middle;
}

/* ── No-data row ── */
.no-data {
  text-align: center;
  padding: 60px 20px;
  color: var(--muted);
  font-size: .82rem;
}

/* ── Info callout ── */
.callout {
  padding: .6rem 2.5rem;
  font-size: .78rem;
  color: var(--muted);
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}

/* ── Footer ── */
footer {
  text-align: center;
  padding: 1rem;
  font-size: .68rem;
  color: var(--subtle);
  border-top: 1px solid var(--border);
  background: var(--surface);
  font-family: var(--mono);
  letter-spacing: .04em;
}

/* ── Horizontal scroll wrapper for tables on small screens ── */
.tbl-outer { -webkit-overflow-scrolling: touch; }
table { min-width: 640px; }

/* ── Mobile responsiveness ── */
@media (max-width: 768px) {
  html { font-size: 13px; }

  header {
    padding: 1.1rem 1.1rem;
    flex-direction: column;
    align-items: stretch;
  }
  .site-nav { padding: .65rem 1.1rem; }
  .date-pill { align-self: flex-start; margin-top: .6rem; }

  .csv-bar { padding: .55rem 1.1rem; }
  .csv-label { margin-left: 0; width: 100%; }

  .charts-area { padding: .9rem 1.1rem; grid-template-columns: 1fr !important; }
  .chart-wrap { height: 180px; }

  .table-sec { padding: 0 1.1rem 2rem; }
  .callout { padding: .6rem 1.1rem; }

  .tbl-head { flex-direction: column; align-items: stretch; }
  .controls { width: 100%; }
  .search { width: 100%; }
  .legend-row { width: 100%; }

  .kpi-strip { grid-template-columns: repeat(2, 1fr); }
  .kpi { padding: .85rem 1rem; border-bottom: 1px solid var(--border); }

  th, td { padding: .55rem .7rem; }
}

@media (max-width: 480px) {
  .kpi-strip { grid-template-columns: 1fr 1fr; }
  header h1 { font-size: 1.25rem; }
  .hdr-sub { font-size: .74rem; }
  .badge-row { gap: .3rem; }
  .hdr-badge { font-size: .6rem; padding: .18rem .6rem; }
}
"""

_TABLE_SORT_JS = """
let sortCol = null, sortAsc = true;
document.querySelectorAll('#mainTable thead th').forEach(th => {
  th.addEventListener('click', () => {
    const col  = th.dataset.col;
    const type = th.dataset.type;
    if (!col) return;
    sortAsc = (sortCol === col) ? !sortAsc : true;
    sortCol = col;
    document.querySelectorAll('#mainTable thead th')
      .forEach(h => h.classList.remove('sort-asc','sort-desc'));
    th.classList.add(sortAsc ? 'sort-asc' : 'sort-desc');
    const tbody = document.getElementById('tableBody');
    Array.from(tbody.querySelectorAll('.srow'))
      .sort((a, b) => {
        let av = a.dataset[col], bv = b.dataset[col];
        if (type === 'num') {
          av = parseFloat(av); bv = parseFloat(bv);
          if (isNaN(av)) av = -Infinity;
          if (isNaN(bv)) bv = -Infinity;
          return sortAsc ? av - bv : bv - av;
        }
        return sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
      })
      .forEach(r => tbody.appendChild(r));
  });
});
"""

_FILTER_JS = """
function filterRows() {
  const q = document.getElementById('searchInput').value.toLowerCase();
  document.querySelectorAll('#tableBody .srow').forEach(r => {
    const sym    = (r.dataset.sym    || '').toLowerCase();
    const indgrp = (r.dataset.indgrp || '').toLowerCase();
    const ind    = (r.dataset.ind    || '').toLowerCase();
    r.style.display = (sym.includes(q)||indgrp.includes(q)||ind.includes(q)) ? '' : 'none';
  });
}
"""

# Populates the "✦ New Stocks (Last 10 Days)" table at the bottom of the
# page by cloning the header + any .is-new rows out of the main table —
# same data already on the page, no server-side duplication needed, works
# identically across every dashboard since it only depends on #mainTable
# and the existing .is-new row marker.
_NEW_STOCKS_JS = """
(function() {
  var mainTable  = document.getElementById('mainTable');
  var newSection = document.getElementById('newStocksSection');
  var newHead    = document.getElementById('newStocksHead');
  var newBody    = document.getElementById('newStocksBody');
  var newCount   = document.getElementById('newStocksCount');
  if (!mainTable || !newSection || !newHead || !newBody) return;

  var headRow = mainTable.querySelector('thead tr');
  var newRows = Array.prototype.slice.call(mainTable.querySelectorAll('tbody tr.is-new'));

  if (!headRow || newRows.length === 0) {
    newSection.style.display = 'none';
    return;
  }

  newHead.innerHTML = headRow.outerHTML;
  newRows.forEach(function(row) {
    newBody.appendChild(row.cloneNode(true));
  });
  if (newCount) newCount.textContent = newRows.length;
})();

function scrollToNewStocks(e) {
  if (e) e.preventDefault();
  var el = document.getElementById('newStocksSection');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
"""

# Shared "New Stocks (Last 10 Days)" table shell — same block dropped into
# every dashboard right before its footer. _NEW_STOCKS_JS fills it in on
# load; if there are zero new stocks that run, the JS just hides it.
_NEW_STOCKS_SECTION_HTML = """
<div class="table-sec" id="newStocksSection" style="padding-top:1.1rem;">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">✦ New Stocks <span style="color:var(--new-text)">(Last 10 Days)</span></span>
      <span class="tbl-count tbl-title">[<span id="newStocksCount">0</span>]</span>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="newStocksTable">
      <thead id="newStocksHead"></thead>
      <tbody id="newStocksBody"></tbody>
    </table>
  </div>
</div>
"""

_CHARTJS_DEFAULTS = """
Chart.defaults.font.family = "'Outfit', sans-serif";
Chart.defaults.font.size   = 11;
Chart.defaults.color       = "#5a6282";
"""

# ── TradingView symbol export (download .txt / copy to clipboard) ──────────
# Reads whatever rows are currently visible in #tableBody (respects the
# search box filter), pulls each row's data-sym, and formats them as
# "NSE:SYMBOL" — the exact format TradingView's watchlist import/paste
# accepts. Works identically on every dashboard since they all share the
# same #tableBody / .srow / data-sym markup.
_TV_EXPORT_JS = """
function _tvSymbolList() {
  return Array.from(document.querySelectorAll('#tableBody .srow'))
    .filter(r => r.style.display !== 'none')
    .map(r => (r.dataset.sym || '').toUpperCase())
    .filter(s => s !== '');
}

function downloadTVList() {
  const syms = _tvSymbolList();
  if (!syms.length) { alert('No symbols to export.'); return; }
  const blob = new Blob([syms.join(',')], { type: 'text/plain' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url;
  a.download = (document.getElementById('tvFileName') || {}).value
               || 'tradingview_watchlist.txt';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function copyTVList() {
  const syms = _tvSymbolList();
  if (!syms.length) { alert('No symbols to copy.'); return; }
  const text = syms.join(',');
  const btn  = document.getElementById('tvCopyBtn');
  const done = () => {
    if (!btn) return;
    const orig = btn.dataset.origLabel || btn.textContent;
    btn.dataset.origLabel = orig;
    btn.textContent = '✓ Copied!';
    setTimeout(() => { btn.textContent = orig; }, 1600);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done).catch(() => _tvFallbackCopy(text, done));
  } else {
    _tvFallbackCopy(text, done);
  }
}

function _tvFallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { alert('Copy failed — please copy manually.'); }
  ta.remove();
}
"""


def _tv_export_bar(filename: str) -> str:
    """TradingView watchlist export bar — download .txt / copy to clipboard.

    Both actions read the currently-visible rows of #tableBody, so they
    honour the search-box filter (export only what's on screen).
    """
    return f"""
<div class="csv-bar">
  <a class="csv-btn csv-primary" href="javascript:void(0)" onclick="downloadTVList()">⬇ Download TradingView List</a>
  <a class="csv-btn csv-secondary" id="tvCopyBtn" href="javascript:void(0)" onclick="copyTVList()">⧉ Copy TradingView List</a>
  <input type="hidden" id="tvFileName" value="{filename}">
  <span class="csv-label">Format: SYMBOL, comma-separated — paste directly into a TradingView watchlist</span>
</div>"""


def _site_nav(active: str, date_str: str) -> str:
    """
    Shared cross-page nav bar — identical on every generated dashboard page
    (and mirrored by the nav-tabs on the tool pages) so a visitor can jump
    to Home or any other dashboard from wherever they land.

    `active` is one of: "momentum", "elite", "volume", "rocket", "newrshigh",
    "stage4", "sme_momentum", "sme_elite".
    `date_str` is the scan date in YYYYMMDD form (dashboards for the same
    date live side-by-side in the same folder, so links are relative).
    """
    def _link(key, href, cls, label):
        active_cls = " is-active" if key == active else ""
        return f'<a href="{href}" class="btn-link {cls}{active_cls}">{label}</a>'

    links = "".join([
        _link("momentum",  f"dashboard_{date_str}.html",           "indigo",  "📊 Momentum"),
        _link("elite",     f"elite_dashboard_{date_str}.html",     "green",   "⚡ Elite"),
        _link("volume",    f"volume_dashboard_{date_str}.html",    "blue",    "🔵 Volume"),
        _link("rocket",    f"rocket_dashboard_{date_str}.html",    "amber",   "🚀 Rocket"),
        _link("newrshigh", f"newrshigh_dashboard_{date_str}.html", "rose",    "🔥 New RS High"),
        _link("stage4",    f"stage4_dashboard_{date_str}.html",    "red",     "📉 Stage 4"),
        _link("sme_momentum", f"sme_momentum_dashboard_{date_str}.html", "violet", "🏷️ SME Momentum"),
        _link("sme_elite",    f"sme_elite_dashboard_{date_str}.html",    "violet", "🏷️ SME Elite"),
        _link("watchlist",    "../watchlist.html",                      "gold",   "⭐ Watchlist"),
    ])
    return f"""
<nav class="site-nav">
  <a href="../index.html" class="btn-link navy">🏠 Home</a>
  {links}
  <a href="../position-size.html" class="btn-link violet">📐 Position Size</a>
  <a href="../position-tracker.html" class="btn-link navy">📈 Position Tracker</a>
</nav>"""


def _html_head(title: str, accent1: str, accent2: str, active: str | None = None, date_str: str | None = None) -> str:
    nav_html = _site_nav(active, date_str) if (active and date_str) else ""
    # The watchlist-star module paints/toggles the ☆/★ buttons added to every
    # table row below. It's only meaningful on pages that carry the shared
    # nav (i.e. real per-date dashboards, one folder below docs/), since the
    # import path is relative to that location.
    star_script = (
        '<script type="module" src="../js/watchlist-star.js"></script>'
        if nav_html else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<script src="{_CDN_CHARTJS}"></script>
<link href="{_GOOGLE_FONTS}" rel="stylesheet"/>
<style>
:root {{ --ACCENT1:{accent1}; --ACCENT2:{accent2}; }}
{_BASE_CSS}
</style>
{star_script}
</head>
<body>
<div class="topbar"></div>
{nav_html}
"""


def _new_star(is_new: bool) -> str:
    if not is_new:
        return ""
    return ' <span class="sym-new-star">✦ NEW</span>'


def _star_cell() -> str:
    """
    Watchlist star toggle — dropped just before the Symbol cell in every
    dashboard table. Reads the row's own data-sym/data-close/data-indgrp/
    data-ind attributes at click time (see watchlist-star.js), so no extra
    per-button data needs to be threaded through here.
    """
    return '<td class="star-td"><button type="button" class="wl-star" onclick="toggleStar(this)" title="Add to Watchlist">☆</button></td>'


def _csv_bar_passing(date_str: str, csv_filename: str | None = None, full_csv_filename: str | None = None) -> str:
    sd = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
    pf = csv_filename or f"passing_stocks_{date_str}.csv"
    ff = full_csv_filename or f"full_results_{date_str}.csv"
    return f"""
<div class="csv-bar">
  <a class="csv-btn csv-primary" href="{pf}" download="{pf}">⬇ Download Passing CSV</a>
  <a class="csv-btn csv-secondary" href="{ff}" download="{ff}">⬇ Full Results CSV</a>
  <span class="csv-label">Passing Stocks · Scan date: {sd}</span>
</div>"""


def _csv_bar_elite(date_str: str, csv_filename: str | None = None) -> str:
    sd = datetime.strptime(date_str, "%Y%m%d").strftime("%Y-%m-%d")
    ef = csv_filename or f"passing_ema10_{date_str}.csv"
    return f"""
<div class="csv-bar">
  <a class="csv-btn csv-primary" href="{ef}" download="{ef}">⬇ Download Elite CSV</a>
  <span class="csv-label">Elite Stocks (Close &gt; EMA10) · Scan date: {sd}</span>
</div>"""


# ─────────────────────────────────────────────────────────────────────────────
#  PASSING STOCKS DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def build_passing_dashboard(
    passing: pd.DataFrame,
    out_path: Path,
    date_str: str,
    known_symbols: set[str] | None = None,
    page_title: str = "Alpha Momentum — Passing Stocks",
    brand_name: str = "Alpha Momentum · NSE Scanner",
    heading: str = "Minervini Trend Template",
    subheading: str = "All 8 Minervini conditions passing · NSE India",
    nav_active: str = "momentum",
    csv_filename: str | None = None,
    full_csv_filename: str | None = None,
    tv_export_filename: str | None = None,
) -> None:
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    n_stocks    = len(passing)
    n_above_ema = int(passing.get("cond9_price_above_ema10", pd.Series(dtype=bool)).sum()) \
                  if "cond9_price_above_ema10" in passing.columns else "N/A"
    total_tmc_s = fmt_cr(passing["total_market_cap_cr"].dropna().sum()) \
                  if "total_market_cap_cr" in passing.columns else "N/A"
    total_tv_s  = fmt_cr(passing["traded_value_cr"].dropna().sum()) \
                  if "traded_value_cr" in passing.columns else "N/A"

    rows_html = ""
    chart_labels, chart_total = [], []

    sort_col = "rs_percentile" if "rs_percentile" in passing.columns else "close"
    for _, row in passing.sort_values(sort_col, ascending=False).iterrows():
        sym = _display_symbol(row.get("symbol", ""), row.get("name"))
        link       = _tv_link(row.get("symbol", sym))
        is_new     = sym not in known
        new_cls    = " is-new" if is_new else ""
        close      = row.get("close", np.nan)
        ema10      = row.get("EMA10",  np.nan)
        rs         = row.get("rs_percentile", np.nan)
        tmc        = row.get("total_market_cap_cr", np.nan)
        tv         = row.get("traded_value_cr", np.nan)
        tvpct      = row.get("traded_val_pct_mc", np.nan)
        ind_grp    = str(row.get("industry_group", "")) or "—"
        industry   = str(row.get("industry",       "")) or "—"
        result_date = str(row.get("result_date", "—"))
        price_band  = str(row.get("price_band",  "—"))

        close_s = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
        ema10_s = f"₹{float(ema10):,.2f}" if _safe(ema10) else "N/A"
        rs_s    = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"
        tmc_s   = fmt_cr(tmc)
        tv_s    = fmt_cr(tv)
        tvpct_s = f"{float(tvpct):.4f}%"  if _safe(tvpct) else "N/A"

        try:
            above_ema = float(close) > float(ema10)
            ema_cls = "pill-green" if above_ema else "pill-red"
        except Exception:
            ema_cls = "pill-muted"

        rows_html += f"""
        <tr class="srow{new_cls}"
          data-sym="{sym}" data-band="{price_band}" data-close="{_r(close)}" data-rs="{_r(rs)}"
          data-ema10="{_r(ema10)}" data-tmc="{_r(tmc)}"
          data-tv="{_r(tv)}" data-tvpct="{_r(tvpct,6)}"
          data-indgrp="{ind_grp}" data-ind="{industry}">
          {_star_cell()}
          <td>
            <a class="sym-tag" style="background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo)"
               href="{link}" target="_blank" rel="noopener">{sym}{_new_star(is_new)}</a>
          </td>
          <td class="c" style="font-family:var(--mono);color:var(--muted);font-size:.74rem" title="Circuit price band">{price_band}</td>
          <td class="r" style="font-family:var(--mono)">{close_s}</td>
          <td class="r"><span class="pill {ema_cls}">{ema10_s}</span></td>
          <td class="r"><span class="pill pill-amber">{rs_s}</span></td>
          <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tmc_s}</td>
          <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tv_s}</td>
          <td class="r" style="font-family:var(--mono);color:var(--subtle);font-size:.73rem">{tvpct_s}</td>
          <td style="color:var(--muted);font-size:.78rem;max-width:150px;overflow:hidden;text-overflow:ellipsis">{ind_grp}</td>
          <td style="color:var(--subtle);font-size:.74rem;max-width:130px;overflow:hidden;text-overflow:ellipsis">{industry}</td>
        </tr>"""

        chart_labels.append(f'"{sym}"')
        chart_total.append(_r(tmc))

    n_new = sum(1 for _, r in passing.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known)

    html  = _html_head(f"{page_title} — {date_display}",
                       "var(--indigo)", "var(--blue)", active=nav_active, date_str=date_str)
    html += _csv_bar_passing(date_str, csv_filename=csv_filename, full_csv_filename=full_csv_filename)
    html += _tv_export_bar(tv_export_filename or f"tradingview_passing_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--indigo)"></div>
      <span class="brand-name">{brand_name}</span>
    </div>
    <h1>{heading}</h1>
    <p class="hdr-sub">{subheading} · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo)">✓ All 8 Conditions</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo)">{date_display}</div>
</header>

<div class="kpi-strip" style="--accent:var(--indigo)">
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Passing Stocks</div>
    <div class="kpi-val">{n_stocks}</div>
    <div class="kpi-hint">all 8 conditions met</div>
  </div>
  <div class="kpi" style="--accent:var(--violet)">
    <div class="kpi-lbl">Above EMA10</div>
    <div class="kpi-val">{n_above_ema}</div>
    <div class="kpi-hint">close &gt; 10-period ema</div>
  </div>
  <div class="kpi" style="--accent:var(--emerald)">
    <div class="kpi-lbl">Combined Market Cap</div>
    <div class="kpi-val">{total_tmc_s}</div>
    <div class="kpi-hint">aggregate market cap</div>
  </div>
  <div class="kpi" style="--accent:var(--blue)">
    <div class="kpi-lbl">Total Traded Value</div>
    <div class="kpi-val">{total_tv_s}</div>
    <div class="kpi-hint">today's traded volume</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--indigo)">Minervini Trend Template:</strong>
  Stocks satisfying all 8 conditions — price above MA150 &amp; MA200, MA150 &gt; MA200,
  MA200 trending up ≥ 1 month, MA50 above MA150 &amp; MA200, price above MA50,
  price ≥ 30% above 52-week low, within 25% of 52-week high, and RS percentile ≥ 70.
  These are momentum candidates in a confirmed Stage 2 uptrend.
</div>

<div class="charts-area" style="grid-template-columns:1fr">
  <div class="chart-card">
    <div class="chart-lbl">Total Market Cap by Stock (₹ Cr)</div>
    <div class="chart-wrap">
      <canvas id="barChart" role="img" aria-label="Market cap bar chart for passing stocks"></canvas>
    </div>
  </div>
</div>

<div class="table-sec">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">Passing Stocks Detail</span>
      <span class="tbl-count tbl-title">[{n_stocks}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--emerald)"></div>Close &gt; EMA10</div>
        <div class="leg"><div class="leg-dot" style="background:var(--red)"></div>Close ≤ EMA10</div>
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol / industry…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"    data-type="str">Symbol<i class="si"></i></th>
        <th class="c" data-col="band" data-type="str" title="Circuit price band — the max % a stock can move from previous close">Band<i class="si"></i></th>
        <th class="r" data-col="close"  data-type="num">Close ₹<i class="si"></i></th>
        <th class="r" data-col="ema10"  data-type="num">EMA10 ₹<i class="si"></i></th>
        <th class="r" data-col="rs"     data-type="num">RS %ile<i class="si"></i></th>
        <th class="r" data-col="tmc"    data-type="num">Mkt Cap<i class="si"></i></th>
        <th class="r" data-col="tv"     data-type="num">Traded Val<i class="si"></i></th>
        <th class="r" data-col="tvpct"  data-type="num">TV % MC<i class="si"></i></th>
        <th          data-col="indgrp" data-type="str">Industry Group<i class="si"></i></th>
        <th          data-col="ind"    data-type="str">Industry<i class="si"></i></th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
const labels    = [{",".join(chart_labels)}];
const totalData = [{",".join(chart_total)}];
{_CHARTJS_DEFAULTS}
new Chart(document.getElementById('barChart'), {{
  type: 'bar',
  data: {{ labels, datasets: [{{
    label: 'Mkt Cap (₹ Cr)', data: totalData,
    backgroundColor: 'rgba(79,70,229,0.12)',
    borderColor:     'rgba(79,70,229,0.45)',
    borderWidth: 1, borderRadius: 3,
  }}]}},
  options: {{
    responsive: true, maintainAspectRatio: false,
    plugins: {{
      legend: {{ display: false }},
      tooltip: {{
        backgroundColor: '#fff', borderColor: '#e2e6f0', borderWidth: 1,
        titleColor: '#0f1629', bodyColor: '#5a6282', padding: 10,
        callbacks: {{ label: c => ` ₹${{(c.parsed.y||0).toLocaleString('en-IN')}} Cr` }},
      }},
    }},
    scales: {{
      x: {{ ticks: {{ color: '#8b93b5', maxTicksLimit: 30 }}, grid: {{ color: '#f1f3f9' }} }},
      y: {{ ticks: {{ color: '#8b93b5', callback: v => '₹' + Number(v).toLocaleString('en-IN') }},
            grid: {{ color: '#f1f3f9' }} }},
    }},
  }},
}});
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Passing dashboard → %s", out_path)


# ─────────────────────────────────────────────────────────────────────────────
#  ELITE DASHBOARD  (all 8 conditions + above EMA10)
# ─────────────────────────────────────────────────────────────────────────────

def build_passing_ema10_dashboard(
    df: pd.DataFrame,
    out_path: Path,
    date_str: str,
    history: list[dict] | None = None,
    known_symbols: set[str] | None = None,
    page_title: str = "Alpha Momentum — Elite Stocks",
    brand_name: str = "Alpha Momentum · Elite Filter",
    heading: str = "Passing Stocks Above EMA10",
    subheading: str = "All 8 Minervini conditions + Close &gt; 10-period EMA · NSE India",
    nav_active: str = "elite",
    csv_filename: str | None = None,
    tv_export_filename: str | None = None,
) -> None:
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    n_total     = len(df)
    total_tmc_s = fmt_cr(df["total_market_cap_cr"].dropna().sum()) \
                  if "total_market_cap_cr" in df.columns else "N/A"
    total_tv_s  = fmt_cr(df["traded_value_cr"].dropna().sum()) \
                  if "traded_value_cr" in df.columns else "N/A"

    rows_html = ""
    for _, row in df.iterrows():
        sym = _display_symbol(row.get("symbol", ""), row.get("name"))
        link     = _tv_link(row.get("symbol", sym))
        is_new   = sym not in known
        new_cls  = " is-new" if is_new else ""
        close    = row.get("close",  np.nan)
        ema10    = row.get("EMA10",  np.nan)
        rs       = row.get("rs_percentile", np.nan)
        tmc      = row.get("total_market_cap_cr", np.nan)
        tv       = row.get("traded_value_cr", np.nan)
        tvpct    = row.get("traded_val_pct_mc", np.nan)
        ind_grp  = str(row.get("industry_group", "")) or "—"
        industry = str(row.get("industry",       "")) or "—"
        result_date = str(row.get("result_date", "—"))
        price_band  = str(row.get("price_band",  "—"))

        close_s = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
        ema10_s = f"₹{float(ema10):,.2f}" if _safe(ema10) else "N/A"
        rs_s    = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"
        tmc_s   = fmt_cr(tmc)
        tv_s    = fmt_cr(tv)
        tvpct_s = f"{float(tvpct):.4f}%"  if _safe(tvpct) else "N/A"

        try:
            gap_pct = (float(close) - float(ema10)) / float(ema10) * 100
            gap_s   = f"+{gap_pct:.2f}%"
            gap_col = "var(--emerald)"
        except Exception:
            gap_pct = -1.0; gap_s = "N/A"; gap_col = "var(--subtle)"

        rows_html += f"""
        <tr class="srow{new_cls}"
          data-sym="{sym}" data-band="{price_band}" data-close="{_r(close)}" data-ema10="{_r(ema10)}"
          data-gap="{_r(gap_pct,4)}" data-rs="{_r(rs)}" data-tmc="{_r(tmc)}"
          data-tv="{_r(tv)}" data-tvpct="{_r(tvpct,6)}"
          data-indgrp="{ind_grp}" data-ind="{industry}">
          {_star_cell()}
          <td>
            <a class="sym-tag" style="background:var(--emerald-lt);border-color:var(--emerald-mid);color:var(--emerald)"
               href="{link}" target="_blank" rel="noopener">{sym}{_new_star(is_new)}</a>
          </td>
          <td class="c" style="font-family:var(--mono);color:var(--muted);font-size:.74rem" title="Circuit price band">{price_band}</td>
          <td class="r" style="font-family:var(--mono)">{close_s}</td>
          <td class="r"><span class="pill pill-green">{ema10_s}</span></td>
          <td class="r" style="font-family:var(--mono);font-weight:600;color:{gap_col}">{gap_s}</td>
          <td class="r"><span class="pill pill-amber">{rs_s}</span></td>
          <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tmc_s}</td>
          <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tv_s}</td>
          <td class="r" style="font-family:var(--mono);color:var(--subtle);font-size:.73rem">{tvpct_s}</td>
          <td style="color:var(--muted);font-size:.78rem;max-width:150px;overflow:hidden;text-overflow:ellipsis">{ind_grp}</td>
          <td style="color:var(--subtle);font-size:.74rem;max-width:130px;overflow:hidden;text-overflow:ellipsis">{industry}</td>
        </tr>"""

    # History charts — filter out any weekend entries (NSE closed Sat/Sun)
    hist = list(history) if history else []
    today_entry = {
        "date": date_str, "count": n_total,
        "market_cap_cr":   float(df["total_market_cap_cr"].dropna().sum()) if "total_market_cap_cr" in df.columns else 0.0,
        "traded_value_cr": float(df["traded_value_cr"].dropna().sum())     if "traded_value_cr"     in df.columns else 0.0,
    }
    hist = [h for h in hist if h.get("date") != date_str]
    hist.append(today_entry)
    hist.sort(key=lambda h: h["date"])
    # Remove Saturday (weekday 5) and Sunday (weekday 6)
    def _is_weekday(d: str) -> bool:
        try:
            return datetime.strptime(d, "%Y%m%d").weekday() < 5
        except Exception:
            return True
    hist = [h for h in hist if _is_weekday(h.get("date", ""))]

    def _dl(d):
        try: return datetime.strptime(d, "%Y%m%d").strftime("%d %b")
        except: return d

    hl_js  = ",".join(f'"{_dl(h["date"])}"'                           for h in hist)
    hc_js  = ",".join(str(int(h.get("count", 0)))                     for h in hist)
    hmc_js = ",".join(str(round(float(h.get("market_cap_cr", 0)), 2)) for h in hist)
    htv_js = ",".join(str(round(float(h.get("traded_value_cr",0)),2)) for h in hist)

    n_new = sum(1 for _, r in df.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known)

    html  = _html_head(f"{page_title} — {date_display}",
                       "var(--emerald)", "var(--blue)", active=nav_active, date_str=date_str)
    html += _csv_bar_elite(date_str, csv_filename=csv_filename)
    html += _tv_export_bar(tv_export_filename or f"tradingview_elite_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--emerald)"></div>
      <span class="brand-name">{brand_name}</span>
    </div>
    <h1>{heading}</h1>
    <p class="hdr-sub">{subheading} · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--emerald-lt);border-color:var(--emerald-mid);color:var(--emerald)">✓ 8 Minervini Conditions</span>
      <span class="hdr-badge" style="background:var(--blue-lt);border-color:var(--blue-mid);color:var(--blue)">✓ Close &gt; EMA10</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--emerald-lt);border-color:var(--emerald-mid);color:var(--emerald)">{date_display}</div>
</header>

<div class="kpi-strip">
  <div class="kpi" style="--accent:var(--emerald)">
    <div class="kpi-lbl">Elite Stocks</div>
    <div class="kpi-val">{n_total}</div>
    <div class="kpi-hint">all filters passing</div>
  </div>
  <div class="kpi" style="--accent:var(--blue)">
    <div class="kpi-lbl">Combined Market Cap</div>
    <div class="kpi-val">{total_tmc_s}</div>
    <div class="kpi-hint">aggregate market cap</div>
  </div>
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Total Traded Value</div>
    <div class="kpi-val">{total_tv_s}</div>
    <div class="kpi-hint">today's traded volume</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--emerald)">Elite Filter:</strong>
  All 8 Minervini conditions met <strong>AND</strong> the latest close is above the 10-period EMA —
  indicating the stock is in an active, accelerating uptrend. These are the highest-conviction
  actionable buy candidates from today's scan.
</div>

<div class="charts-area" style="grid-template-columns:1fr 1fr 1fr">
  <div class="chart-card">
    <div class="chart-lbl">Elite Stock Count — Daily</div>
    <div class="chart-wrap"><canvas id="countChart" role="img" aria-label="Elite stock count over time"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="chart-lbl">Combined Market Cap (₹ Cr) — Daily</div>
    <div class="chart-wrap"><canvas id="mcChart" role="img" aria-label="Combined market cap over time"></canvas></div>
  </div>
  <div class="chart-card">
    <div class="chart-lbl">Total Traded Value (₹ Cr) — Daily</div>
    <div class="chart-wrap"><canvas id="tvChart" role="img" aria-label="Total traded value over time"></canvas></div>
  </div>
</div>

<div class="table-sec">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">Elite Stocks Detail</span>
      <span class="tbl-count tbl-title">[{n_total}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol / industry…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"   data-type="str">Symbol<i class="si"></i></th>
        <th class="c" data-col="band" data-type="str" title="Circuit price band — the max % a stock can move from previous close">Band<i class="si"></i></th>
        <th class="r" data-col="close"  data-type="num">Close ₹<i class="si"></i></th>
        <th class="r" data-col="ema10"  data-type="num">EMA10 ₹<i class="si"></i></th>
        <th class="r" data-col="gap"    data-type="num">Gap % Above EMA10<i class="si"></i></th>
        <th class="r" data-col="rs"     data-type="num">RS %ile<i class="si"></i></th>
        <th class="r" data-col="tmc"    data-type="num">Mkt Cap<i class="si"></i></th>
        <th class="r" data-col="tv"     data-type="num">Traded Val<i class="si"></i></th>
        <th class="r" data-col="tvpct"  data-type="num">TV % MC<i class="si"></i></th>
        <th          data-col="indgrp" data-type="str">Industry Group<i class="si"></i></th>
        <th          data-col="ind"    data-type="str">Industry<i class="si"></i></th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
const histLabels = [{hl_js}];
const histCount  = [{hc_js}];
const histMC     = [{hmc_js}];
const histTV     = [{htv_js}];
{_CHARTJS_DEFAULTS}
const lineDs = (data, color) => ({{
  data,
  borderColor: color,
  backgroundColor: color.replace('rgb','rgba').replace(')',',0.07)'),
  borderWidth: 2, pointRadius: 3.5, pointBackgroundColor: color, fill: true, tension: 0.35,
}});
const lineOpts = (yFmt, tipFmt) => ({{
  responsive: true, maintainAspectRatio: false,
  plugins: {{
    legend: {{ display: false }},
    tooltip: {{
      backgroundColor: '#fff', borderColor: '#e2e6f0', borderWidth: 1,
      titleColor: '#0f1629', bodyColor: '#5a6282', padding: 10,
      callbacks: {{ label: tipFmt }},
    }},
  }},
  scales: {{
    x: {{ ticks: {{ color: '#8b93b5', maxTicksLimit: 10 }}, grid: {{ color: '#f1f3f9' }} }},
    y: {{ ticks: {{ color: '#8b93b5', callback: yFmt }}, grid: {{ color: '#f1f3f9' }} }},
  }},
}});
new Chart(document.getElementById('countChart'), {{
  type: 'line',
  data: {{ labels: histLabels, datasets: [lineDs(histCount, 'rgb(5,150,105)')] }},
  options: lineOpts(v => v, c => ` ${{c.parsed.y}} stocks`),
}});
new Chart(document.getElementById('mcChart'), {{
  type: 'line',
  data: {{ labels: histLabels, datasets: [lineDs(histMC, 'rgb(37,99,235)')] }},
  options: lineOpts(v => '₹'+Number(v).toLocaleString('en-IN'), c => ` ₹${{(c.parsed.y||0).toLocaleString('en-IN')}} Cr`),
}});
new Chart(document.getElementById('tvChart'), {{
  type: 'line',
  data: {{ labels: histLabels, datasets: [lineDs(histTV, 'rgb(79,70,229)')] }},
  options: lineOpts(v => '₹'+Number(v).toLocaleString('en-IN'), c => ` ₹${{(c.parsed.y||0).toLocaleString('en-IN')}} Cr`),
}});
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Elite dashboard → %s", out_path)


# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME ACTION DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  VOLUME ACTION DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────
def build_volume_action_dashboard(
    volume_df: pd.DataFrame,
    out_path: Path,
    date_str: str,
    known_symbols: set[str] | None = None,
) -> None:
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    # Ensure bull_snort exists and is boolean, then sort so snort stocks
    # are grouped together at the top, with relative_volume as the tiebreaker.
    sorted_df = volume_df.copy()
    sorted_df["bull_snort"] = sorted_df.get("bull_snort", False).fillna(False).astype(bool)
    sorted_df = sorted_df.sort_values(
        ["bull_snort", "relative_volume"], ascending=[False, False]
    )

    n_total   = len(sorted_df)
    n_snort   = int(sorted_df["bull_snort"].sum())
    avg_rv_s  = f"{sorted_df['relative_volume'].dropna().mean():.1f}%" \
                if "relative_volume" in sorted_df.columns and not sorted_df["relative_volume"].dropna().empty \
                else "N/A"

    rows_html = ""
    for _, row in sorted_df.iterrows():
        sym = _display_symbol(row.get("symbol", ""), row.get("name"))
        link      = _tv_link(row.get("symbol", sym))
        is_new    = sym not in known
        new_cls   = " is-new" if is_new else ""
        close     = row.get("close", np.nan)
        relvol    = row.get("relative_volume", np.nan)
        rs        = row.get("rs_percentile", np.nan)
        is_snort  = bool(row.get("bull_snort", False))
        snort_flag = 1 if is_snort else 0
        result_date = str(row.get("result_date", "—"))
        price_band = str(row.get("price_band", "—"))

        close_s  = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
        rs_s     = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"

        try:
            rv_f    = float(relvol)
            rv_s    = f"{rv_f:.1f}%"
            bar_w   = max(0, min(150, rv_f))  # cap the bar visually at 150%
            bar_col = "var(--emerald)" if rv_f >= 100 else ("var(--amber)" if rv_f >= 50 else "var(--blue)")
        except Exception:
            rv_f = -1; rv_s = "N/A"; bar_w = 0; bar_col = "var(--subtle)"

        snort_html = (
            '<span class="pill pill-amber">🔥 Snort</span>' if is_snort
            else '<span class="pill pill-muted">—</span>'
        )

        rows_html += f"""
        <tr class="srow{new_cls}"
          data-sym="{sym}" data-band="{price_band}" data-close="{_r(close)}" data-relvol="{_r(relvol)}"
          data-snort="{snort_flag}" data-rs="{_r(rs)}">
          {_star_cell()}
          <td>
            <a class="sym-tag" style="background:var(--blue-lt);border-color:var(--blue-mid);color:var(--blue)"
               href="{link}" target="_blank" rel="noopener">{sym}{_new_star(is_new)}</a>
          </td>
          <td class="c" style="font-family:var(--mono);color:var(--muted);font-size:.74rem" title="Circuit price band">{price_band}</td>
          <td class="r" style="font-family:var(--mono)">{close_s}</td>
          <td>
            <div class="vol-wrap">
              <div class="vol-bg"><div class="vol-fill" style="width:{(bar_w/150)*100}%;background:{bar_col}"></div></div>
              <span class="vol-pct" style="color:{bar_col}">{rv_s}</span>
            </div>
          </td>
          <td class="c"><span class="pill pill-blue">Blue PPV</span></td>
          <td class="c">{snort_html}</td>
          <td class="r"><span class="pill pill-amber">{rs_s}</span></td>
        </tr>"""

    n_new = sum(1 for _, r in sorted_df.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known)

    html  = _html_head(f"Alpha Momentum — Volume Action — {date_display}",
                       "var(--blue)", "var(--indigo)", active="volume", date_str=date_str)
    html += _tv_export_bar(f"tradingview_volume_action_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--blue)"></div>
      <span class="brand-name">Alpha Momentum · Volume Action</span>
    </div>
    <h1>Pocket Pivot / Blue Volume</h1>
    <p class="hdr-sub">Institutional accumulation signals · NSE India · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--blue-lt);border-color:var(--blue-mid);color:var(--blue)">✓ Blue Volume Pivot</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--blue-lt);border-color:var(--blue-mid);color:var(--blue)">{date_display}</div>
</header>

<div class="kpi-strip">
  <div class="kpi" style="--accent:var(--blue)">
    <div class="kpi-lbl">Volume Signals</div>
    <div class="kpi-val">{n_total}</div>
    <div class="kpi-hint">blue volume / pocket pivot</div>
  </div>
  <div class="kpi" style="--accent:var(--amber)">
    <div class="kpi-lbl">Bull Snort</div>
    <div class="kpi-val">{n_snort}</div>
    <div class="kpi-hint">snort confirmed signals</div>
  </div>
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Avg Relative Volume</div>
    <div class="kpi-val">{avg_rv_s}</div>
    <div class="kpi-hint">vs. average volume</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--blue)">Pocket Pivot / Blue Volume:</strong>
  Up-day volume exceeding the highest down-day volume of the last 10 sessions, signaling
  institutional accumulation. <strong style="color:var(--amber)">🔥 Bull Snort</strong> stocks are listed first —
  these show an additional sharp volume/price confirmation on top of the base pivot signal.
</div>

<div class="table-sec">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">Volume Action Detail</span>
      <span class="tbl-count tbl-title">[{n_total}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--amber)"></div>🔥 Bull Snort</div>
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"    data-type="str">Symbol<i class="si"></i></th>
        <th class="c" data-col="band" data-type="str" title="Circuit price band — the max % a stock can move from previous close">Band<i class="si"></i></th>
        <th class="r" data-col="close"  data-type="num">Close ₹<i class="si"></i></th>
        <th class="r" data-col="relvol" data-type="num">Rel Volume<i class="si"></i></th>
        <th class="c">Signal</th>
        <th class="c" data-col="snort"  data-type="num">Bull Snort<i class="si"></i></th>
        <th class="r" data-col="rs"     data-type="num">RS %ile<i class="si"></i></th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Volume action dashboard → %s  (%d stocks, %d snort)", out_path, n_total, n_snort)

# ─────────────────────────────────────────────────────────────────────────────
#  ROCKET DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def build_rocket_dashboard(
    passing: pd.DataFrame,
    out_path: Path,
    date_str: str,
    known_symbols: set[str] | None = None,
) -> None:
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    rocket    = passing[passing["inside_bar"] == True].copy() \
                if "inside_bar" in passing.columns else pd.DataFrame()
    n_rocket  = len(rocket)
    n_passing = len(passing)

    if n_rocket == 0:
        rows_html = f'<tr><td colspan="11" class="no-data">No Rocket Stocks today — no inside bars among {n_passing} passing stocks.</td></tr>'
    else:
        rows_html = ""
        for _, row in rocket.sort_values("rs_percentile", ascending=False).iterrows():
            sym = _display_symbol(row.get("symbol", ""), row.get("name"))
            link     = _tv_link(row.get("symbol", sym))
            is_new   = sym not in known
            new_cls  = " is-new" if is_new else ""
            close    = row.get("close",  np.nan)
            ema10    = row.get("EMA10",  np.nan)
            rs       = row.get("rs_percentile", np.nan)
            tmc      = row.get("total_market_cap_cr", np.nan)
            tv       = row.get("traded_value_cr", np.nan)
            hi52_pct = row.get("pct_from_52w_high", np.nan)
            lo52_pct = row.get("pct_above_52w_low", np.nan)
            ind_grp  = str(row.get("industry_group", "")) or "—"
            price_band = str(row.get("price_band", "—"))

            close_s = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
            ema10_s = f"₹{float(ema10):,.2f}" if _safe(ema10) else "N/A"
            rs_s    = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"
            tmc_s   = fmt_cr(tmc)
            tv_s    = fmt_cr(tv)

            if not _safe(hi52_pct) and _safe(close) and _safe(row.get("52w_high")):
                hi52_pct = (float(close) / float(row["52w_high"]) - 1) * 100
            if not _safe(lo52_pct) and _safe(close) and _safe(row.get("52w_low")):
                lo52_pct = (float(close) / float(row["52w_low"]) - 1) * 100

            hi52_s = f"{float(hi52_pct):+.1f}%" if _safe(hi52_pct) else "N/A"
            lo52_s = f"{float(lo52_pct):+.1f}%" if _safe(lo52_pct) else "N/A"

            rows_html += f"""
            <tr class="srow{new_cls}"
              data-sym="{sym}" data-band="{price_band}" data-close="{_r(close)}" data-rs="{_r(rs)}"
              data-ema10="{_r(ema10)}" data-tmc="{_r(tmc)}" data-tv="{_r(tv)}"
              data-indgrp="{ind_grp}" data-ind="">
              {_star_cell()}
              <td>
                <a class="sym-tag" style="background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber)"
                   href="{link}" target="_blank" rel="noopener">{sym}<span class="ib-badge">IB</span>{_new_star(is_new)}</a>
              </td>
              <td class="c" style="font-family:var(--mono);color:var(--muted);font-size:.74rem" title="Circuit price band">{price_band}</td>
              <td class="r" style="font-family:var(--mono)">{close_s}</td>
              <td class="r"><span class="pill pill-green">{ema10_s}</span></td>
              <td class="r"><span class="pill pill-amber">{rs_s}</span></td>
              <td class="r" style="font-family:var(--mono);color:var(--emerald);font-weight:600">{lo52_s}</td>
              <td class="r" style="font-family:var(--mono);color:var(--amber);font-weight:600">{hi52_s}</td>
              <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tmc_s}</td>
              <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tv_s}</td>
              <td style="color:var(--muted);font-size:.78rem">{ind_grp}</td>
            </tr>"""

    n_new = sum(1 for _, r in rocket.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known) if n_rocket > 0 else 0
    hit_rate = f"{100*n_rocket/n_passing:.1f}%" if n_passing > 0 else "N/A"

    html  = _html_head(f"Alpha Momentum — Rocket Stocks — {date_display}",
                       "var(--amber)", "var(--red)", active="rocket", date_str=date_str)
    html += _tv_export_bar(f"tradingview_rocket_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--amber)"></div>
      <span class="brand-name">Alpha Momentum · Rocket Stocks</span>
    </div>
    <h1>Rocket Stocks</h1>
    <p class="hdr-sub">All 8 Minervini conditions + Inside Bar coiling setup · NSE India · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber)">✓ 8 Minervini Conditions</span>
      <span class="hdr-badge" style="background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber)">✓ Inside Bar</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber)">{date_display}</div>
</header>

<div class="kpi-strip">
  <div class="kpi" style="--accent:var(--amber)">
    <div class="kpi-lbl">Rocket Stocks</div>
    <div class="kpi-val">{n_rocket}</div>
    <div class="kpi-hint">inside bar coiling</div>
  </div>
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Total Passing</div>
    <div class="kpi-val">{n_passing}</div>
    <div class="kpi-hint">all 8 conditions</div>
  </div>
  <div class="kpi" style="--accent:var(--emerald)">
    <div class="kpi-lbl">IB Hit Rate</div>
    <div class="kpi-val">{hit_rate}</div>
    <div class="kpi-hint">inside bar frequency</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--amber)">Inside Bar:</strong>
  Today's high &lt; yesterday's high <strong>AND</strong> today's low &gt; yesterday's low —
  price compression inside a strong uptrend. Potential coiling setup before breakout.
</div>

<div class="table-sec" style="padding-top:1.1rem">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">Rocket Stocks</span>
      <span class="tbl-count tbl-title">[{n_rocket}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol / industry…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"  data-type="str">Symbol<i class="si"></i></th>
        <th class="c" data-col="band" data-type="str" title="Circuit price band — the max % a stock can move from previous close">Band<i class="si"></i></th>
        <th class="r" data-col="close" data-type="num">Close ₹<i class="si"></i></th>
        <th class="r" data-col="ema10" data-type="num">EMA10 ₹<i class="si"></i></th>
        <th class="r" data-col="rs"    data-type="num">RS %ile<i class="si"></i></th>
        <th class="r">% above 52W Low</th>
        <th class="r">% from 52W High</th>
        <th class="r" data-col="tmc"   data-type="num">Mkt Cap<i class="si"></i></th>
        <th class="r" data-col="tv"    data-type="num">Traded Val<i class="si"></i></th>
        <th data-col="indgrp" data-type="str">Industry Group<i class="si"></i></th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Rocket dashboard → %s  (%d stocks)", out_path, n_rocket)


# ─────────────────────────────────────────────────────────────────────────────
#  NEW RS HIGH DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def build_new_rs_high_dashboard(
    new_rs_df: pd.DataFrame,
    out_path: Path,
    date_str: str,
    universe_size: int = 0,
    lookback_days: int = 60,
    known_symbols: set[str] | None = None,
) -> None:
    """
    `new_rs_df` is the already-computed output of
    scanner.new_rs_high.compute_new_rs_highs() — stocks whose rs_percentile
    is at a fresh trailing-`lookback_days` high today. Unlike the other
    dashboards, this scans the FULL daily universe, not just Stage-1-gated
    stocks, so some of these names won't yet clear every Trend Template
    condition — that's the point: relative strength turning up early,
    before price necessarily confirms it.
    """
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    n_new_rs = len(new_rs_df) if new_rs_df is not None else 0

    if n_new_rs == 0:
        rows_html = f'<tr><td colspan="8" class="no-data">No stocks made a fresh {lookback_days}-day RS high today.</td></tr>'
    else:
        rows_html = ""
        for _, row in new_rs_df.iterrows():
            sym = _display_symbol(row.get("symbol", ""), row.get("name"))
            link      = _tv_link(row.get("symbol", sym))
            is_new    = sym not in known
            new_cls   = " is-new" if is_new else ""
            close     = row.get("close", np.nan)
            rs        = row.get("rs_percentile", np.nan)
            prior_rs  = row.get("prior_rs_high", np.nan)
            hist_days = row.get("rs_high_history_days", np.nan)
            also_mom  = bool(row.get("all_conditions_met", False))
            ind_grp   = str(row.get("industry_group", "")) or "—"

            close_s    = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
            rs_s       = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"
            prior_rs_s = f"{float(prior_rs):.1f}"  if _safe(prior_rs) else "N/A"
            delta_s    = f"{float(rs) - float(prior_rs):+.1f}" if _safe(rs) and _safe(prior_rs) else "N/A"
            hist_s     = f"{int(hist_days)}d history" if _safe(hist_days) else "—"

            mom_badge = (
                '<span class="pill pill-green">✓ In Momentum</span>' if also_mom
                else '<span class="pill" style="background:var(--surface-2);border-color:var(--border);color:var(--muted)">Not yet</span>'
            )

            rows_html += f"""
            <tr class="srow{new_cls}"
              data-sym="{sym}" data-close="{_r(close)}" data-rs="{_r(rs)}"
              data-indgrp="{ind_grp}" data-ind="">
              {_star_cell()}
              <td>
                <a class="sym-tag" style="background:var(--rose-lt);border-color:var(--rose-mid);color:var(--rose)"
                   href="{link}" target="_blank" rel="noopener">{sym}<span class="ib-badge">RS+</span>{_new_star(is_new)}</a>
              </td>
              <td class="r" style="font-family:var(--mono)">{close_s}</td>
              <td class="r"><span class="pill pill-rose">{rs_s}</span></td>
              <td class="r" style="font-family:var(--mono);color:var(--muted)">{prior_rs_s}</td>
              <td class="r" style="font-family:var(--mono);color:var(--emerald);font-weight:600">{delta_s}</td>
              <td class="c" style="font-family:var(--mono);color:var(--subtle);font-size:.72rem">{hist_s}</td>
              <td>{mom_badge}</td>
            </tr>"""

    n_new = sum(1 for _, r in new_rs_df.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known) if n_new_rs > 0 else 0
    n_also_momentum = int(new_rs_df["all_conditions_met"].sum()) if n_new_rs > 0 and "all_conditions_met" in new_rs_df.columns else 0
    hit_rate = f"{100*n_new_rs/universe_size:.2f}%" if universe_size > 0 else "N/A"

    html  = _html_head(f"Alpha Momentum — New RS High — {date_display}",
                       "var(--rose)", "var(--amber)", active="newrshigh", date_str=date_str)
    html += _tv_export_bar(f"tradingview_newrshigh_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--rose)"></div>
      <span class="brand-name">Alpha Momentum · New RS High</span>
    </div>
    <h1>New RS High</h1>
    <p class="hdr-sub">Relative Strength making a fresh {lookback_days}-trading-day high · full NSE universe · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--rose-lt);border-color:var(--rose-mid);color:var(--rose)">🔥 RS at {lookback_days}-day high</span>
      <span class="hdr-badge" style="background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo)">Full universe scan</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--rose-lt);border-color:var(--rose-mid);color:var(--rose)">{date_display}</div>
</header>

<div class="kpi-strip">
  <div class="kpi" style="--accent:var(--rose)">
    <div class="kpi-lbl">New RS Highs</div>
    <div class="kpi-val">{n_new_rs}</div>
    <div class="kpi-hint">of {universe_size} scanned</div>
  </div>
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Hit Rate</div>
    <div class="kpi-val">{hit_rate}</div>
    <div class="kpi-hint">of full universe</div>
  </div>
  <div class="kpi" style="--accent:var(--emerald)">
    <div class="kpi-lbl">Also in Momentum</div>
    <div class="kpi-val">{n_also_momentum}</div>
    <div class="kpi-hint">already clear all 8 conditions</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--rose)">New RS High:</strong>
  Today's RS percentile is at or above the highest it's been over the trailing {lookback_days} trading days —
  relative strength turning up, which often leads price. Some of these stocks won't clear every Minervini
  Trend Template condition yet (see the "Also in Momentum" column) — that's expected, and often the point:
  catching leadership emerging before it's fully confirmed by price.
</div>

<div class="table-sec" style="padding-top:1.1rem">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">New RS High</span>
      <span class="tbl-count tbl-title">[{n_new_rs}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol / industry…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"  data-type="str">Symbol<i class="si"></i></th>
        <th class="r" data-col="close" data-type="num">Close ₹<i class="si"></i></th>
        <th class="r" data-col="rs"    data-type="num">RS %ile Today<i class="si"></i></th>
        <th class="r">Prior {lookback_days}d High</th>
        <th class="r">Δ RS</th>
        <th class="c">History</th>
        <th>Momentum?</th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("New RS High dashboard → %s  (%d stocks)", out_path, n_new_rs)
# ─────────────────────────────────────────────────────────────────────────────
#  STAGE 4 BREAKDOWN DASHBOARD
# ─────────────────────────────────────────────────────────────────────────────

def build_stage4_dashboard(
    stage4_df: pd.DataFrame,
    out_path: Path,
    date_str: str,
    universe_size: int = 0,
    min_market_cap_cr: float = 50_000.0,
    known_symbols: set[str] | None = None,
) -> None:
    """
    `stage4_df` is the already-computed output of
    scanner.stage4_breakdown.compute_stage4_breakdown() — large-cap stocks
    (>= min_market_cap_cr) currently trading below their 50-day MA. The
    broader of the two weakness dashboards (the site's single weakness/risk-radar dashboard.
    """
    date_display = datetime.strptime(date_str, "%Y%m%d").strftime("%d %b %Y")
    known = known_symbols or set()

    n_stage4 = len(stage4_df) if stage4_df is not None else 0

    if n_stage4 == 0:
        rows_html = f'<tr><td colspan="8" class="no-data">No large-caps (≥ ₹{min_market_cap_cr:,.0f} Cr) are currently below their 50-day MA.</td></tr>'
    else:
        rows_html = ""
        for _, row in stage4_df.iterrows():
            sym = _display_symbol(row.get("symbol", ""), row.get("name"))
            link      = _tv_link(row.get("symbol", sym))
            is_new    = sym not in known
            new_cls   = " is-new" if is_new else ""
            close     = row.get("close", np.nan)
            ma50      = row.get("MA50", np.nan)
            pct_below = row.get("pct_below_ma50", np.nan)
            rs        = row.get("rs_percentile", np.nan)
            tmc       = row.get("total_market_cap_cr", np.nan)
            ind_grp   = str(row.get("industry_group", "")) or "—"

            close_s     = f"₹{float(close):,.2f}" if _safe(close) else "N/A"
            ma50_s      = f"₹{float(ma50):,.2f}"   if _safe(ma50)  else "N/A"
            pct_below_s = f"{float(pct_below):.1f}%" if _safe(pct_below) else "N/A"
            rs_s        = f"{float(rs):.1f}"       if _safe(rs)    else "N/A"
            tmc_s       = fmt_cr(tmc)

            rows_html += f"""
            <tr class="srow{new_cls}"
              data-sym="{sym}" data-close="{_r(close)}" data-rs="{_r(rs)}"
              data-tmc="{_r(tmc)}" data-indgrp="{ind_grp}" data-ind="">
              {_star_cell()}
              <td>
                <a class="sym-tag" style="background:var(--red-lt);border-color:var(--red-mid);color:var(--red)"
                   href="{link}" target="_blank" rel="noopener">{sym}<span class="ib-badge">S4</span>{_new_star(is_new)}</a>
              </td>
              <td class="r" style="font-family:var(--mono)">{close_s}</td>
              <td class="r" style="font-family:var(--mono);color:var(--muted)">{ma50_s}</td>
              <td class="r"><span class="pill pill-red">{pct_below_s}</span></td>
              <td class="r"><span class="pill pill-amber">{rs_s}</span></td>
              <td class="r" style="font-family:var(--mono);color:var(--muted);font-size:.77rem">{tmc_s}</td>
              <td style="color:var(--muted);font-size:.78rem">{ind_grp}</td>
            </tr>"""

    n_new = sum(1 for _, r in stage4_df.iterrows()
                if _strip_symbol(r.get("symbol", "")) not in known) if n_stage4 > 0 else 0
    hit_rate = f"{100*n_stage4/universe_size:.2f}%" if universe_size > 0 else "N/A"

    html  = _html_head(f"Alpha Momentum — Stage 4 Breakdown — {date_display}",
                       "var(--red)", "var(--navy)", active="stage4", date_str=date_str)
    html += _tv_export_bar(f"tradingview_stage4_{date_str}.txt")
    html += f"""
<header>
  <div class="hdr-left">
    <div class="brand">
      <div class="brand-dot" style="background:var(--red)"></div>
      <span class="brand-name">Alpha Momentum · Stage 4 Breakdown</span>
    </div>
    <h1>Stage 4 Breakdown</h1>
    <p class="hdr-sub">Large-caps (≥ ₹{min_market_cap_cr:,.0f} Cr) currently trading below their 50-day moving average · {date_display}</p>
    <div class="badge-row">
      <span class="hdr-badge" style="background:var(--red-lt);border-color:var(--red-mid);color:var(--red)">📉 Below 50-day MA</span>
      <span class="hdr-badge" style="background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo)">Full universe scan</span>
      {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='hdr-badge' style='cursor:pointer;text-decoration:none;background:var(--new-bg);border-color:var(--new-border);color:var(--new-text)'>✦ " + str(n_new) + " New Stocks &rarr;</a>" if n_new else ""}
    </div>
  </div>
  <div class="date-pill" style="background:var(--red-lt);border-color:var(--red-mid);color:var(--red)">{date_display}</div>
</header>

<div class="kpi-strip">
  <div class="kpi" style="--accent:var(--red)">
    <div class="kpi-lbl">Below 50-day MA</div>
    <div class="kpi-val">{n_stage4}</div>
    <div class="kpi-hint">of {universe_size} scanned</div>
  </div>
  <div class="kpi" style="--accent:var(--navy)">
    <div class="kpi-lbl">Market Cap Floor</div>
    <div class="kpi-val" style="font-size:1rem">₹{min_market_cap_cr:,.0f} Cr</div>
    <div class="kpi-hint">large/mega-cap only</div>
  </div>
  <div class="kpi" style="--accent:var(--indigo)">
    <div class="kpi-lbl">Hit Rate</div>
    <div class="kpi-val">{hit_rate}</div>
    <div class="kpi-hint">of full universe</div>
  </div>
  {"<a href='#newStocksSection' onclick='scrollToNewStocks(event)' class='kpi' style='--accent:var(--new-text);cursor:pointer;text-decoration:none;display:block'><div class='kpi-lbl'>New Appearances</div><div class='kpi-val'>" + str(n_new) + "</div><div class='kpi-hint'>first time in 10 days</div></a>" if n_new else ""}
</div>

<div class="callout">
  <strong style="color:var(--red)">Stage 4 Breakdown:</strong>
  Large-caps (≥ ₹{min_market_cap_cr:,.0f} Cr) whose price is currently below the 50-day moving
  average — no "recently in Momentum" requirement, no RS filter — just the cap floor plus the MA50 break.
</div>

<div class="table-sec" style="padding-top:1.1rem">
  <div class="tbl-head">
    <div>
      <span class="tbl-title">Stage 4 Breakdown</span>
      <span class="tbl-count tbl-title">[{n_stage4}]</span>
    </div>
    <div class="controls">
      <div class="legend-row">
        <div class="leg"><div class="leg-dot" style="background:var(--new-border)"></div>✦ New (10-day)</div>
      </div>
      <input class="search" id="searchInput" type="text"
             placeholder="Search symbol / industry…" oninput="filterRows()"/>
    </div>
  </div>
  <div class="tbl-outer">
    <table id="mainTable">
      <thead><tr>
        <th class="star-th" title="Watchlist"></th>
        <th data-col="sym"  data-type="str">Symbol<i class="si"></i></th>
        <th class="r" data-col="close" data-type="num">Close ₹<i class="si"></i></th>
        <th class="r">MA50 ₹</th>
        <th class="r">% Below MA50</th>
        <th class="r" data-col="rs" data-type="num">RS %ile<i class="si"></i></th>
        <th class="r" data-col="tmc" data-type="num">Mkt Cap<i class="si"></i></th>
        <th data-col="indgrp" data-type="str">Industry Group<i class="si"></i></th>
      </tr></thead>
      <tbody id="tableBody">{rows_html}</tbody>
    </table>
  </div>
</div>

{_NEW_STOCKS_SECTION_HTML}

<footer>Data: NSE India &amp; Yahoo Finance · Generated {date_display} · For informational purposes only · Not financial advice</footer>

<script>
{_FILTER_JS}
{_TABLE_SORT_JS}
{_NEW_STOCKS_JS}
{_TV_EXPORT_JS}
</script>
</body></html>"""

    out_path.write_text(html, encoding="utf-8")
    logger.info("Stage 4 Breakdown dashboard → %s  (%d stocks)", out_path, n_stage4)


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN INDEX PAGE
# ─────────────────────────────────────────────────────────────────────────────

def build_main_index(
    passing_path="passing_dashboard.html",
    elite_path="elite_dashboard.html",
    volume_path="volume_action_dashboard.html",
    rocket_path="rocket_dashboard.html",
    out_path="index.html"
):
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Alpha Momentum Dashboard</title>
<link href="{_GOOGLE_FONTS}" rel="stylesheet"/>
<style>
:root {{
  --bg:#ffffff; --surface:#fff; --border:#e5e8f0;
  --text:#0d1426; --muted:#5b6178;
  --sans:'Outfit',system-ui,-apple-system,sans-serif; --mono:'DM Mono','SF Mono','Courier New',monospace;
}}
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--text);font-family:var(--sans);min-height:100vh;padding:2.5rem}}
.topbar{{height:3px;background:linear-gradient(90deg,#0f1b3d 0%,#4f46e5 55%,#059669 100%);margin:-2.5rem -2.5rem 2rem;}}
h1{{font-size:1.7rem;font-weight:700;letter-spacing:-.03em;margin-bottom:.35rem}}
.sub{{color:var(--muted);font-size:.85rem;margin-bottom:2rem;font-family:var(--mono)}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:1rem}}
.card{{background:var(--surface);border:1px solid var(--border);border-radius:12px;
       padding:1.4rem 1.6rem;position:relative;overflow:hidden;transition:box-shadow .15s}}
.card:hover{{box-shadow:0 4px 20px rgba(0,0,0,.07)}}
.card::before{{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--c)}}
.card-icon{{font-size:1.4rem;margin-bottom:.6rem}}
.card h2{{font-size:1rem;font-weight:700;letter-spacing:-.01em;margin-bottom:.3rem}}
.card p{{font-size:.78rem;color:var(--muted);margin-bottom:1rem;line-height:1.5}}
.card a{{display:inline-flex;align-items:center;gap:.35rem;padding:.38rem .95rem;
         font-family:var(--mono);font-size:.72rem;font-weight:500;border-radius:8px;
         border:1px solid;text-decoration:none;transition:background .14s}}

@media (max-width: 600px) {{
  body{{padding:1.2rem}}
  .topbar{{margin:-1.2rem -1.2rem 1.4rem}}
  h1{{font-size:1.35rem}}
  .grid{{grid-template-columns:1fr;gap:.85rem}}
  .card{{padding:1.1rem 1.25rem}}
}}
</style>
</head>
<body>
<div class="topbar"></div>
<h1>Alpha Momentum</h1>
<p class="sub">// NSE Minervini Trend Scanner · India</p>
<div class="grid">
  <div class="card" style="--c:#4f46e5">
    <div class="card-icon">📊</div>
    <h2>Passing Stocks</h2>
    <p>All 8 Minervini conditions met — the core universe of momentum candidates.</p>
    <a href="{passing_path}" style="background:#eef0fd;border-color:#c7d2fe;color:#4f46e5">
      Open Dashboard →
    </a>
  </div>
  <div class="card" style="--c:#059669">
    <div class="card-icon">⭐</div>
    <h2>Elite Stocks</h2>
    <p>All 8 conditions + Close above EMA10 — highest-quality momentum stocks.</p>
    <a href="{elite_path}" style="background:#ecfdf5;border-color:#a7f3d0;color:#059669">
      Open Dashboard →
    </a>
  </div>
  <div class="card" style="--c:#2563eb">
    <div class="card-icon">📈</div>
    <h2>Volume Action</h2>
    <p>Pocket pivot volume events — institutional accumulation signals.</p>
    <a href="{volume_path}" style="background:#eff6ff;border-color:#bfdbfe;color:#2563eb">
      Open Dashboard →
    </a>
  </div>
  <div class="card" style="--c:#d97706">
    <div class="card-icon">🚀</div>
    <h2>Rocket Stocks</h2>
    <p>All 8 conditions + Inside Bar — price coiling for potential breakout.</p>
    <a href="{rocket_path}" style="background:#fffbeb;border-color:#fde68a;color:#d97706">
      Open Dashboard →
    </a>
  </div>
</div>
</body></html>"""

    Path(out_path).write_text(html, encoding="utf-8")
    logger.info("Main index page → %s", out_path)


# ─────────────────────────────────────────────────────────────────────────────
#  HOMEPAGE WIDGET — Industry Group → Industry → Stock (nested accordion)
# ─────────────────────────────────────────────────────────────────────────────
#
# Renders an interactive, self-contained fragment (CSS + markup + a single
# tiny shared toggle script) meant to be embedded into docs/index.html (the
# GitHub Pages landing page). For TODAY's Momentum (8-condition passing)
# universe it shows:
#
#   • Every Industry Group, sorted descending by stock count, as an
#     expand/collapse row — exactly the same accordion pattern already used
#     by the "Scan History" month groups further down the page.
#   • Expanding a group reveals its Industries, sorted descending by stock
#     count, each its own (nested) expand/collapse row.
#   • Expanding an industry reveals the stock list (symbol / close / RS),
#     where clicking a symbol opens that stock on TradingView.
#
# Everything is rendered server-side (no client-side data/JSON, no fetch) —
# the whole tree is plain HTML with CSS max-height transitions, just like the
# month-wise history section, so opening/closing feels identical site-wide.


def _indv_stock_row(rec: dict) -> str:
    close_s = f"₹{rec['c']:,.2f}" if rec.get("c") is not None else "N/A"
    rs_s    = f"{rec['r']:.1f}" if rec.get("r") is not None else "N/A"
    tv_link = _tv_link(rec["s"] + ".NS")
    sym_esc = html.escape(rec["s"])
    return f"""
        <tr>
          <td><a class="indv-sym" href="{tv_link}" target="_blank" rel="noopener">{sym_esc} &#8599;</a></td>
          <td class="r" style="font-family:var(--mono)">{close_s}</td>
          <td class="r"><span class="indv-pill">{rs_s}</span></td>
        </tr>"""


def build_industry_drilldown(
    passing: "pd.DataFrame",
    date_display: str,
    dashboard_link: str | None = None,
) -> str:
    """
    Build the homepage "Industry Group → Industry → Stock" nested-accordion
    widget from TODAY's Momentum (8-condition passing) stocks.

    Returns a self-contained HTML fragment (markup + scoped CSS + a tiny
    shared toggle script) ready to be dropped into docs/index.html. Visually
    and interactively it mirrors the "Scan History" month-wise accordion
    already on that page — same expand/collapse mechanics, same fonts and
    color tokens, same pill/sym-tag components — so it reads as one more
    section of the same product rather than a bolted-on widget.
    """
    records: list[dict] = []
    if passing is not None and not passing.empty:
        for _, row in passing.iterrows():
            sym = _display_symbol(row.get("symbol", ""), row.get("name"))
            if not sym:
                continue
            grp   = str(row.get("industry_group") or "").strip() or "Unclassified"
            ind   = str(row.get("industry") or "").strip() or "Unclassified"
            close = row.get("close", np.nan)
            rs    = row.get("rs_percentile", np.nan)
            records.append({
                "s": sym,
                "g": grp,
                "i": ind,
                "c": round(float(close), 2) if _safe(close) else None,
                "r": round(float(rs), 1) if _safe(rs) else None,
            })

    n_stocks = len(records)

    dash_btn = (
        f'<a href="{dashboard_link}" class="btn-link green">📊 Full Momentum Dashboard</a>'
        if dashboard_link else ""
    )

    if n_stocks == 0:
        subtitle = f"No passing stocks yet for {date_display}."
        return f"""
<div class="indv-section" id="indv-section">
  <div class="indv-titlebar">
    <div class="indv-titlewrap">
      <div class="indv-eyebrow" style="color:var(--blue)"><span class="indv-dot" style="background:var(--blue);box-shadow:0 0 0 3px var(--blue-lt)"></span>MOMENTUM UNIVERSE</div>
      <h2 class="indv-heading">Industry Breakdown</h2>
      <p class="indv-sub">{subtitle}</p>
    </div>
    {dash_btn}
  </div>
  <div class="indv-card">
    <div class="indv-empty">No stocks are currently passing the Momentum scan &mdash; check back after the next run.</div>
  </div>
</div>
{_INDV_STYLE}
{_INDV_SCRIPT}
"""

    # ── Group stocks by Industry Group → Industry ─────────────────────────────
    groups: "OrderedDict[str, list[dict]]" = OrderedDict()
    for r in records:
        groups.setdefault(r["g"], []).append(r)
    group_items = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    n_groups = len(group_items)
    top_group, top_group_recs = group_items[0]
    top_group_disp = top_group if len(top_group) <= 26 else top_group[:24] + "…"

    groups_html_parts: list[str] = []
    for idx, (grp, recs) in enumerate(group_items):
        industries: "OrderedDict[str, list[dict]]" = OrderedDict()
        for r in recs:
            industries.setdefault(r["i"], []).append(r)
        industry_items = sorted(industries.items(), key=lambda kv: (-len(kv[1]), kv[0]))

        industries_html_parts: list[str] = []
        for jdx, (ind, irecs) in enumerate(industry_items):
            irecs_sorted = sorted(
                irecs,
                key=lambda d: (-(d["r"] if d["r"] is not None else -1.0), d["s"]),
            )
            rows = "".join(_indv_stock_row(d) for d in irecs_sorted)
            ind_esc = html.escape(ind)
            industries_html_parts.append(f"""
      <div class="indv-ind">
        <button class="indv-ind-acc" onclick="toggleAccordion(this)" aria-expanded="false">
          <span class="indv-ind-rank">{jdx + 1:02d}</span>
          <span class="indv-ind-label">{ind_esc}</span>
          <span class="indv-ind-meta">{len(irecs)} stock{'s' if len(irecs) != 1 else ''}</span>
          <span class="indv-ind-chev">&#8963;</span>
        </button>
        <div class="indv-ind-body">
          <div class="indv-table-wrap">
            <table class="indv-table">
              <thead><tr><th>Symbol</th><th class="r">Close ₹</th><th class="r">RS %ile</th></tr></thead>
              <tbody>{rows}
              </tbody>
            </table>
          </div>
        </div>
      </div>""")

        is_first  = idx == 0
        grp_esc   = html.escape(grp)
        groups_html_parts.append(f"""
  <div class="indv-group">
    <button class="indv-acc" onclick="toggleAccordion(this)" aria-expanded="{'true' if is_first else 'false'}">
      <span class="indv-acc-rank">{idx + 1:02d}</span>
      <span class="indv-acc-label">{grp_esc}</span>
      <span class="indv-acc-meta">{len(recs)} stock{'s' if len(recs) != 1 else ''}</span>
      <span class="indv-acc-chevron">&#8963;</span>
    </button>
    <div class="indv-body{' open' if is_first else ''}">
      <div class="indv-industries">{''.join(industries_html_parts)}
      </div>
    </div>
  </div>""")

    subtitle = (
        f"{n_stocks} stock{'s' if n_stocks != 1 else ''} passing today&rsquo;s Minervini scan "
        f"across {n_groups} industry group{'s' if n_groups != 1 else ''} "
        f"&middot; click a group to expand it, click a symbol to open TradingView"
    )

    return f"""
<div class="indv-section" id="indv-section">
  <div class="indv-titlebar">
    <div class="indv-titlewrap">
      <div class="indv-eyebrow" style="color:var(--blue)"><span class="indv-dot" style="background:var(--blue);box-shadow:0 0 0 3px var(--blue-lt)"></span>MOMENTUM UNIVERSE</div>
      <h2 class="indv-heading">Industry Breakdown</h2>
      <p class="indv-sub">{subtitle}</p>
    </div>
    {dash_btn}
  </div>

  <div class="indv-card">
    <div class="indv-kpis">
      <div class="indv-kpi" style="--accent:var(--indigo);--accent-lt:var(--indigo-lt);--accent-mid:var(--indigo-mid)">
        <div class="indv-kpi-lbl">Stocks Passing</div>
        <div class="indv-kpi-val">{n_stocks}</div>
        <div class="indv-kpi-hint">{date_display}</div>
      </div>
      <div class="indv-kpi" style="--accent:var(--blue);--accent-lt:var(--blue-lt);--accent-mid:var(--blue-mid)">
        <div class="indv-kpi-lbl">Industry Groups</div>
        <div class="indv-kpi-val">{n_groups}</div>
        <div class="indv-kpi-hint">represented today</div>
      </div>
      <div class="indv-kpi" style="--accent:var(--emerald);--accent-lt:var(--emerald-lt);--accent-mid:var(--emerald-mid)">
        <div class="indv-kpi-lbl">Top Group</div>
        <div class="indv-kpi-val" title="{html.escape(top_group)}">{html.escape(top_group_disp)}</div>
        <div class="indv-kpi-hint">{len(top_group_recs)} stock{'s' if len(top_group_recs) != 1 else ''}</div>
      </div>
    </div>
    <div class="indv-body-outer">
      <div class="indv-groups">{''.join(groups_html_parts)}
      </div>
    </div>
  </div>
</div>
{_INDV_STYLE}
{_INDV_SCRIPT}
"""


_INDV_STYLE = """
<style>
.indv-section{max-width:1120px;margin:0 auto 2rem;padding:0 1.5rem;}
.indv-titlebar{display:flex;justify-content:space-between;align-items:flex-start;gap:1.25rem;flex-wrap:wrap;margin-bottom:1rem;}
.indv-titlewrap{min-width:220px;flex:1;}
.indv-eyebrow{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);font-size:.62rem;
              font-weight:700;letter-spacing:.14em;color:var(--indigo);margin-bottom:.4rem;}
.indv-dot{width:6px;height:6px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-lt);}
.indv-heading{font-family:var(--sans);font-size:1.25rem;font-weight:700;letter-spacing:-.02em;
              color:var(--text);margin-bottom:.3rem;}
.indv-sub{font-family:var(--sans);font-size:.8rem;color:var(--muted);max-width:640px;line-height:1.55;}

.indv-card{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
           box-shadow:var(--shadow-sm);overflow:hidden;}

.indv-kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));border-bottom:1px solid var(--border);}
.indv-kpi{padding:.95rem 1.35rem;border-right:1px solid var(--border);position:relative;min-width:0;
          background:linear-gradient(120deg,var(--accent-mid) -60%,var(--accent-lt) 40%,var(--surface) 100%);
          border-left:3px solid var(--accent);}
.indv-kpi:first-child{border-left:none;}
.indv-kpi:last-child{border-right:none;}
.indv-kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:3px;background:var(--accent);}
.indv-kpi-lbl{font-family:var(--mono);font-size:.6rem;font-weight:600;text-transform:uppercase;
              letter-spacing:.1em;color:var(--muted);margin-bottom:.35rem;}
.indv-kpi-val{font-family:var(--sans);font-size:1.15rem;font-weight:700;letter-spacing:-.02em;
              color:var(--accent);line-height:1.2;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.indv-kpi-hint{font-family:var(--mono);font-size:.64rem;color:var(--subtle);margin-top:.25rem;}

.indv-body-outer{padding:1.25rem 1.4rem 1.4rem;}
.indv-empty{text-align:center;padding:2.4rem 1rem;color:var(--muted);font-size:.83rem;font-family:var(--mono);}

/* ── Top-level: Industry Group accordion (mirrors .month-accordion) ── */
.indv-groups{display:flex;flex-direction:column;gap:.7rem;}
.indv-acc{
  width:100%;display:flex;align-items:center;gap:.75rem;
  background:var(--surface);border:1px solid var(--border);border-radius:10px;
  padding:.85rem 1.2rem;cursor:pointer;
  font-family:var(--sans);font-size:.92rem;font-weight:600;color:var(--text);
  letter-spacing:-.01em;text-align:left;
  transition:background .15s,box-shadow .15s;
  box-shadow:var(--shadow-sm);
}
.indv-acc:hover{background:var(--surface-2,#fbfbfe);box-shadow:var(--shadow-md);}
.indv-acc[aria-expanded="true"]{
  border-bottom-left-radius:0;border-bottom-right-radius:0;
  border-bottom-color:transparent;
  background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo);
}
.indv-acc-rank{flex-shrink:0;width:22px;height:22px;border-radius:6px;background:rgba(15,23,42,.05);
               display:flex;align-items:center;justify-content:center;
               font-family:var(--mono);font-size:.62rem;font-weight:700;}
.indv-acc[aria-expanded="true"] .indv-acc-rank{background:rgba(255,255,255,.65);}
.indv-acc-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.indv-acc-meta{font-family:var(--mono);font-size:.67rem;font-weight:500;color:var(--subtle);letter-spacing:.04em;white-space:nowrap;}
.indv-acc[aria-expanded="true"] .indv-acc-meta{color:var(--indigo);opacity:.75;}
.indv-acc-chevron{font-size:.8rem;transition:transform .3s cubic-bezier(.4,0,.2,1);display:inline-block;flex-shrink:0;}
.indv-acc[aria-expanded="false"] .indv-acc-chevron{transform:rotate(180deg);}

.indv-body{
  overflow:hidden;max-height:0;opacity:0;
  transition:max-height .38s cubic-bezier(.4,0,.2,1),opacity .28s ease;
  border:1px solid transparent;border-top:none;
  border-bottom-left-radius:10px;border-bottom-right-radius:10px;
}
.indv-body.open{max-height:6000px;opacity:1;border-color:var(--indigo-mid);}
.indv-industries{padding:.9rem 1.1rem 1.05rem;display:flex;flex-direction:column;gap:.55rem;background:var(--surface);}

/* ── Nested: Industry accordion (same mechanics, smaller/indented) ── */
.indv-ind{border-left:2px solid var(--indigo-mid);padding-left:.7rem;}
.indv-ind-acc{
  width:100%;display:flex;align-items:center;gap:.6rem;
  background:var(--surface-2,#fbfbfe);border:1px solid var(--border);border-radius:8px;
  padding:.6rem .9rem;cursor:pointer;
  font-family:var(--sans);font-size:.82rem;font-weight:600;color:var(--text);
  letter-spacing:-.005em;text-align:left;
  transition:background .15s,box-shadow .15s;
}
.indv-ind-acc:hover{box-shadow:var(--shadow-sm);}
.indv-ind-acc[aria-expanded="true"]{
  background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo);
  border-bottom-left-radius:0;border-bottom-right-radius:0;border-bottom-color:transparent;
}
.indv-ind-rank{flex-shrink:0;width:18px;height:18px;border-radius:5px;background:var(--border);
               display:flex;align-items:center;justify-content:center;
               font-family:var(--mono);font-size:.58rem;font-weight:700;color:var(--muted);}
.indv-ind-acc[aria-expanded="true"] .indv-ind-rank{background:#fff;color:var(--indigo);}
.indv-ind-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.indv-ind-meta{font-family:var(--mono);font-size:.63rem;color:var(--subtle);white-space:nowrap;}
.indv-ind-acc[aria-expanded="true"] .indv-ind-meta{color:var(--indigo);opacity:.75;}
.indv-ind-chev{font-size:.7rem;transition:transform .3s cubic-bezier(.4,0,.2,1);flex-shrink:0;}
.indv-ind-acc[aria-expanded="false"] .indv-ind-chev{transform:rotate(180deg);}

.indv-ind-body{
  overflow:hidden;max-height:0;opacity:0;
  transition:max-height .34s cubic-bezier(.4,0,.2,1),opacity .24s ease;
  border:1px solid transparent;border-top:none;
  border-bottom-left-radius:8px;border-bottom-right-radius:8px;
}
.indv-ind-body.open{max-height:4000px;opacity:1;border-color:var(--indigo-mid);}

/* ── Stock table (leaf level) ── */
.indv-table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch;}
table.indv-table{width:100%;border-collapse:collapse;min-width:340px;background:var(--surface);}
.indv-table thead th{font-family:var(--mono);font-size:.62rem;font-weight:700;text-transform:uppercase;
                     letter-spacing:.1em;color:var(--subtle);padding:.65rem .9rem;text-align:left;
                     background:var(--surface-2,#fbfbfe);border-bottom:1px solid var(--border);}
.indv-table td{padding:.65rem .9rem;border-bottom:1px solid var(--border);font-size:.83rem;color:var(--text);}
.indv-table tr:last-child td{border-bottom:none;}
.indv-table tr:hover td{background:var(--indigo-lt);}
th.r,td.r{text-align:right;}

.indv-sym{display:inline-flex;align-items:center;gap:.35rem;font-family:var(--mono);font-weight:500;
          font-size:.72rem;padding:.2rem .6rem;border-radius:6px;letter-spacing:.05em;border:1px solid;
          background:var(--indigo-lt);border-color:var(--indigo-mid);color:var(--indigo);
          text-decoration:none;transition:filter .12s;}
.indv-sym:hover{filter:brightness(.93);}
.indv-pill{display:inline-block;font-family:var(--mono);font-size:.7rem;font-weight:500;padding:.18rem .55rem;
           border-radius:999px;border:1px solid;background:var(--amber-lt);border-color:var(--amber-mid);color:var(--amber);}

@media (max-width:768px){
  .indv-section{padding:0 1rem;}
  .indv-heading{font-size:1.1rem;}
  .indv-body-outer{padding:1rem 1.05rem 1.15rem;}
  .indv-kpi{padding:.8rem 1rem;}
  .indv-kpi-val{font-size:1.02rem;}
  .indv-acc{padding:.75rem .95rem;font-size:.85rem;}
  .indv-ind-acc{padding:.55rem .75rem;font-size:.78rem;}
}
</style>
"""

_INDV_SCRIPT = """
<script>
if (typeof window.toggleAccordion !== 'function') {
  window.toggleAccordion = function(btn) {
    var body = btn.nextElementSibling;
    var open = body.classList.contains('open');
    body.classList.toggle('open', !open);
    btn.setAttribute('aria-expanded', String(!open));
  };
}
</script>
"""


# ─────────────────────────────────────────────────────────────────────────────
#  HOMEPAGE WIDGET — Chartink screener embed
# ─────────────────────────────────────────────────────────────────────────────
#
# Embeds an external Chartink dashboard (e.g. https://chartink.com/dashboard/<id>)
# inline via <iframe>. Note: Chartink may send X-Frame-Options / CSP headers
# that block third-party framing — if the iframe fails to render in the
# user's browser, the fallback "Open in Chartink" link/button still works.

def build_chartink_embed(
    chartink_url: str = "https://chartink.com/dashboard/160633",
    title: str = "Chartink Screener",
) -> str:
    """
    Build a self-contained HTML fragment linking out to a Chartink dashboard.
    Chartink sends X-Frame-Options / CSP headers that block iframing on other
    domains, so rather than a blank/broken iframe box, this renders a styled
    launch card that opens the live screener in a new tab.
    """
    safe_url = html.escape(chartink_url, quote=True)
    return f"""
<div class="chartink-section" id="chartink-section">
  <a href="{safe_url}" target="_blank" rel="noopener" class="chartink-card">
    <div class="chartink-cardtext">
      <div class="chartink-eyebrow"><span class="chartink-dot"></span>LIVE SCREENER</div>
      <h2 class="chartink-heading">{html.escape(title)}</h2>
      <p class="chartink-sub">Opens the live Chartink screener in a new tab — Chartink doesn't allow embedding its dashboards inline, so this links straight through.</p>
    </div>
    <div class="chartink-cta">Open in Chartink&nbsp;↗</div>
  </a>
</div>
{_CHARTINK_STYLE}
"""


_CHARTINK_STYLE = """
<style>
.chartink-section{max-width:1120px;margin:0 auto 2.2rem;padding:0 1.5rem;}
.chartink-card{display:flex;justify-content:space-between;align-items:center;gap:1.5rem;flex-wrap:wrap;
              border-radius:14px;border:1px solid var(--border);background:var(--surface);
              box-shadow:var(--shadow-sm);padding:1.5rem 1.75rem;text-decoration:none;
              transition:border-color .15s ease, box-shadow .15s ease;}
.chartink-card:hover{border-color:var(--indigo-mid);box-shadow:0 2px 10px rgba(79,70,229,.12);}
.chartink-cardtext{min-width:220px;flex:1;}
.chartink-eyebrow{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);font-size:.62rem;
              font-weight:700;letter-spacing:.14em;color:var(--indigo);margin-bottom:.4rem;}
.chartink-dot{width:6px;height:6px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-lt);}
.chartink-heading{font-size:1.2rem;font-weight:700;margin:0 0 .3rem;color:var(--text);}
.chartink-sub{font-size:.85rem;color:var(--muted);margin:0;max-width:640px;line-height:1.5;}
.chartink-cta{flex-shrink:0;font-weight:700;font-size:.85rem;padding:.6rem 1.1rem;border-radius:999px;
              background:var(--indigo-lt);border:1px solid var(--indigo-mid);color:var(--indigo);white-space:nowrap;}
.chartink-card:hover .chartink-cta{background:var(--indigo);color:#fff;border-color:var(--indigo);}

@media (max-width:768px){
  .chartink-section{padding:0 1rem;}
  .chartink-card{padding:1.25rem 1.4rem;}
  .chartink-heading{font-size:1.05rem;}
}
</style>
"""

# ─────────────────────────────────────────────────────────────────────────────
#  HOMEPAGE WIDGET — EMA-Based Allocation Model (Nifty 50 / Midcap Select / Smallcap 100)
# ─────────────────────────────────────────────────────────────────────────────
#
# Visualizes scanner.ema_allocation.compute_ema_allocation_all() output as a
# table: CMP vs EMA21/50/100, the six ±1 scoring rules, total score (-6..+6),
# bullish/neutral/bearish signal, and a suggested allocation %.

_EMA_ALLOC_RULE_LABELS = [
    ("price_above_ema21",  "CMP vs EMA21"),
    ("price_above_ema50",  "CMP vs EMA50"),
    ("price_above_ema100", "CMP vs EMA100"),
    ("ema21_above_ema50",  "EMA21 vs EMA50"),
    ("ema21_above_ema100", "EMA21 vs EMA100"),
    ("ema50_above_ema100", "EMA50 vs EMA100"),
]


def _ema_alloc_history_chart(history: list[dict]) -> str:
    """
    Render the last-N-days history as an inline SVG line chart: allocation %
    (0..100, filled area, indigo→emerald line) with the daily score plotted
    as a secondary dotted line + colored dots (emerald/rose/slate for
    bullish/bearish/neutral), oldest → newest left to right. No JS chart
    library needed — this is a self-contained <svg> so it renders anywhere
    this HTML fragment is dropped in.
    """
    if not history:
        return '<div class="ema-hist-empty">No history available.</div>'

    days = history  # oldest → newest, as returned by compute_ema_allocation_for_index
    n = len(days)

    # ── Layout ──
    W, H = 560, 200
    pad_l, pad_r, pad_t, pad_b = 34, 34, 16, 26
    plot_w = W - pad_l - pad_r
    plot_h = H - pad_t - pad_b

    def x_at(i: int) -> float:
        return pad_l + (i / (n - 1) * plot_w if n > 1 else plot_w / 2)

    # Allocation % → y (0..100 domain, fixed, since it's already a %)
    def y_alloc(pct: float) -> float:
        return pad_t + plot_h * (1 - pct / 100)

    # Score → y (-6..+6 domain, fixed by the model's own scoring range)
    def y_score(score: float) -> float:
        return pad_t + plot_h * (1 - (score + 6) / 12)

    alloc_pts = [(x_at(i), y_alloc(d["allocation_pct"])) for i, d in enumerate(days)]
    score_pts = [(x_at(i), y_score(d["score"])) for i, d in enumerate(days)]

    alloc_path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in alloc_pts)
    alloc_area = (
        alloc_path
        + f" L{alloc_pts[-1][0]:.1f},{pad_t + plot_h:.1f}"
        + f" L{alloc_pts[0][0]:.1f},{pad_t + plot_h:.1f} Z"
    )
    score_path = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in score_pts)

    def score_color(score: int) -> str:
        return "#10b981" if score > 0 else "#dc2626" if score < 0 else "#94a3b8"

    score_dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{score_color(days[i]["score"])}" />'
        for i, (x, y) in enumerate(score_pts)
    )

    # Gridlines + labels for the 0/50/100 allocation axis
    grid = "".join(
        f'<line x1="{pad_l}" y1="{y_alloc(v):.1f}" x2="{W - pad_r}" y2="{y_alloc(v):.1f}" class="ema-chart-grid" />'
        f'<text x="{pad_l - 8}" y="{y_alloc(v) + 3:.1f}" class="ema-chart-axis" text-anchor="end">{v}%</text>'
        for v in (0, 50, 100)
    )

    # X-axis: first / mid / last date labels only, to avoid crowding
    label_idxs = sorted({0, n // 2, n - 1})
    x_labels = "".join(
        f'<text x="{x_at(i):.1f}" y="{H - 6}" class="ema-chart-axis" text-anchor="middle">'
        f'{html.escape(days[i]["date"][5:])}</text>'
        for i in label_idxs
    )

    latest = days[-1]

    return f"""
    <div class="ema-hist-chart-wrap">
      <div class="ema-hist-chart-legend">
        <span><i class="ema-swatch ema-swatch-alloc"></i>Allocation %</span>
        <span><i class="ema-swatch ema-swatch-score"></i>Score (-6..+6)</span>
        <span class="ema-hist-chart-latest">Latest: {latest['allocation_pct']}% · score {latest['score']:+d} ({html.escape(latest['date'])})</span>
      </div>
      <svg viewBox="0 0 {W} {H}" class="ema-hist-chart" role="img"
           aria-label="Allocation % and score over the past {n} trading days">
        <defs>
          <linearGradient id="emaAllocFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#6366f1" stop-opacity="0.28" />
            <stop offset="100%" stop-color="#10b981" stop-opacity="0.03" />
          </linearGradient>
        </defs>
        {grid}
        <path d="{alloc_area}" fill="url(#emaAllocFill)" stroke="none" />
        <path d="{alloc_path}" class="ema-chart-line-alloc" />
        <path d="{score_path}" class="ema-chart-line-score" />
        {score_dots}
        {x_labels}
      </svg>
    </div>"""


def _ema_alloc_row(row: dict) -> str:
    name = html.escape(row["name"])

    if not row.get("available"):
        return f"""
        <div class="ema-row ema-row-na">
          <div class="ema-col ema-col-name">{name}</div>
          <div class="ema-col ema-col-na">Data unavailable</div>
        </div>"""

    score = row["score"]
    score_str = f"{score:+d}" if score is not None else "–"
    signal = row["signal"]
    score_cls = {"bullish": "emerald", "bearish": "rose", "neutral": "slate"}.get(signal, "slate")

    alloc = row["allocation_pct"]
    alloc_str = f"{alloc}%" if alloc is not None else "–"

    main_row = f"""
    <div class="ema-row">
      <div class="ema-col ema-col-name">
        <div class="ema-name">{name}</div>
        <div class="ema-asof">as of {html.escape(row.get('as_of') or '–')}</div>
      </div>
      <div class="ema-col ema-col-score">
        <span class="chip {score_cls}">{score_str}</span>
      </div>
      <div class="ema-col ema-col-alloc">
        <div class="ema-alloc-bar-wrap">
          <div class="ema-alloc-bar" style="width:{alloc if alloc is not None else 0}%"></div>
        </div>
        <div class="ema-alloc-pct">{alloc_str}</div>
      </div>
    </div>"""

    history_row = f"""
    <details class="ema-hist-details">
      <summary>Past {len(row.get('history', []))} trading days</summary>
      {_ema_alloc_history_chart(row.get('history', []))}
    </details>"""

    return main_row + history_row


def build_ema_allocation_table(results: dict) -> str:
    """
    Build a self-contained HTML fragment visualizing the EMA-Based Allocation
    Model across the tracked indices (Nifty 50, Nifty Midcap Select,
    Nifty Smallcap 100). `results` is the dict returned by
    scanner.ema_allocation.compute_ema_allocation_all().

    Rendered as flex/grid rows (not a <table>) for the main Index / Score /
    Allocation % line, so the layout can't be overridden by this site's
    global `table {{ width/min-width/white-space }}` rules used elsewhere.
    Expand a row for the past 20 trading days (date / score / allocation %).
    """
    rows_html = "".join(_ema_alloc_row(row) for row in results.values())

    return f"""
<div class="ema-alloc-section" id="ema-alloc-section">
  <div class="ema-alloc-titlebar">
    <div class="ema-eyebrow"><span class="ema-dot"></span>EMA-BASED ALLOCATION MODEL</div>
    <h2 class="ema-heading">Index Allocation Scorecard</h2>
    <p class="ema-sub">Trend-strength score (-6..+6) from CMP vs EMA21/50/100, mapped to a
      suggested capital allocation %. Expand a row for a chart of the past 20 trading days.</p>
  </div>

  <div class="ema-alloc-card">
    <div class="ema-alloc-head">
      <div class="ema-col ema-col-name">Index</div>
      <div class="ema-col ema-col-score">Score</div>
      <div class="ema-col ema-col-alloc">Allocation</div>
    </div>
    {rows_html}
    <div class="ema-alloc-legend">
      Score range -6..+6 from six EMA21/50/100 pairwise rules · Allocation % thresholds are a
      configurable default (see <code>SCORE_ALLOCATION_TABLE</code> in
      <code>scanner/ema_allocation.py</code>), not part of the original slide.
    </div>
  </div>
</div>
{_EMA_ALLOC_STYLE}
"""


_EMA_ALLOC_STYLE = """
<style>
.ema-alloc-section{max-width:640px;margin:0 auto 2.2rem;padding:0 1.5rem;}
.ema-alloc-titlebar{margin-bottom:1rem;}
.ema-eyebrow{display:flex;align-items:center;gap:.45rem;font-family:var(--mono);font-size:.62rem;
              font-weight:700;letter-spacing:.14em;color:var(--indigo);margin-bottom:.4rem;}
.ema-dot{width:6px;height:6px;border-radius:50%;background:var(--emerald);box-shadow:0 0 0 3px var(--emerald-lt);}
.ema-heading{font-size:1.35rem;font-weight:700;margin:0 0 .3rem;color:var(--text);}
.ema-sub{font-size:.85rem;color:var(--muted);margin:0;line-height:1.5;}

.ema-alloc-card{border-radius:14px;border:1px solid var(--border);background:var(--surface);
                box-shadow:var(--shadow-sm);overflow:hidden;}

/* Column layout shared by header + every row — flex, not <table>, so global
   `table {{ width/min-width/white-space }}` rules elsewhere on the site can't
   touch it. */
.ema-alloc-head, .ema-row{
  display:flex;align-items:center;gap:.5rem;
  border-bottom:1px solid var(--border);
}
.ema-alloc-head{
  background:var(--indigo-lt);padding:.7rem .9rem;
  font-size:.66rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);
}
.ema-row{padding:.85rem .9rem;}
.ema-row:hover{background:var(--indigo-lt);}

.ema-col{box-sizing:border-box;}
.ema-col-name{flex:1 1 52%;min-width:0;text-align:left;}
.ema-col-score{flex:0 0 20%;text-align:center;}
.ema-col-alloc{flex:0 0 28%;text-align:center;}
.ema-col-na{flex:1;text-align:center;color:var(--muted);font-style:italic;}

.ema-name{font-weight:700;color:var(--text);font-size:.9rem;}
.ema-asof{font-size:.68rem;color:var(--muted);margin-top:.1rem;}

.chip{display:inline-block;padding:.3rem .7rem;border-radius:999px;font-size:.78rem;font-weight:700;
      font-family:var(--mono);letter-spacing:.02em;white-space:nowrap;}
.chip.sm{padding:.15rem .5rem;font-size:.68rem;}
.chip.emerald{background:var(--emerald-lt);color:var(--emerald);border:1px solid var(--emerald-mid);}
.chip.rose{background:#fee2e2;color:#dc2626;border:1px solid #fecaca;}
.chip.slate{background:var(--indigo-lt);color:var(--muted);border:1px solid var(--border);}

.ema-alloc-bar-wrap{width:100%;max-width:110px;height:6px;border-radius:999px;background:var(--indigo-lt);
                    overflow:hidden;margin:0 auto .3rem;}
.ema-alloc-bar{height:100%;border-radius:999px;background:linear-gradient(90deg,var(--indigo),var(--emerald));}
.ema-alloc-pct{font-family:var(--mono);font-size:.78rem;font-weight:700;color:var(--text);text-align:center;}

.ema-hist-details{border-bottom:1px solid var(--border);background:var(--indigo-lt);}
.ema-hist-details summary{cursor:pointer;list-style:none;padding:.5rem .9rem;font-size:.72rem;
              font-weight:700;color:var(--indigo);user-select:none;}
.ema-hist-details summary::-webkit-details-marker{display:none;}
.ema-hist-details summary::before{content:"▸ ";display:inline-block;transition:transform .15s ease;}
.ema-hist-details[open] summary::before{transform:rotate(90deg);}

.ema-hist-chart-wrap{background:var(--surface);padding:.7rem .9rem .5rem;}
.ema-hist-chart-legend{display:flex;flex-wrap:wrap;gap:.9rem;align-items:center;
              font-size:.68rem;color:var(--muted);margin-bottom:.3rem;}
.ema-hist-chart-legend span{display:flex;align-items:center;gap:.35rem;}
.ema-swatch{width:10px;height:3px;border-radius:2px;display:inline-block;}
.ema-swatch-alloc{background:linear-gradient(90deg,#6366f1,#10b981);}
.ema-swatch-score{background:#94a3b8;}
.ema-hist-chart-latest{margin-left:auto;font-family:var(--mono);font-weight:700;color:var(--text);}

.ema-hist-chart{width:100%;height:auto;display:block;}
.ema-chart-grid{stroke:var(--border);stroke-width:1;stroke-dasharray:2 3;}
.ema-chart-axis{font-family:var(--mono);font-size:8px;fill:var(--muted);}
.ema-chart-line-alloc{fill:none;stroke:url(#emaAllocFill);stroke:#6366f1;stroke-width:2.2;
              stroke-linejoin:round;stroke-linecap:round;}
.ema-chart-line-score{fill:none;stroke:#94a3b8;stroke-width:1.4;stroke-dasharray:3 2;
              stroke-linejoin:round;stroke-linecap:round;}

@media (max-width:768px){
  .ema-hist-chart-latest{margin-left:0;width:100%;}
}

.ema-hist-empty{padding:.6rem .9rem;font-size:.75rem;color:var(--muted);font-style:italic;}

.ema-alloc-legend{padding:.65rem .9rem;font-size:.7rem;color:var(--muted);}
.ema-alloc-legend code{font-family:var(--mono);font-size:.68rem;background:var(--indigo-lt);
                  padding:.05rem .3rem;border-radius:4px;border:1px solid var(--border);}

@media (max-width:768px){
  .ema-alloc-section{padding:0 1rem;}
  .ema-heading{font-size:1.1rem;}
  .ema-col-name{flex-basis:44%;}
  .ema-col-score{flex-basis:24%;}
  .ema-col-alloc{flex-basis:32%;}
}
</style>
"""
