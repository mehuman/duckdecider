#!/usr/bin/env python3
"""Parse Sauvie Island harvest PDFs and sort blinds by ducks per hunter."""

import json
import pdfplumber
import re
from pathlib import Path
from urllib.request import urlopen


def parse_page1_summary(text):
    """Extract named blinds with hunters and ducks from page 1 daily summary.
    Returns list of (side, name, hunters, ducks).
    Line format: name (1+ words) hunters ducks geese other ducks_per_hunter
    """
    blinds = []
    def parse_section(block, side):
        out = []
        for line in block.split("\n"):
            parts = line.split()
            if len(parts) < 6:
                continue
            try:
                hunters = int(parts[-5])
                ducks = int(parts[-4])
                name = " ".join(parts[:-5])
                if not name or name in ("EASTSIDE", "EASTSIDE TOTALS", "WESTSIDE", "WESTSIDE TOTALS"):
                    continue
                out.append((side, name, hunters, ducks))
            except (ValueError, IndexError):
                pass
        return out

    east_match = re.search(r"EASTSIDE HUNTERS.*?EASTSIDE TOTALS", text, re.DOTALL)
    if east_match:
        blinds.extend(parse_section(east_match.group(0), "Eastside"))
    west_match = re.search(r"WESTSIDE HUNTERS.*?WESTSIDE TOTALS", text, re.DOTALL)
    if west_match:
        blinds.extend(parse_section(west_match.group(0), "Westside"))
    return blinds


# Map PDF vertical-text in column 0 to unit names (exact strings from extract_tables)
UNIT_COL0_MAP = {
    "t\ni\nn\nU\nn\no\ns\nn\nh\no\nJ": "Johnson",
    "k\nc\na\nt\nr i\nt n\ne U\nc\na\nR": "Racetrack",
    "t\ni\nn\nU\nt\nn\nu\nH": "Hunt",
    "t\ni\nn\nU\nn\ne\nh\nd\nu\nM": "Mudhen",
    "d\nn\na t\nl s i n\nI U\nk\na\nO": "Oak Island",
    "t\ni\nn\nU\ne\nk\na\nL\nd\nu\nM": "Mud Lake",
    "t\ni\nn\nU\ne\nk\na\nL\nl\na\ne\nS": "Seal",
    "t\ni\nn\nU\nn\na\nm\nl\ne\ne\nt\nS": "Steelman",
    "t\nn\ni\no\nP\nt\nn i n\na U\nm\nl\no\nH": "Holman Point",
}


def parse_detail_tables(pdf):
    """Extract blind rows from detail tables on pages 2 and 3.
    Returns list of (side, name, hunters, ducks).
    """
    rows = []
    current_unit = None

    for page_num in [1, 2]:  # 0-indexed: pages 2 and 3
        page = pdf.pages[page_num]
        tables = page.extract_tables()
        current_unit = None
        for table in tables:
            for row in table:
                if not row or len(row) < 6:
                    continue
                blind_cell = row[1] if len(row) > 1 else None
                if blind_cell is None:
                    continue
                if blind_cell == "Blind":
                    continue
                col0 = row[0]
                if col0 and str(col0).strip() and "\n" in str(col0):
                    current_unit = UNIT_COL0_MAP.get(str(col0).strip())
                if current_unit is None:
                    continue
                try:
                    blind_id = str(blind_cell).strip()
                    hunters_str = row[2] if len(row) > 2 else ""
                    ducks_str = row[3] if len(row) > 3 else ""
                    if hunters_str is None or ducks_str is None:
                        continue
                    hunters, ducks = int(hunters_str or 0), int(ducks_str or 0)
                    side = "Eastside" if page_num == 1 else "Westside"
                    rows.append((side, f"{current_unit} #{blind_id}", hunters, ducks))
                except (ValueError, TypeError, IndexError):
                    pass

    return rows


ODFW_DAILY_URL = "https://myodfw.com/2025-26-sauvie-island-wildlife-area-game-bird-harvest-statistics"


