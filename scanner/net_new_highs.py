"""
scanner/net_new_highs.py
-------------------------
Net New Highs (NNH) — NSE market-breadth indicator.

Methodology follows Nitin R's "Net New Highs" script
(https://finallynitin.substack.com/p/net-new-highs-script):

  NNH = (# stocks making a new 52-week high today) - (# stocks making a
        new 52-week low today)

  • Bias is BULLISH when NNH stays positive for 3 consecutive sessions,
    BEARISH when it stays negative for 3 consecutive sessions.
  • A 10-day SMA of NNH is also tracked to reduce whipsaws — the smoothed
    bias is bullish/bearish based on the sign of that average.

This module computes today's counts from the scanner's full results
DataFrame (one row per symbol, with `is_52w_high` / `is_52w_low` flags
already computed in data_loader.py), appends them to a small persistent
history CSV so the rolling/SMA bias can be tracked day over day, and
renders an HTML card for the landing page (docs/index.html).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .config import DATA_DIR

logger = logging.getLogger(__name__)

HISTORY_PATH = DATA_DIR / "net_new_highs_history.csv"
SMA_WINDOW   = 10
BIAS_STREAK  = 3


# ── 1. Today's counts ─────────────────────────────────────────────────────────

def compute_today_counts(df: pd.DataFrame) -> dict:
    """
    Count new 52-week highs / lows from the full scan results DataFrame.
    Expects boolean columns `is_52w_high` and `is_52w_low` (added in
    data_loader.py). Returns a dict with new_highs, new_lows, net, total.
    """
    if df is None or df.empty or "is_52w_high" not in df.columns or "is_52w_low" not in df.columns:
        return {"new_highs": 0, "new_lows": 0, "net": 0, "total": 0}

    valid      = df.dropna(subset=["is_52w_high", "is_52w_low"])
    new_highs  = int(valid["is_52w_high"].sum())
    new_lows   = int(valid["is_52w_low"].sum())
    total      = int(len(valid))

    return {
        "new_highs": new_highs,
        "new_lows":  new_lows,
        "net":       new_highs - new_lows,
        "total":     total,
    }


# ── 2. Persistent history (for streak + SMA bias) ────────────────────────────

def _load_history() -> pd.DataFrame:
    if HISTORY_PATH.exists():
        try:
            df = pd.read_csv(HISTORY_PATH)
            df["date"] = pd.to_datetime(df["date"])
            return df
        except Exception as exc:
            logger.warning("Could not read NNH history (%s) — starting fresh.", exc)
    empty = pd.DataFrame(columns=["date", "new_highs", "new_lows", "net", "total"])
    empty["date"] = pd.to_datetime(empty["date"])
    return empty


def _save_history(hist: pd.DataFrame) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    hist.to_csv(HISTORY_PATH, index=False)


def update_history(date_str: str, counts: dict) -> pd.DataFrame:
    """
    Append/overwrite today's row (keyed by date_str, format YYYY-MM-DD) into
    the NNH history CSV and return the updated, date-sorted history.
    """
    hist = _load_history()
    date_ts = pd.Timestamp(date_str)

    hist = hist[hist["date"] != date_ts]  # replace if re-run same day
    new_row = pd.DataFrame([{
        "date":      date_ts,
        "new_highs": counts["new_highs"],
        "new_lows":  counts["new_lows"],
        "net":       counts["net"],
        "total":     counts["total"],
    }])
    hist = pd.concat([hist, new_row], ignore_index=True).sort_values("date").reset_index(drop=True)
    hist["date"] = pd.to_datetime(hist["date"])
    _save_history(hist)
    return hist


# ── 3. Bias / stats derived from history ──────────────────────────────────────

def _consecutive_bias(net_series: pd.Series, streak: int = BIAS_STREAK) -> str:
    """'bullish' if the last `streak` values are all > 0, 'bearish' if all < 0, else 'neutral'."""
    tail = net_series.tail(streak)
    if len(tail) < streak:
        return "neutral"
    if (tail > 0).all():
        return "bullish"
    if (tail < 0).all():
        return "bearish"
    return "neutral"


def _stateful_bias_series(net_series: pd.Series, streak: int = BIAS_STREAK) -> list[str]:
    """
    Sticky bias per day, matching the Pine Script's background-shading behaviour:
    the state flips to bullish once `streak` consecutive positive days occur,
    flips to bearish once `streak` consecutive negative days occur, and otherwise
    holds the previous state (rather than flickering day to day).
    """
    state = "neutral"
    pos_streak = 0
    neg_streak = 0
    out: list[str] = []
    for v in net_series:
        if v > 0:
            pos_streak += 1
            neg_streak = 0
        elif v < 0:
            neg_streak += 1
            pos_streak = 0
        else:
            pos_streak = 0
            neg_streak = 0

        if pos_streak >= streak:
            state = "bullish"
        elif neg_streak >= streak:
            state = "bearish"
        out.append(state)
    return out



def compute_stats(history: pd.DataFrame, chart_sessions: int = 180) -> dict:
    """
    Build the full NNH stats dict used to render the dashboard card:
      today's high/low/net/total, raw 3-day bias, smoothed 10-day SMA + bias,
      and a longer per-day series (date, net, sma, bias, highs, lows) used to
      draw the TradingView-style histogram + background-shaded chart.
    """
    if history.empty:
        return {
            "available": False,
            "new_highs": 0, "new_lows": 0, "net": 0, "total": 0,
            "pct_highs": 0.0, "pct_lows": 0.0,
            "bias": "neutral", "sma": 0.0, "sma_bias": "neutral",
            "history": [], "chart": [],
        }

    history = history.sort_values("date").reset_index(drop=True)
    history["sma"]  = history["net"].rolling(window=SMA_WINDOW, min_periods=1).mean()
    history["bias"] = _stateful_bias_series(history["net"])

    today    = history.iloc[-1]
    bias     = _consecutive_bias(history["net"])
    sma_val  = float(today["sma"])
    sma_bias = "bullish" if sma_val > 0 else ("bearish" if sma_val < 0 else "neutral")

    total = float(today["total"]) or 1.0
    pct_highs = round(100.0 * today["new_highs"] / total, 1)
    pct_lows  = round(100.0 * today["new_lows"]  / total, 1)

    recent = history.tail(20)[["date", "net", "sma"]].copy()
    recent["date"] = recent["date"].dt.strftime("%Y-%m-%d")

    chart = history.tail(chart_sessions)[["date", "net", "sma", "bias", "new_highs", "new_lows"]].copy()
    chart["date"] = chart["date"].dt.strftime("%Y-%m-%d")
    chart["sma"]  = chart["sma"].round(2)

    return {
        "available":  True,
        "new_highs":  int(today["new_highs"]),
        "new_lows":   int(today["new_lows"]),
        "net":        int(today["net"]),
        "total":      int(today["total"]),
        "pct_highs":  pct_highs,
        "pct_lows":   pct_lows,
        "bias":       bias,        # 3-day consecutive raw bias (today)
        "sma":        round(sma_val, 1),
        "sma_bias":   sma_bias,    # 10-day SMA bias
        "history":    recent.to_dict("records"),
        "chart":      chart.to_dict("records"),
    }


def run(df: pd.DataFrame, date_str: str) -> dict:
    """Convenience entry-point: compute → persist → return rendered stats."""
    counts  = compute_today_counts(df)
    history = update_history(date_str, counts)
    stats   = compute_stats(history)
    logger.info(
        "Net New Highs — highs:%d lows:%d net:%d (3d bias:%s, 10-SMA:%.1f/%s)",
        stats["new_highs"], stats["new_lows"], stats["net"],
        stats["bias"], stats["sma"], stats["sma_bias"],
    )
    return stats


# ── 4. HTML card for the landing page ─────────────────────────────────────────

def build_html(stats: dict) -> str:
    """Render the Net New Highs section: chart with background bias shading,
    10-day SMA line, and a collapsible new-highs/new-lows table — modeled on
    Nitin R's TradingView Net New Highs script."""
    if not stats.get("available"):
        return """
<div class="sentiment-section">
  <h2 class="section-title">Net New Highs</h2>
  <div class="sentiment-card" style="max-width:480px">
    <div class="close-val">No data yet — runs after the first scan.</div>
  </div>
</div>"""

    import json as _json

    badge_map = {
        "bullish": ("bullish", "🟢 Bullish"),
        "bearish": ("bearish", "🔴 Bearish"),
        "neutral": ("mixed",   "🟡 Neutral"),
    }
    bias_css, bias_label = badge_map.get(stats["bias"],     ("mixed", "🟡 Neutral"))
    sma_css,  sma_label  = badge_map.get(stats["sma_bias"], ("mixed", "🟡 Neutral"))

    net     = stats["net"]
    net_css = "green" if net > 0 else ("red" if net < 0 else "na")
    net_str = f"{net:+d}"

    bias_gradients = {
        "bullish": "linear-gradient(90deg,var(--emerald),var(--blue))",
        "bearish": "linear-gradient(90deg,var(--red),var(--amber))",
        "neutral": "linear-gradient(90deg,var(--amber),var(--violet))",
    }
    card_accent = bias_gradients.get(stats["bias"], bias_gradients["neutral"])

    bias_glows = {
        "bullish": "0 20px 45px -22px rgba(5,150,105,.45)",
        "bearish": "0 20px 45px -22px rgba(220,38,38,.45)",
        "neutral": "0 20px 45px -22px rgba(180,83,9,.4)",
    }
    card_glow = bias_glows.get(stats["bias"], bias_glows["neutral"])

    bias_dot = {
        "bullish": "var(--emerald)", "bearish": "var(--red)", "neutral": "var(--amber)",
    }.get(stats["bias"], "var(--blue)")
    bias_dot_lt = {
        "bullish": "var(--emerald-lt)", "bearish": "var(--red-lt)", "neutral": "var(--amber-lt)",
    }.get(stats["bias"], "var(--blue-lt)")

    chart_data = _json.dumps(stats.get("chart", []))
    table_rows = "".join(
        f"<tr><td>{r['date']}</td>"
        f"<td style='color:var(--emerald)'>{r['new_highs']}</td>"
        f"<td style='color:var(--red)'>{r['new_lows']}</td>"
        f"<td style='color:{'var(--emerald)' if r['net']>=0 else 'var(--red)'}'>{r['net']:+d}</td></tr>"
        for r in reversed(stats.get("chart", [])[-20:])
    )

    return f"""
<div class="sentiment-section">
  <div class="hub-eyebrow" style="color:{bias_dot}"><span class="hub-dot" style="background:{bias_dot};box-shadow:0 0 0 3px {bias_dot_lt}"></span>MARKET BREADTH</div>
  <div style="display:flex;align-items:center;gap:1rem;margin-bottom:.25rem;">
    <h2 class="section-title" style="margin-bottom:0">Net New Highs</h2>
    <span class="overall-badge {bias_css}">{bias_label}</span>
  </div>
  <p style="font-size:.8rem;color:var(--muted);margin-bottom:.75rem;">
    NSE stocks making new 52-week highs minus new 52-week lows &middot;
    background shows bias confirmed after {BIAS_STREAK} consecutive days &middot;
    smoothed with a {SMA_WINDOW}-day SMA.
  </p>

  <div class="sentiment-card" style="margin-bottom:1.1rem;--card-accent:{card_accent};box-shadow:var(--shadow-sm),{card_glow};">
    <div class="sentiment-card-header">
      <span class="sentiment-index-name">Net New Highs &nbsp;
        <strong style="color:{'var(--emerald)' if net>=0 else 'var(--red)'}">{net_str}</strong>
      </span>
      <span class="overall-badge {sma_css}">10-SMA {stats['sma']:+.1f} &middot; {sma_label}</span>
    </div>
    <div class="close-val" style="margin-bottom:.6rem;">
      <span style="color:var(--emerald)">{stats['new_highs']} new highs</span> &nbsp;/&nbsp;
      <span style="color:var(--red)">{stats['new_lows']} new lows</span> &nbsp;
      of {stats['total']} stocks &nbsp;
      <span style="color:var(--muted);font-size:.8rem;">
        ({stats['pct_highs']}% / {stats['pct_lows']}%)
      </span>
    </div>
    <div style="position:relative;height:220px;">
      <canvas id="nnhChart"></canvas>
    </div>
    <div class="sentiment-legend" style="margin-top:.5rem;">
      Green/red background = sticky 3-day-confirmed bullish/bearish bias &middot;
      bars = daily Net New Highs &middot; amber line = {SMA_WINDOW}-day SMA
    </div>
  </div>

  <div class="sentiment-card" style="max-width:640px;">
    <div class="sentiment-card-header">
      <button onclick="toggleMonth(this)" aria-expanded="false"
        style="background:none;border:none;cursor:pointer;padding:0;font:inherit;color:var(--text);font-weight:600;">
        📋 New Highs / New Lows table (last 20 sessions) ▾
      </button>
    </div>
    <div class="month-body">
      <table style="width:100%;font-size:.82rem;border-collapse:collapse;">
        <thead>
          <tr style="text-align:left;border-bottom:1px solid var(--border);">
            <th style="padding:.35rem .5rem;">Date</th>
            <th style="padding:.35rem .5rem;">New Highs</th>
            <th style="padding:.35rem .5rem;">New Lows</th>
            <th style="padding:.35rem .5rem;">Net</th>
          </tr>
        </thead>
        <tbody>{table_rows}</tbody>
      </table>
    </div>
  </div>
</div>

<script src="js/chart.umd.js"></script>
<script>
(function() {{
  const nnhData = {chart_data};
  if (!nnhData.length) return;
  if (typeof Chart === 'undefined') {{
    const el = document.getElementById('nnhChart');
    if (el && el.parentElement) {{
      el.parentElement.innerHTML = '<div style="font-size:.8rem;color:var(--muted);padding:1rem 0;">Chart library failed to load — check your internet connection or ad-blocker.</div>';
    }}
    return;
  }}

  const labels = nnhData.map(d => d.date);
  const netVals = nnhData.map(d => d.net);
  const smaVals = nnhData.map(d => d.sma);
  const barColors = netVals.map(v => v >= 0 ? 'rgba(5,150,105,0.85)' : 'rgba(220,38,38,0.85)');

  // Background shading plugin — draws bullish/bearish zones behind the bars
  const biasBackgroundPlugin = {{
    id: 'biasBackground',
    beforeDatasetsDraw(chart) {{
      const {{ctx, chartArea, scales}} = chart;
      if (!chartArea) return;
      const xScale = scales.x;
      ctx.save();
      let segStart = 0;
      for (let i = 0; i <= nnhData.length; i++) {{
        const curBias = i < nnhData.length ? nnhData[i].bias : null;
        const prevBias = i > 0 ? nnhData[i - 1].bias : null;
        if (curBias !== prevBias) {{
          if (prevBias && prevBias !== 'neutral') {{
            const xStart = xScale.getPixelForValue(segStart) - (xScale.getPixelForValue(1) - xScale.getPixelForValue(0)) / 2;
            const xEnd   = xScale.getPixelForValue(i - 1) + (xScale.getPixelForValue(1) - xScale.getPixelForValue(0)) / 2;
            ctx.fillStyle = prevBias === 'bullish' ? 'rgba(5,150,105,0.10)' : 'rgba(220,38,38,0.10)';
            ctx.fillRect(xStart, chartArea.top, xEnd - xStart, chartArea.bottom - chartArea.top);
          }}
          segStart = i;
        }}
      }}
      ctx.restore();
    }}
  }};

  new Chart(document.getElementById('nnhChart'), {{
    type: 'bar',
    data: {{
      labels: labels,
      datasets: [
        {{
          type: 'bar',
          label: 'Net New Highs',
          data: netVals,
          backgroundColor: barColors,
          borderWidth: 0,
          barPercentage: 0.9,
          categoryPercentage: 0.9,
          order: 2,
        }},
        {{
          type: 'line',
          label: '{SMA_WINDOW}-day SMA',
          data: smaVals,
          borderColor: '#d97706',
          borderWidth: 1.5,
          pointRadius: 0,
          tension: 0.25,
          order: 1,
        }},
      ],
    }},
    plugins: [biasBackgroundPlugin],
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: 'index', intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            footer: (items) => {{
              const d = nnhData[items[0].dataIndex];
              return `Highs: ${{d.new_highs}}  Lows: ${{d.new_lows}}  Bias: ${{d.bias}}`;
            }}
          }}
        }},
      }},
      scales: {{
        x: {{
          ticks: {{ maxTicksLimit: 10, font: {{ size: 10 }} }},
          grid: {{ display: false }},
        }},
        y: {{
          ticks: {{ font: {{ size: 10 }} }},
          grid: {{ color: 'rgba(0,0,0,0.05)' }},
        }},
      }},
    }},
  }});
}})();
</script>"""
