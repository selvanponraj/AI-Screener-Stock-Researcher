from __future__ import annotations

"""
Download BSE Annual Reports & Concall Transcripts.

Folder structure: downloads/<scrip_name>/<doc_type>/
File name format:
- Annual Reports: FY2026_2026-05-15.pdf
- Concalls: FY2026_Q4_2026-04-14.pdf, FY2026_Q3_2026-01-16.pdf

Usage:
    python src/examples/download_reports.py <scrip_code_or_name> [--type report|concall|all] [--years 10]

Examples:
    python src/examples/download_reports.py tcs --type report --years 10
    python src/examples/download_reports.py 532540 --type concall --years 10
"""

import sys
import os
import re
import argparse
from datetime import datetime, timedelta
from collections import defaultdict
import requests
from pathlib import Path
from bse import BSE


HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Referer': 'https://www.bseindia.com/'
}

ATTACHMENT_URLS = [
    "https://www.bseindia.com/xml-data/corpfiling/AttachLive/",
    "https://www.bseindia.com/xml-data/corpfiling/AttachHis/"
]

EXCLUDE_ANNUAL_KEYWORDS = [
    "notice", "convening", "newspaper", "publication", "dispatch",
    "postal ballot", "reg. 36", "reg 36", "regulation 36", "voting results",
    "intimation", "disclosure under"
]


def determine_fy_and_period(dt_str: str, text: str, doc_type: str) -> tuple[str, str]:
    """
    Accurately maps filing dates to Indian Financial Year & Quarter.
    Indian Fiscal Year ends March 31.
    - Apr-Jun (Months 4-6)   -> Q4 of FY (current calendar year)
    - Jul-Sep (Months 7-9)   -> Q1 of FY (next calendar year)
    - Oct-Dec (Months 10-12) -> Q2 of FY (next calendar year)
    - Jan-Mar (Months 1-3)   -> Q3 of FY (current calendar year)
    """
    if doc_type == "report":
        text_upper = text.upper()
        match = re.search(r'\bFY\s*(\d{2,4})\b', text_upper)
        if match:
            y_val = match.group(1)
            return f"FY20{y_val}" if len(y_val) == 2 else f"FY{y_val}", "FY"
        
        match = re.search(r'\b(20\d{2})\s*[-–/]\s*(\d{2,4})\b', text_upper)
        if match:
            end_yr = match.group(2)
            return f"FY20{end_yr}" if len(end_yr) == 2 else f"FY{end_yr}", "FY"

        if dt_str:
            dt = datetime.strptime(dt_str[:10], '%Y-%m-%d')
            fy_year = dt.year + 1 if dt.month >= 4 else dt.year
            return f"FY{fy_year}", "FY"
        return "FY_UNKNOWN", "FY"

    # For Concalls
    text_upper = text.upper()

    q_match = re.search(r'\b(Q[1-4])\b', text_upper)
    fy_match = re.search(r'\bFY\s*(\d{2,4})\b', text_upper)

    parsed_q = q_match.group(1) if q_match else None
    parsed_fy = None
    if fy_match:
        y_val = fy_match.group(1)
        parsed_fy = f"FY20{y_val}" if len(y_val) == 2 else f"FY{y_val}"

    if dt_str:
        dt = datetime.strptime(dt_str[:10], '%Y-%m-%d')
        month = dt.month
        year = dt.year

        if 1 <= month <= 3:
            calc_fy = f"FY{year}"
            calc_q = "Q3"
        elif 4 <= month <= 6:
            calc_fy = f"FY{year}"
            calc_q = "Q4"
        elif 7 <= month <= 9:
            calc_fy = f"FY{year + 1}"
            calc_q = "Q1"
        else:
            calc_fy = f"FY{year + 1}"
            calc_q = "Q2"

        final_fy = parsed_fy if parsed_fy else calc_fy
        final_q = parsed_q if parsed_q else calc_q
        return final_fy, final_q

    return parsed_fy or "FY_UNKNOWN", parsed_q or "Q1"