def get_latest_pdf_urls(n: int = 3):
    """
    Fetch the ODFW daily harvest page and return the latest n daily-report PDF URLs.
    We look for URLs like .../YYYY-MM/DDMMYYYYs.pdf and sort by the 8-digit date in the filename.
    """
    with urlopen(ODFW_DAILY_URL) as resp:
        html = resp.read().decode("utf-8", errors="ignore")

    # Daily harvest PDFs all end with an 8-digit date + 's.pdf' (optionally '_0'), e.g. 01252026s.pdf or 10132025s_0.pdf
    # Capture the full URL and the 8-digit date separately.
    pattern = re.compile(
        r"(https://myodfw\.com/sites/default/files/\d{4}-\d{2}/(\d{8})s(?:_0)?\.pdf)"
    )
    matches = pattern.findall(html)

    if not matches:
        return []

    # Filename date is MMDDYYYY. Sort by (year, month, day) descending so Jan 2026 > Dec 2025.
    def sort_key(datestr):
        ds = str(datestr).zfill(8)  # MMDDYYYY
        mm, dd, yyyy = int(ds[:2]), int(ds[2:4]), int(ds[4:])
        return (yyyy, mm, dd)

    url_to_date = {}
    for full_url, datestr in matches:
        if len(datestr) != 8 or not datestr.isdigit():
            continue
        url_to_date[full_url] = datestr

    sorted_urls = sorted(url_to_date.items(), key=lambda kv: sort_key(kv[1]), reverse=True)

    # Return (url, iso_date) pairs, e.g. ('…/01252026s.pdf', '2026-01-25')
    result = []
    for full_url, ds in sorted_urls[:n]:
        ds = ds.zfill(8)
        iso = f"{ds[4:]}-{ds[:2]}-{ds[2:4]}"
        result.append((full_url, iso))
    return result


def download_pdf(url: str, dest: Path):
    """Download a PDF from url to dest if it doesn't already exist."""
    if dest.exists():
        return
    with urlopen(url) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def parse_one_pdf(pdf_path):
    """Parse a single daily harvest PDF. Returns list of (side, name, hunters, ducks)."""
    with pdfplumber.open(pdf_path) as pdf:
        page1_text = pdf.pages[0].extract_text() or ""
        summary = parse_page1_summary(page1_text)
        detail = parse_detail_tables(pdf)
    return summary + detail


def main():
    base = Path(__file__).parent
    # Aggregate per blind across latest 3 days:
    # (side, name) -> {"totals": {"hunters": int, "ducks": int}, "daily": {date: {...}}}
    by_blind = {}
    used_dates = set()

    # Discover the latest 3 daily harvest PDFs from the ODFW page
    pdf_infos = get_latest_pdf_urls(3)  # list of (url, iso_date)
    for url, date_label in pdf_infos:
        used_dates.add(date_label)
        filename = url.rsplit("/", 1)[-1]
        pdf_path = base / filename
        download_pdf(url, pdf_path)
        for side, name, hunters, ducks in parse_one_pdf(pdf_path):
            key = (side, name)
            rec = by_blind.setdefault(
                key,
                {"side": side, "name": name, "totals": {"hunters": 0, "ducks": 0}, "daily": {}},
            )
            rec["totals"]["hunters"] += hunters
            rec["totals"]["ducks"] += ducks
            day_entry = rec["daily"].setdefault(date_label, {"hunters": 0, "ducks": 0})
            day_entry["hunters"] += hunters
            day_entry["ducks"] += ducks

    dates = sorted(used_dates)

    # Build rankings arrays for each side with totals and daily breakdown
    eastside_records = []
    westside_records = []
    for (side, name), rec in by_blind.items():
        th = rec["totals"]["hunters"]
        td = rec["totals"]["ducks"]
        dph = (td / th) if th else 0.0
        daily_list = []
        for d in sorted(rec["daily"].keys()):
            dh = rec["daily"][d]["hunters"]
            dd = rec["daily"][d]["ducks"]
            ddph = (dd / dh) if dh else 0.0
            daily_list.append(
                {
                    "date": d,
                    "hunters": dh,
                    "ducks": dd,
                    "ducksPerHunter": round(ddph, 3),
                }
            )
        record = {
            "blind": name,
            "totalHunters": th,
            "totalDucks": td,
            "ducksPerHunter": round(dph, 3),
            "daily": daily_list,
        }
        if side == "Eastside":
            eastside_records.append(record)
        else:
            westside_records.append(record)

    eastside_records.sort(key=lambda r: (r["ducksPerHunter"], r["blind"]), reverse=True)
    westside_records.sort(key=lambda r: (r["ducksPerHunter"], r["blind"]), reverse=True)

    def print_ranking(side_name, rows):
        print(f"\n{'='*52}")
        print(f"  {side_name} — ranked by ducks per hunter (3-day total)")
        print("="*52)
        print(f"{'Rank':>4}  {'Blind':<25} {'Ducks/Hunter':>12}")
        print("-"*52)
        for rank, rec in enumerate(rows, 1):
            print(f"{rank:>4}  {rec['blind']:<25} {rec['ducksPerHunter']:>12.1f}")

    print("Blinds by ducks per hunter (latest 3 days combined, highest first)")
    print(f"Source: latest 3 daily harvest reports from ODFW")
    print_ranking("EASTSIDE", eastside_records)
    print_ranking("WESTSIDE", westside_records)

    out_path = base / "blinds_by_ducks_per_hunter.txt"
    with open(out_path, "w") as f:
        f.write("Blinds ranked by ducks per hunter (3-day aggregate)\n")
        f.write("Sauvie Island Wildlife Area — latest 3 daily reports\n\n")
        f.write("EASTSIDE\n")
        f.write(f"{'Rank':>4}  {'Blind':<25} {'Ducks/Hunter':>12}\n")
        f.write("-"*52 + "\n")
        for rank, rec in enumerate(eastside_records, 1):
            f.write(f"{rank:>4}  {rec['blind']:<25} {rec['ducksPerHunter']:>12.1f}\n")
        f.write("\nWESTSIDE\n")
        f.write(f"{'Rank':>4}  {'Blind':<25} {'Ducks/Hunter':>12}\n")
        f.write("-"*52 + "\n")
        for rank, rec in enumerate(westside_records, 1):
            f.write(f"{rank:>4}  {rec['blind']:<25} {rec['ducksPerHunter']:>12.1f}\n")
    print(f"\nWrote rankings to {out_path}")

    # Also write JSON for the static website
    json_path = base / "blinds_data.json"
    data = {
        "source": "latest 3 daily harvest reports from ODFW",
        "dates": dates,
        "eastside": eastside_records,
        "westside": westside_records,
    }
    with open(json_path, "w") as jf:
        json.dump(data, jf, indent=2)
    print(f"Wrote JSON data to {json_path}")

    # Generate and write index.html for the static site
    index_path = base / "index.html"
    index_path.write_text(get_index_html(), encoding="utf-8")
    print(f"Wrote {index_path}")


