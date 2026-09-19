"""
csv_download_snippet.py
=======================
Drop-in helpers for scanner/dashboard.py
Adds a "Download CSV" button to the generated HTML dashboards.

Usage
-----
1. Copy `csv_download_bar_html()` into dashboard.py (or paste inline).
2. Call it inside your HTML-building function and inject the result
   right after your <body> tag (or after the top navbar).

See dashboard_csv_download.patch for the full step-by-step guide.
"""

from datetime import datetime


# ── CSS to inject inside your existing <style>…</style> block ────────────────

CSV_DOWNLOAD_CSS = """
/* ── CSV download button ── */
.csv-download-bar {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 20px;
    background: #0d1117;
    border-bottom: 1px solid #30363d;
}
.csv-download-btn {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 7px 16px;
    background: #238636;
    color: #ffffff;
    font-size: 13px;
    font-weight: 600;
    border: 1px solid #2ea043;
    border-radius: 6px;
    text-decoration: none;
    cursor: pointer;
    transition: background 0.15s;
}
.csv-download-btn:hover { background: #2ea043; }
.csv-download-label {
    color: #8b949e;
    font-size: 12px;
}
"""


# ── HTML snippet generator ────────────────────────────────────────────────────

def csv_download_bar_html(
    date_str: str,           # "YYYYMMDD"  e.g. "20250531"
    scan_date_display: str,  # "YYYY-MM-DD" e.g. "2025-05-31"
    elite: bool = False,     # True → use elite CSV, False → passing stocks CSV
    include_full: bool = True,  # also add a "Full Results" button
) -> str:
    """
    Returns the HTML for the download bar to inject into the dashboard.

    Parameters
    ----------
    date_str          : YYYYMMDD string used in filenames.
    scan_date_display : Human-readable date shown in the label.
    elite             : If True, primary CSV is passing_ema10_*.csv;
                        otherwise passing_stocks_*.csv.
    include_full      : Optionally add a second button for full_results_*.csv.
    """
    if elite:
        primary_file  = f"passing_ema10_{date_str}.csv"
        primary_label = "Download Elite CSV"
        panel_label   = f"Elite Stocks (Close &gt; EMA10) · Scan date: {scan_date_display}"
    else:
        primary_file  = f"passing_stocks_{date_str}.csv"
        primary_label = "Download CSV"
        panel_label   = f"Passing Stocks · Scan date: {scan_date_display}"

    full_btn = ""
    if include_full:
        full_file = f"passing_stocks_{date_str}.csv"
        full_btn = f"""
      <a class="csv-download-btn"
         style="background:#1f6feb; border-color:#388bfd;"
         href="{full_file}"
         download="{full_file}">
        ⬇ Minervini Trend Template Passing Stock CSV
      </a>"""

    return f"""
    <!-- ── CSV download bar (auto-generated) ── -->
    <div class="csv-download-bar">
      <a class="csv-download-btn"
         href="{primary_file}"
         download="{primary_file}">
        ⬇ {primary_label}
      </a>{full_btn}
      <span class="csv-download-label">{panel_label}</span>
    </div>
    """


# ── Example: how to wire it into your existing dashboard builder ─────────────
#
# Inside your existing function (e.g. build_dashboard or generate_html),
# before you return / write the HTML string, do something like:
#
#   date_str          = datetime.today().strftime("%Y%m%d")
#   scan_date_display = datetime.today().strftime("%Y-%m-%d")
#
#   download_bar = csv_download_bar_html(date_str, scan_date_display, elite=False)
#
#   html = html.replace("<body>", f"<body>\n{download_bar}", 1)
#
#   # Also inject the CSS into your <style> block:
#   html = html.replace("</style>", f"{CSV_DOWNLOAD_CSS}\n</style>", 1)
#
# Repeat with elite=True for the elite dashboard builder.