def classify_announcement(item: dict, requested_type: str) -> str | None:
    """Classify document strictly into 'report' or 'concall' transcripts."""
    subcat = (item.get('SUBCATNAME') or '').lower()
    headline = (item.get('HEADLINE') or '').lower()
    newssub = (item.get('NEWSSUB') or '').lower()
    cat = (item.get('CATEGORYNAME') or '').lower()

    text = f"{subcat} {headline} {newssub} {cat}"

    doc_type = None

    if 'annual report' in text or 'reg. 34 (1)' in text or 'reg. 34' in text:
        if not any(exc in headline or exc in newssub for exc in EXCLUDE_ANNUAL_KEYWORDS):
            doc_type = 'report'

    elif 'transcript' in text:
        if any(k in text for k in ['earnings', 'concall', 'conference call', 'investor call', 'call held']):
            if not any(exc in headline or exc in newssub for exc in ["notice", "intimation", "analyst day"]):
                doc_type = 'concall'

    if not doc_type:
        return None

    if requested_type == 'all' or requested_type == doc_type:
        return doc_type

    return None


def find_existing_file(save_dir: Path, doc_type: str, year: str, period: str, dt_str: str) -> Path | None:
    """
    Checks if a report or concall for the same period/date already exists on disk.
    """
    if not save_dir.exists():
        return None

    # 1. Exact match check
    exact_filename = f"{year}_{dt_str}.pdf" if doc_type == "report" else f"{year}_{period}_{dt_str}.pdf"
    exact_path = save_dir / exact_filename
    if exact_path.exists():
        return exact_path

    # 2. Check for any file starting with year & period (e.g. FY2026_Q4_*.pdf or FY2026_*.pdf)
    prefix = f"{year}_" if doc_type == "report" else f"{year}_{period}_"
    for file in save_dir.glob("*.pdf"):
        if file.name.startswith(prefix):
            return file

    return None


def download_attachment(attachment_name: str, save_path: Path) -> bool:
    if not attachment_name:
        return False

    if save_path.exists():
        print(f"  [Already Exists] {save_path.name}")
        return True

    for base_url in ATTACHMENT_URLS:
        url = base_url + attachment_name
        try:
            res = requests.get(url, headers=HEADERS, timeout=15)
            if res.status_code == 200 and len(res.content) > 1000 and not res.content.startswith(b'<!DOCTYPE'):
                save_path.parent.mkdir(parents=True, exist_ok=True)
                save_path.write_bytes(res.content)
                print(f"  [Downloaded] {save_path.name} ({len(res.content) / 1024 / 1024:.2f} MB)")
                return True
        except Exception:
            continue
    print(f"  [Failed] Could not download {attachment_name}")
    return False


def select_best_documents(found_docs: list) -> list:
    """
    Deduplicates so only 1 main document is downloaded per period:
    - 1 Annual Report per FY
    - 1 Earnings Call Transcript per Quarter
    """
    reports_by_fy = defaultdict(list)
    concalls_by_quarter = defaultdict(list)

    for doc_type, item, year, period in found_docs:
        if doc_type == 'report':
            reports_by_fy[year].append((item, year, period))
        elif doc_type == 'concall':
            concalls_by_quarter[(year, period)].append((item, year, period))

    selected_docs = []

    for year, items in reports_by_fy.items():
        items.sort(key=lambda x: x[0].get('Fld_Attachsize') or 0, reverse=True)
        best_item = items[0]
        selected_docs.append(('report', best_item[0], best_item[1], best_item[2]))

    for (year, period), items in concalls_by_quarter.items():
        items.sort(key=lambda x: x[0].get('Fld_Attachsize') or 0, reverse=True)
        best_item = items[0]
        selected_docs.append(('concall', best_item[0], best_item[1], best_item[2]))

    return selected_docs


