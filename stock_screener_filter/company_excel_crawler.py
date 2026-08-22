"""Download Screener Excel exports for companies meeting a rules-pass threshold."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Error, Page, TimeoutError, sync_playwright

from stock_screener_filter.screener_login import (
    DEFAULT_BROWSER_CHANNEL,
    DEFAULT_PROFILE_DIR,
    verify_logged_in_session,
)


DEFAULT_COMPANY_DIR = Path("data") / "runs" / "20260625_updated_company_profiles" / "companies"
RULE_COLUMNS = (
    "pe_vs_industry",
    "pe_vs_historical",
    "roce_over_15",
    "roe_over_15",
    "debt_to_equity_under_0_5",
    "pledged_zero",
    "sales_yoy_growth",
    "profit_growth_over_10",
    "stock_cagr_below_profit_growth",
    "promoter_holding_decrease_under_5",
    "peg_ratio_under_1_5",
    "revenue_quality_guard",
)


class RequestRejectedError(RuntimeError):
    """Raised when Screener serves a non-company page or blocks the export."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download Screener Excel exports for companies passing enough existing rules."
    )
    parser.add_argument(
        "--analysis-csv",
        type=Path,
        default=DEFAULT_COMPANY_DIR / "analysis_filtered" / "company_rule_results.csv",
        help="Filtered rule-analysis CSV used to select companies.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_COMPANY_DIR / "excel",
        help="Directory for downloaded Excel files and manifest.",
    )
    parser.add_argument(
        "--min-passing-rules",
        type=int,
        default=6,
        help="Minimum number of passed prior rules required. Default: 6.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=10.0,
        help="Pause between Screener requests. Default: 10.0.",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(DEFAULT_PROFILE_DIR),
        help="Browser profile containing the saved Screener login.",
    )
    parser.add_argument(
        "--browser-channel",
        default=DEFAULT_BROWSER_CHANNEL,
        help=f"Browser channel to use. Default: {DEFAULT_BROWSER_CHANNEL}",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser while downloading.")
    parser.add_argument("--resume", action="store_true", help="Skip Excel files already in the manifest.")
    parser.add_argument("--limit", type=int, default=None, help="Limit selected companies for testing.")
    return parser.parse_args()


def selected_companies(analysis_csv: Path, minimum_passes: int) -> list[dict[str, str]]:
    with analysis_csv.open(newline="", encoding="utf-8") as csv_file:
        rows = list(csv.DictReader(csv_file))

    # Excel analysis is intentionally limited to companies that meet the configured
    # first-stage pass threshold, reducing authenticated exports and rate-limit risk.
    selected: list[dict[str, str]] = []
    for row in rows:
        pass_count = sum(row.get(rule) == "pass" for rule in RULE_COLUMNS)
        if pass_count >= minimum_passes:
            row["prior_rule_pass_count"] = str(pass_count)
            selected.append(row)
    return selected


def load_manifest(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    return {
        record["company_url"]: record
        for record in json.loads(path.read_text(encoding="utf-8"))
        if "company_url" in record
    }


def save_manifest(path: Path, records: dict[str, dict[str, Any]], companies: list[dict[str, str]]) -> None:
    ordered = [records[row["company_url"]] for row in companies if row["company_url"] in records]
    path.write_text(json.dumps(ordered, indent=2), encoding="utf-8")


def download_excel(page: Page, company: dict[str, str], output_path: Path) -> dict[str, Any]:
    page.goto(company["company_url"], wait_until="domcontentloaded")
    if "/login/" in urlparse(page.url).path.lower():
        raise RequestRejectedError("Screener redirected to login.")

    export_button = page.locator("button[formaction*='/user/company/export/']").first
    try:
        export_button.wait_for(state="visible", timeout=10_000)
    except TimeoutError as exc:
        raise RequestRejectedError("Screener did not return the company export control.") from exc

    with page.expect_download(timeout=30_000) as download_info:
        export_button.click()
    download = download_info.value
    download.save_as(output_path)
    return {
        "download_file": str(output_path.name),
        "suggested_filename": download.suggested_filename,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
    }


def launch_context(playwright: Any, args: argparse.Namespace) -> Any:
    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(Path(args.profile_dir).resolve()),
        "headless": not args.headed,
        "accept_downloads": True,
        "viewport": {"width": 1366, "height": 850},
    }
    if args.browser_channel.lower() != "chromium":
        launch_kwargs["channel"] = args.browser_channel
    return playwright.chromium.launch_persistent_context(**launch_kwargs)


def context_page(context: Any) -> Page:
    return context.pages[0] if context.pages else context.new_page()


def main() -> int:
    args = parse_args()
    if args.delay_seconds < 1:
        print("--delay-seconds must be at least 1 second.", file=sys.stderr)
        return 2

    analysis_csv = args.analysis_csv.resolve()
    if not analysis_csv.is_file():
        raise FileNotFoundError(f"Analysis CSV not found: {analysis_csv}")

    companies = selected_companies(analysis_csv, args.min_passing_rules)
    if args.limit is not None:
        companies = companies[: args.limit]
    print(f"Selected {len(companies)} companies with at least {args.min_passing_rules} passed rules.")

    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / "manifest.json"
    records = load_manifest(manifest_path) if args.resume else {}
    pending = [
        company
        for company in companies
        if not (
            args.resume
            and records.get(company["company_url"], {}).get("download_file")
            and (output_dir / records[company["company_url"]]["download_file"]).is_file()
        )
    ]
    if args.resume:
        print(f"Resuming: {len(companies) - len(pending)} already downloaded, {len(pending)} remaining.")

    playwright = sync_playwright().start()
    try:
        context = launch_context(playwright, args)
    except Error as exc:
        playwright.stop()
        print(f"Could not launch browser: {exc}", file=sys.stderr)
        return 1

    try:
        page = context_page(context)
        if not verify_logged_in_session(page):
            print("Warning: Screener profile is not logged in. Excel downloads require a logged-in session. Skipping Excel crawler.", file=sys.stderr)
            return 0
        for index, company in enumerate(pending, start=1):
            slug = Path(company["html_file"]).stem
            output_path = output_dir / f"{slug}.xlsx"
            print(f"[{index}/{len(pending)}] Exporting {company['company_name']}", flush=True)
            stop = False
            try:
                record: dict[str, Any] = {**company, **download_excel(page, company, output_path)}
            except (Error, TimeoutError, RuntimeError) as exc:
                record = {
                    **company,
                    "error": str(exc),
                    "downloaded_at": datetime.now(timezone.utc).isoformat(),
                }
                print(f"Failed {company['company_name']}: {exc}", file=sys.stderr, flush=True)
                stop = isinstance(exc, RequestRejectedError)
                if not stop:
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        context = launch_context(playwright, args)
                        page = context_page(context)
                    except Error as launch_exc:
                        print(f"Could not relaunch browser: {launch_exc}", file=sys.stderr, flush=True)
                        stop = True

            records[company["company_url"]] = record
            save_manifest(manifest_path, records, companies)
            if stop:
                print("Stopping to avoid additional rejected requests.", file=sys.stderr, flush=True)
                break
            if index < len(pending):
                time.sleep(args.delay_seconds)

        completed = sum(
            bool(records.get(company["company_url"], {}).get("download_file"))
            for company in companies
        )
        print(f"Downloaded {completed}/{len(companies)} Excel files.")
        return 0 if completed == len(companies) else 1
    finally:
        try:
            context.close()
        except Exception:
            pass
        try:
            playwright.stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
