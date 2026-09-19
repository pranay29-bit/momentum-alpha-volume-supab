"""
scanner/kelly_analysis.py
--------------------------
Kelly Criterion position-sizing analysis for booked trades in
`data/journal.csv`.

WHAT IT DOES
------------
For every open position in the journal it estimates:
  • R  — the reward:risk multiple of the trade (reward measured off the
         stop-loss defined risk)
  • f* — the Kelly-optimal fraction of the portfolio to risk on a trade
         of that quality, given an assumed historical win-rate
  • how the CURRENT allocation compares to f*  (under-sized / on-target /
    over-sized)

It then renders a single self-contained HTML dashboard (same visual
language as the other dashboards in this repo) showing:
  • a per-trade Kelly table
  • the bankroll-growth curve g(f) for the portfolio's average trade,
    with the peak ("point of maximum return") and the break-even /
    danger zone ("when to start under-trading") marked on the curve
  • a portfolio-level summary (aggregate Kelly risk budget vs. actual
    risk currently committed)

ASSUMPTIONS (all configurable — see the top of `main()`)
----------------------------------------------------------------
1. WIN_RATE — journal.csv only records currently open trades, not a
   closed-trade history, so there is no way to *measure* your historical
   win-rate from this file alone. Kelly math needs one. Default 40%,
   which is typical for trend/momentum breakout systems (small edge,
   low hit-rate, large winners). Change it to your own tracked win-rate
   for a real answer — everything downstream is driven by this number.
2. Per-trade R is estimated from the columns already in journal.csv:
       risk_pct   = "% Loss (if SL hit)"
       reward_pct = current unrealized "Profit %" if the trade is in
                    profit, otherwise a DEFAULT_TARGET_R multiple of the
                    risk (a still-open trade hasn't told you its reward
                    yet, so we assume your usual target multiple).
   R = reward_pct / risk_pct
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

# ── Tunable assumptions ──────────────────────────────────────────────────
DEFAULT_WIN_RATE = 0.40      # 40% historical hit-rate assumption
DEFAULT_TARGET_R = 2.5       # assumed reward multiple for still-open, non-profitable trades
KELLY_SAFETY_FRACTION = 0.5  # "half-Kelly" — the practical, lower-variance sizing most traders should actually use


# ── Journal parsing ──────────────────────────────────────────────────────
def load_journal(csv_path: str | Path):
    """
    Parses data/journal.csv, which has a 2-row metadata header
    (Portfolio Size, Portfolio wise Risk per Trade) followed by a blank
    row and then the position table.

    Returns (portfolio_size, risk_per_trade_pct, positions_df).
    positions_df may be EMPTY if no trades have been logged yet.
    """
    csv_path = Path(csv_path)
    raw = pd.read_csv(csv_path, header=None)

    def _pct_to_float(v, default):
        try:
            s = str(v).strip().replace("%", "")
            return float(s) / 100.0
        except Exception:
            return default

    def _num(v, default):
        try:
            return float(str(v).replace(",", "").strip())
        except Exception:
            return default

    portfolio_size = _num(raw.iloc[0, 2], 1_000_000)
    risk_per_trade = _pct_to_float(raw.iloc[1, 2], 0.005)

    header_row_idx = None
    for i in range(len(raw)):
        if str(raw.iloc[i, 0]).strip() == "S.No.":
            header_row_idx = i
            break

    if header_row_idx is None:
        return portfolio_size, risk_per_trade, pd.DataFrame()

    body = raw.iloc[header_row_idx + 1:].copy()
    body.columns = raw.iloc[header_row_idx]
    body = body.dropna(how="all")
    body = body[body["Stock Name"].notna()] if "Stock Name" in body.columns else body.iloc[0:0]

    return portfolio_size, risk_per_trade, body.reset_index(drop=True)


def _sample_positions() -> pd.DataFrame:
    """Illustrative sample rows, used ONLY when journal.csv has no real
    positions logged yet, so the report isn't empty. Clearly flagged in
    the output — replace with your real booked trades."""
    return pd.DataFrame([
        {"Stock Name": "SAMPLE_A", "Entry Price": 1000, "Stoploss Price": 920,
         "% Loss (if SL hit)": 8.0, "Allocation as per portfolio": 62500, "Profit %": 18.0},
        {"Stock Name": "SAMPLE_B", "Entry Price": 500, "Stoploss Price": 460,
         "% Loss (if SL hit)": 8.0, "Allocation as per portfolio": 62500, "Profit %": -3.0},
        {"Stock Name": "SAMPLE_C", "Entry Price": 250, "Stoploss Price": 225,
         "% Loss (if SL hit)": 10.0, "Allocation as per portfolio": 100000, "Profit %": 32.0},
        {"Stock Name": "SAMPLE_D", "Entry Price": 3200, "Stoploss Price": 3040,
         "% Loss (if SL hit)": 5.0, "Allocation as per portfolio": 200000, "Profit %": 4.0},
    ])


# ── Kelly math ────────────────────────────────────────────────────────────
def kelly_fraction(win_rate: float, R: float) -> float:
    """f* = W - (1-W)/R . Clipped at 0 (never bet on a negative-edge trade)."""
    if R <= 0 or np.isnan(R):
        return 0.0
    f = win_rate - (1 - win_rate) / R
    return max(0.0, f)


def growth_rate(f: float, win_rate: float, R: float) -> float:
    """g(f) = W*ln(1+R*f) + (1-W)*ln(1-f) — expected log-growth of the
    bankroll per trade, for a fraction f risked. This is the curve whose
    peak IS the Kelly fraction."""
    if f <= -1 / R if R else True:
        pass
    try:
        term1 = win_rate * math.log(1 + R * f) if (1 + R * f) > 0 else -np.inf
        term2 = (1 - win_rate) * math.log(1 - f) if (1 - f) > 0 else -np.inf
        return term1 + term2
    except (ValueError, ZeroDivisionError):
        return -np.inf


def compute_position_kelly(df: pd.DataFrame, win_rate: float,
                            default_target_r: float) -> pd.DataFrame:
    out = df.copy()

    def _f(v, default=np.nan):
        try:
            return float(str(v).replace(",", "").replace("%", "").strip())
        except Exception:
            return default

    out["risk_pct"] = out.get("% Loss (if SL hit)", np.nan).apply(_f)
    out["profit_pct"] = out.get("Profit %", np.nan).apply(_f)
    out["allocation"] = out.get("Allocation as per portfolio", np.nan).apply(_f)

    def _reward_pct(row):
        p = row["profit_pct"]
        r = row["risk_pct"]
        if pd.notna(p) and p > 0:
            return p
        return default_target_r * (r if pd.notna(r) else 8.0)

    out["reward_pct"] = out.apply(_reward_pct, axis=1)
    out["R_multiple"] = out["reward_pct"] / out["risk_pct"].replace(0, np.nan)
    out["kelly_f"] = out["R_multiple"].apply(lambda R: kelly_fraction(win_rate, R))
    out["half_kelly_f"] = out["kelly_f"] * KELLY_SAFETY_FRACTION
    return out


def portfolio_summary(pos_df: pd.DataFrame, portfolio_size: float,
                       risk_per_trade: float, win_rate: float) -> dict:
    n = len(pos_df)
    actual_risk_pct_of_portfolio = (n * risk_per_trade) * 100  # sum of risk already committed, in % of portfolio
    avg_R = float(pos_df["R_multiple"].mean()) if n else 0.0
    avg_kelly = float(pos_df["kelly_f"].mean()) if n else 0.0
    return {
        "n_positions": n,
        "avg_R": avg_R,
        "avg_kelly_pct": avg_kelly * 100,
        "half_kelly_pct": avg_kelly * 100 * KELLY_SAFETY_FRACTION,
        "actual_risk_per_trade_pct": risk_per_trade * 100,
        "actual_total_risk_pct": actual_risk_pct_of_portfolio,
        "recommended_total_risk_pct": avg_kelly * 100 * KELLY_SAFETY_FRACTION * max(n, 1),
    }


# ── HTML report ───────────────────────────────────────────────────────────
_CSS = """
:root{
  --bg:#ffffff; --surface:#ffffff; --surface2:#fbfbfe; --surface3:#f1f3f9;
  --border:#e5e8f0; --text:#1a1c29; --text-dim:#6b7086;
  --indigo:#5b5fef; --emerald:#12b76a; --amber:#f79009; --red:#f04438;
}
*{box-sizing:border-box;}
body{margin:0;background:var(--bg);color:var(--text);
  font-family:'Outfit',system-ui,-apple-system,sans-serif;padding:32px;}