def main():
    parser = argparse.ArgumentParser(description="Download BSE Annual Reports & Concall Transcripts")
    parser.add_argument("symbol", help="Stock Scrip Code (e.g., 532540) or Name (e.g., tcs)")
    parser.add_argument("--type", choices=["report", "concall", "all"], default="all", help="Document type: report (Annual) or concall (Transcripts)")
    parser.add_argument("--years", type=int, default=10, help="Number of years to fetch (default: 10)")
    parser.add_argument("--output-dir", default="data/reports", help="Output directory (default: data/reports)")

    args = parser.parse_args()

    to_date = datetime.now()
    from_date = to_date - timedelta(days=args.years * 365)

    print("=== BSE Document Downloader ===")
    print(f"Target Symbol : {args.symbol}")
    print(f"Filter Type   : {args.type.upper()}")
    print(f"Date Range    : {from_date.strftime('%Y-%m-%d')} to {to_date.strftime('%Y-%m-%d')} ({args.years} Years)")
    print(f"Output Directory: {os.path.abspath(args.output_dir)}\n")

    with BSE(download_folder='./') as bse:
        scrip_code = args.symbol
        scrip_name = args.symbol.lower()

        if not scrip_code.isdigit():
            scrip_code = bse.getScripCode(args.symbol)
            if not scrip_code:
                print(f"Error: Could not resolve scrip code for '{args.symbol}'")
                sys.exit(1)
            print(f"Resolved '{args.symbol}' to Scrip Code: {scrip_code}\n")
        else:
            resolved_name = bse.getScripName(scrip_code)
            if resolved_name:
                scrip_name = resolved_name.lower().replace(" ", "_")

        output_base_dir = Path(args.output_dir) / scrip_name

        page = 1
        found_docs = []

        print("Fetching announcements from BSE...")
        while True:
            data = bse.announcements(
                scripcode=scrip_code,
                from_date=from_date,
                to_date=to_date,
                page_no=page
            )
            table = data.get('Table', [])
            if not table:
                break

            total_rows = data.get('Table1', [{}])[0].get('ROWCNT', 0)

            for item in table:
                doc_type = classify_announcement(item, args.type)
                if doc_type:
                    dt_str = (item.get('NEWS_DT') or '')[:10]
                    headline = item.get('HEADLINE') or ''
                    newssub = item.get('NEWSSUB') or ''
                    combined_text = f"{headline} {newssub}"

                    year, period = determine_fy_and_period(dt_str, combined_text, doc_type)
                    found_docs.append((doc_type, item, year, period))

            print(f"Processed Page {page} / {(total_rows + 24) // 25} (Found {len(found_docs)} candidate documents)")

            if page * 25 >= total_rows or len(table) < 25:
                break
            page += 1

        selected_docs = select_best_documents(found_docs)

        print(f"\nSelected {len(selected_docs)} primary documents for download.\n")

        if not selected_docs:
            print("No matching primary documents found.")
            return

        print("Downloading PDF attachments...")

        for doc_type, item, year, period in selected_docs:
            dt_str = (item.get('NEWS_DT') or '')[:10]
            att_name = item.get('ATTACHMENTNAME')

            if att_name:
                save_dir = output_base_dir / doc_type
                
                # Pre-check if file for this year/quarter/date already exists
                existing = find_existing_file(save_dir, doc_type, year, period, dt_str)
                if existing:
                    print(f"[{doc_type.upper()}] {year} | {period} | Date={dt_str}")
                    print(f"  [Already Exists] {existing.name}")
                    continue

                if doc_type == "report":
                    filename = f"{year}_{dt_str}.pdf"
                else:
                    filename = f"{year}_{period}_{dt_str}.pdf"

                pdf_save_path = save_dir / filename
                print(f"[{doc_type.upper()}] {year} | {period} | Date={dt_str} -> {pdf_save_path.name}")
                download_attachment(att_name, pdf_save_path)


if __name__ == "__main__":
    main()