def get_index_html():
    """Return the full HTML for the static blinds site (loads blinds_data.json via fetch)."""
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>Sauvie Island Duck Blinds – Last 3 Days</title>
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <style>
    :root {
      --bg: #050816;
      --bg-elevated: #0b1020;
      --accent: #4ade80;
      --accent-soft: rgba(74, 222, 128, 0.12);
      --text: #e5e7eb;
      --muted: #9ca3af;
      --border: #1f2937;
      --danger: #f97373;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif;
      background: radial-gradient(circle at top, #111827 0, #020617 55%, #000 100%);
      color: var(--text);
      display: flex;
      flex-direction: column;
      align-items: stretch;
    }
    header {
      padding: 1.5rem clamp(1.5rem, 3vw, 3rem);
      border-bottom: 1px solid rgba(15,23,42,0.9);
      backdrop-filter: blur(18px);
      background: linear-gradient(to bottom, rgba(15,23,42,0.9), rgba(15,23,42,0.4));
      position: sticky;
      top: 0;
      z-index: 20;
    }
    h1 { margin: 0 0 0.25rem; font-size: clamp(1.4rem, 2vw, 1.8rem); letter-spacing: 0.03em; }
    .subheading {
      font-size: 0.9rem;
      color: var(--muted);
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem;
      align-items: center;
      justify-content: space-between;
    }
    .pill {
      padding: 0.15rem 0.6rem;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.35);
      font-size: 0.75rem;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
    }
    .pill-dot {
      width: 6px; height: 6px; border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 12px rgba(74,222,128,0.7);
    }
    main {
      padding: 1.5rem clamp(1.5rem, 3vw, 3rem) 2.5rem;
      display: flex;
      flex-direction: column;
      gap: 1.5rem;
    }
    .layout {
      display: grid;
      grid-template-columns: minmax(0, 1fr);
      gap: 1.25rem;
    }
    @media (min-width: 980px) {
      .layout { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    .panel {
      background: radial-gradient(circle at top left, rgba(37,99,235,0.2), transparent 60%),
                  radial-gradient(circle at bottom right, rgba(16,185,129,0.16), transparent 55%),
                  linear-gradient(to bottom right, rgba(15,23,42,0.98), rgba(2,6,23,0.98));
      border-radius: 1.1rem;
      border: 1px solid rgba(30,64,175,0.9);
      box-shadow: 0 30px 80px rgba(0,0,0,0.85), 0 0 0 1px rgba(15,23,42,0.8);
      display: flex;
      flex-direction: column;
      overflow: hidden;
    }
    .panel-header {
      padding: 0.95rem 1.1rem 0.8rem;
      border-bottom: 1px solid rgba(31,41,55,0.95);
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 0.75rem;
    }
    .panel-title {
      font-size: 0.95rem;
      text-transform: uppercase;
      letter-spacing: 0.18em;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      gap: 0.6rem;
    }
    .panel-title span.badge {
      font-size: 0.7rem;
      padding: 0.12rem 0.4rem;
      border-radius: 999px;
      border: 1px solid rgba(148,163,184,0.45);
      color: var(--muted);
      text-transform: none;
      letter-spacing: 0.04em;
    }
    .panel-meta { font-size: 0.75rem; color: var(--muted); display: flex; flex-direction: column; align-items: flex-end; gap: 0.15rem; }
    .panel-meta strong { color: var(--accent); font-weight: 600; }
    .blinds-list {
      padding: 0.4rem 0.4rem 0.75rem;
      max-height: 80vh;
      overflow: auto;
      scrollbar-width: thin;
      scrollbar-color: rgba(148,163,184,0.7) transparent;
    }
    .blinds-list::-webkit-scrollbar { width: 6px; }
    .blinds-list::-webkit-scrollbar-track { background: transparent; }
    .blinds-list::-webkit-scrollbar-thumb { background: rgba(148,163,184,0.55); border-radius: 999px; }
    .blind {
      border-radius: 0.75rem;
      border: 1px solid rgba(31,41,55,0.95);
      background: linear-gradient(to bottom right, rgba(15,23,42,0.9), rgba(10,15,30,0.98));
      margin: 0.3rem;
      overflow: hidden;
    }
    .blind-header {
      all: unset;
      cursor: pointer;
      display: grid;
      grid-template-columns: auto 1fr auto;
      gap: 0.55rem;
      align-items: center;
      padding: 0.55rem 0.7rem;
      font-size: 0.85rem;
      color: var(--text);
    }
    .blind-header:hover {
      background: radial-gradient(circle at top left, rgba(37,99,235,0.25), transparent 60%);
    }
    .blind-rank { font-variant-numeric: tabular-nums; color: var(--muted); font-size: 0.78rem; width: 2.2rem; text-align: right; }
    .blind-name { font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .blind-metrics {
      font-variant-numeric: tabular-nums;
      text-align: right;
      font-size: 0.8rem;
      color: var(--muted);
      display: flex;
      flex-direction: column;
      gap: 0.15rem;
    }
    .blind-metrics strong { color: var(--accent); font-weight: 600; }
    .blind-arrow { margin-left: 0.25rem; transition: transform 0.18s ease; font-size: 0.9rem; opacity: 0.85; }
    .blind.open .blind-arrow { transform: rotate(90deg); }
    .blind-panel {
      display: none;
      padding: 0 0.7rem 0.6rem;
      font-size: 0.78rem;
      background: radial-gradient(circle at top, rgba(15,118,110,0.32), transparent 55%);
      border-top: 1px solid rgba(30,64,175,0.6);
    }
    .blind.open .blind-panel { display: block; }
    .daily-meta { display: flex; justify-content: space-between; align-items: baseline; margin: 0.45rem 0 0.25rem; }
    .daily-title { font-weight: 500; color: var(--muted); letter-spacing: 0.06em; text-transform: uppercase; font-size: 0.72rem; }
    .daily-summary { font-size: 0.78rem; color: var(--muted); }
    .daily-summary strong { color: var(--accent); }
    table { width: 100%; border-collapse: collapse; margin-top: 0.3rem; }
    thead { background: rgba(15,23,42,0.9); }
    th, td { padding: 0.25rem 0.35rem; text-align: right; font-variant-numeric: tabular-nums; }
    th:first-child, td:first-child { text-align: left; }
    th {
      font-size: 0.72rem;
      color: var(--muted);
      border-bottom: 1px solid rgba(30,64,175,0.8);
      position: sticky;
      top: 0;
      background: linear-gradient(to bottom, rgba(15,23,42,1), rgba(15,23,42,0.9));
      z-index: 1;
    }
    tbody tr:nth-child(even) td { background: rgba(15,23,42,0.7); }
    tbody tr:nth-child(odd) td { background: rgba(15,23,42,0.4); }
    .empty-state { padding: 1rem 1.2rem 1.2rem; font-size: 0.85rem; color: var(--muted); }
    .empty-state strong { color: var(--danger); }
    .footer-note { margin-top: 0.25rem; font-size: 0.75rem; color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>Sauvie Island Duck Blinds</h1>
    <div class="subheading">
      <span>Latest 3 daily harvest reports · Ducks per hunter by blind</span>
      <span class="pill"><span class="pill-dot"></span> Live from&nbsp;<code>myodfw.com</code></span>
    </div>
    <div class="footer-note">
      Data source: latest three Daily Harvest PDFs from
      <a href="https://myodfw.com/2025-26-sauvie-island-wildlife-area-game-bird-harvest-statistics" target="_blank" rel="noreferrer" style="color: var(--accent); text-decoration: none; border-bottom: 1px solid rgba(74,222,128,0.5);">ODFW Sauvie Island harvest statistics</a>.
    </div>
  </header>
  <main>
    <div class="layout">
      <section class="panel" id="east-panel">
        <div class="panel-header">
          <div class="panel-title">Eastside <span class="badge" id="east-days"></span></div>
          <div class="panel-meta">
            <span>Ranked by 3-day ducks per hunter</span>
            <span id="east-count"></span>
          </div>
        </div>
        <div class="blinds-list" id="eastside"></div>
      </section>
      <section class="panel" id="west-panel">
        <div class="panel-header">
          <div class="panel-title">Westside <span class="badge" id="west-days"></span></div>
          <div class="panel-meta">
            <span>Ranked by 3-day ducks per hunter</span>
            <span id="west-count"></span>
          </div>
        </div>
        <div class="blinds-list" id="westside"></div>
      </section>
    </div>
  </main>
  <script>
    async function loadData() {
      try {
        const res = await fetch('blinds_data.json', { cache: 'no-cache' });
        if (!res.ok) throw new Error('Failed to load blinds_data.json');
        const data = await res.json();
        renderAll(data);
      } catch (err) {
        console.error(err);
        document.querySelectorAll('.blinds-list').forEach(function(list) {
          list.innerHTML = '<div class="empty-state"><strong>Unable to load data.</strong> Ensure blinds_data.json is present and open via a local web server.</div>';
        });
      }
    }
    function formatDate(iso) {
      var p = iso.split('-');
      return p[1] + '/' + p[2] + '/' + p[0].slice(2);
    }
    function renderAll(data) {
      var dateLabel = (data.dates && data.dates.length) ? data.dates.map(formatDate).join(' \u00b7 ') : 'No dates';
      document.getElementById('east-days').textContent = dateLabel;
      document.getElementById('west-days').textContent = dateLabel;
      renderSide('eastside', data.eastside || [], 'east-count');
      renderSide('westside', data.westside || [], 'west-count');
    }
    function renderSide(containerId, blinds, countId) {
      var container = document.getElementById(containerId);
      container.innerHTML = '';
      if (!blinds.length) {
        container.innerHTML = '<div class="empty-state"><strong>No blinds found.</strong></div>';
        if (countId) document.getElementById(countId).textContent = '0 blinds';
        return;
      }
      if (countId) document.getElementById(countId).textContent = blinds.length + ' blinds';
      blinds.forEach(function(blind, index) {
        var wrapper = document.createElement('article');
        wrapper.className = 'blind';
        var header = document.createElement('button');
        header.className = 'blind-header';
        header.innerHTML = '<div class="blind-rank">' + String(index + 1).padStart(2, ' ') + '</div><div class="blind-name">' + blind.blind + '</div><div class="blind-metrics"><div><strong>' + blind.ducksPerHunter.toFixed(1) + '</strong> ducks / hunter</div><div>' + blind.totalDucks + ' ducks \u00b7 ' + blind.totalHunters + ' hunters</div></div><span class="blind-arrow">\u203a</span>';
        var panel = document.createElement('div');
        panel.className = 'blind-panel';
        var dailySorted = blind.daily.slice().sort(function(a,b) { return a.date.localeCompare(b.date); });
        var rows = dailySorted.map(function(d) {
          return '<tr><td>' + formatDate(d.date) + '</td><td>' + d.hunters + '</td><td>' + d.ducks + '</td><td>' + d.ducksPerHunter.toFixed(1) + '</td></tr>';
        }).join('');
        panel.innerHTML = '<div class="daily-meta"><div class="daily-title">Daily breakdown</div><div class="daily-summary">' + blind.daily.length + ' day(s) from latest reports.</div></div><table><thead><tr><th>Date</th><th>Hunters</th><th>Ducks</th><th>Ducks/Hunter</th></tr></thead><tbody>' + rows + '</tbody></table>';
        header.addEventListener('click', function() {
          wrapper.classList.toggle('open');
          if (wrapper.classList.contains('open')) {
            container.querySelectorAll('.blind.open').forEach(function(el) { if (el !== wrapper) el.classList.remove('open'); });
          }
        });
        wrapper.appendChild(header);
        wrapper.appendChild(panel);
        container.appendChild(wrapper);
      });
    }
    document.addEventListener('DOMContentLoaded', loadData);
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
