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


if __name__ == "__main__":
    main()