h1{font-size:26px;font-weight:700;margin:0 0 4px;}
h2{font-size:18px;font-weight:600;margin:36px 0 12px;}
p.sub{color:var(--text-dim);margin:0 0 24px;font-size:14px;}
.banner{background:#fff7e6;border:1px solid #f5c453;color:#8a5a00;
  padding:12px 16px;border-radius:10px;font-size:13px;margin-bottom:24px;}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-bottom:28px;}
.card{background:var(--surface2);border:1px solid var(--border);border-radius:14px;padding:16px 18px;}
.card .label{font-size:12px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.04em;}
.card .value{font-size:24px;font-weight:700;margin-top:4px;}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--surface);
  border-radius:12px;overflow:hidden;border:1px solid var(--border);}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--border);}
th{background:var(--surface3);font-weight:600;font-size:11px;text-transform:uppercase;
  letter-spacing:.03em;color:var(--text-dim);}
tr:last-child td{border-bottom:none;}
.tag{display:inline-block;padding:3px 9px;border-radius:100px;font-size:11px;font-weight:600;}
.tag.reduce{background:#fef0f0;color:var(--red);}
.tag.hold{background:#eef2ff;color:var(--indigo);}
.tag.add{background:#e9f9f0;color:var(--emerald);}
.chart-wrap{background:var(--surface);border:1px solid var(--border);border-radius:14px;
  padding:20px;max-width:760px;}
.legend-note{font-size:12px;color:var(--text-dim);margin-top:10px;line-height:1.6;}
.explain{background:var(--surface2);border:1px solid var(--border);border-radius:14px;
  padding:18px 20px;font-size:14px;line-height:1.7;max-width:820px;}
.explain b{color:var(--indigo);}
footer{margin-top:40px;font-size:12px;color:var(--text-dim);}
"""


def _verdict(row) -> str:
    actual = row["allocation"] / row["_portfolio_size"] if row["_portfolio_size"] else 0
    kf = row["kelly_f"]
    if kf <= 0:
        return '<span class="tag reduce">No edge — exit/avoid</span>'
    ratio = actual / kf if kf else 0
    if ratio > 1.5:
        return '<span class="tag reduce">Over-sized — undertrade</span>'
    if ratio < 0.6:
        return '<span class="tag add">Room to add (vs. Kelly)</span>'
    return '<span class="tag hold">Near Kelly-optimal</span>'


def build_report(pos_kelly: pd.DataFrame, portfolio_size: float,
                  risk_per_trade: float, win_rate: float,
                  summary: dict, used_sample: bool) -> str:
    df = pos_kelly.copy()
    df["_portfolio_size"] = portfolio_size
    df["verdict"] = df.apply(_verdict, axis=1)

    rows_html = "\n".join(
        f"<tr><td>{r['Stock Name']}</td>"
        f"<td>{r['risk_pct']:.2f}%</td>"
        f"<td>{r['reward_pct']:.2f}%</td>"
        f"<td>{r['R_multiple']:.2f}R</td>"
        f"<td>{r['kelly_f']*100:.2f}%</td>"
        f"<td>{r['half_kelly_f']*100:.2f}%</td>"
        f"<td>{(r['allocation']/portfolio_size*100 if portfolio_size else 0):.2f}%</td>"
        f"<td>{r['verdict']}</td></tr>"
        for _, r in df.iterrows()
    )

    avg_R = summary["avg_R"] if summary["avg_R"] > 0 else 2.0
    curve_f = [round(x, 3) for x in np.arange(-0.05, 0.91, 0.01)]
    curve_g = [growth_rate(f, win_rate, avg_R) for f in curve_f]
    curve_g = [None if (g is None or math.isinf(g) or math.isnan(g)) else round(g, 5) for g in curve_g]
    peak_f = summary["avg_kelly_pct"] / 100
    danger_f = 2 * peak_f  # beyond 2x Kelly, growth turns negative — classic over-betting line

    banner = ""
    if used_sample:
        banner = ('<div class="banner">⚠️ <b>data/journal.csv has no logged positions yet</b> — '
                   'the table and chart below use 4 illustrative sample trades so you can see how '
                   'the analysis works. Fill in your real booked positions in journal.csv and re-run '
                   'this script for your actual numbers.</div>')

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Kelly Criterion — Position Analysis</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;500;600;700&display=swap">
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>{_CSS}</style></head>
<body>
<h1>Kelly Criterion — Booked Position Analysis</h1>
<p class="sub">Portfolio ₹{portfolio_size:,.0f} · Risk/trade currently {risk_per_trade*100:.2f}% ·
Assumed win-rate {win_rate*100:.0f}% (edit in kelly_analysis.py)</p>
{banner}

<div class="grid">
  <div class="card"><div class="label">Open Positions</div><div class="value">{summary['n_positions']}</div></div>
  <div class="card"><div class="label">Avg Reward:Risk</div><div class="value">{summary['avg_R']:.2f}R</div></div>
  <div class="card"><div class="label">Avg Kelly f*</div><div class="value">{summary['avg_kelly_pct']:.2f}%</div></div>
  <div class="card"><div class="label">Half-Kelly (recommended)</div><div class="value">{summary['half_kelly_pct']:.2f}%</div></div>
  <div class="card"><div class="label">Actual risk / trade</div><div class="value">{summary['actual_risk_per_trade_pct']:.2f}%</div></div>
  <div class="card"><div class="label">Total risk committed</div><div class="value">{summary['actual_total_risk_pct']:.2f}%</div></div>
</div>

<h2>Per-position Kelly sizing</h2>
<table>
<tr><th>Stock</th><th>Risk</th><th>Reward</th><th>R multiple</th>
<th>Kelly f*</th><th>Half-Kelly</th><th>Actual alloc</th><th>Verdict</th></tr>
{rows_html}
</table>

<h2>Point of maximum return &amp; the under-trading zone</h2>
<div class="chart-wrap">
<canvas id="growthChart" height="260"></canvas>
<div class="legend-note">
Curve = expected long-run growth rate of the portfolio g(f) for a trade with the portfolio's
average {avg_R:.2f}R reward:risk at a {win_rate*100:.0f}% win-rate, as the fraction of capital
risked per trade (f) increases.<br>
🟢 <b>Peak (f* = {peak_f*100:.2f}%)</b> — the mathematical point of maximum return. Betting exactly
this much per trade grows the account fastest over the long run.<br>
🟠 <b>Danger line (2×f* = {danger_f*100:.2f}%)</b> — beyond this, growth turns negative. This is
where you should start <b>under-trading</b> — cut size back toward, or below, the peak.
</div>
</div>

<h2>What is the Kelly Criterion? (plain terms)</h2>
<div class="explain">
Think of each trade as a bet with two things attached to it: <b>how often you win</b> (your
win-rate) and <b>how much you make when you win vs. how much you lose when you lose</b> (your
reward:risk, or "R multiple").<br><br>
The Kelly Criterion answers one question: <b>"What fraction of my capital should I risk on this
bet to grow my account as fast as possible, without eventually blowing it up?"</b><br><br>
The formula is simply: <b>f* = W − (1 − W) / R</b><br>
&nbsp;&nbsp;• W = probability of winning (e.g. 0.40 for a 40% win-rate)<br>
&nbsp;&nbsp;• R = reward you make per unit you risk (e.g. 2.5 means you make ₹2.5 for every ₹1 you risk)<br>
&nbsp;&nbsp;• f* = the % of your capital to risk on that trade<br><br>
The intuition: if your edge (W and R combined) is strong, bet bigger — you'll compound faster.
If your edge is weak or negative, Kelly tells you to bet nothing (or short of nothing, meaning
skip the trade). Bet <i>more</i> than f* and you're not adding extra long-run return — you're
adding pure risk, and the growth curve above actually bends back down. That's why professional
Kelly users almost never use full Kelly; using a fraction of it (commonly <b>half-Kelly</b>, shown
in the table) sacrifices a little theoretical growth for a lot less volatility and drawdown, which
matters when your real-world win-rate/R estimates are imperfect — as they always are.
</div>

<footer>Generated by scanner/kelly_analysis.py — edit DEFAULT_WIN_RATE / DEFAULT_TARGET_R at the
top of that file to match your own tracked statistics.</footer>

<script>
new Chart(document.getElementById('growthChart'), {{
  type: 'line',
  data: {{
    labels: {json.dumps([round(f*100,1) for f in curve_f])},
    datasets: [{{
      label: 'Expected growth rate g(f)',
      data: {json.dumps(curve_g)},
      borderColor: '#5b5fef',
      backgroundColor: 'rgba(91,95,239,0.08)',
      fill: true, tension: 0.25, pointRadius: 0, borderWidth: 2.5
    }}]
  }},
  options: {{
    responsive: true,
    plugins: {{
      legend: {{display:false}},
      annotation: undefined
    }},
    scales: {{
      x: {{ title: {{display:true, text:'Capital risked per trade (%)'}} }},
      y: {{ title: {{display:true, text:'Expected log-growth rate'}} }}
    }}
  }}
}});
</script>
</body></html>"""
    return html


def main():
    root = Path(__file__).resolve().parent.parent
    csv_path = root / "data" / "journal.csv"
    portfolio_size, risk_per_trade, positions = load_journal(csv_path)

    used_sample = positions.empty
    if used_sample:
        positions = _sample_positions()

    pos_kelly = compute_position_kelly(positions, DEFAULT_WIN_RATE, DEFAULT_TARGET_R)
    summary = portfolio_summary(pos_kelly, portfolio_size, risk_per_trade, DEFAULT_WIN_RATE)

    html = build_report(pos_kelly, portfolio_size, risk_per_trade,
                         DEFAULT_WIN_RATE, summary, used_sample)

    out_dir = root / "docs" / "kelly"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "kelly_report.html"
    out_path.write_text(html, encoding="utf-8")
    print(f"Kelly report written to {out_path}")
    return out_path


if __name__ == "__main__":
    main()
