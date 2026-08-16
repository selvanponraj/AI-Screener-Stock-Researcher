"""Orchestrate the Screener download and rule-analysis pipeline."""

from __future__ import annotations

import csv
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_RUN_DIR = PROJECT_ROOT / "data" / "current_run"

FIRST_STAGE_RULE_COLUMNS = (
    "pe_vs_industry",
    "pe_vs_historical",
    "roce_over_10",
    "roe_over_10",
    "debt_to_equity_under_0_5",
    "dpr_yoy_positive",
    "pledged_zero",
    "sales_yoy_growth",
    "profit_growth_over_10",
    "stock_cagr_below_profit_growth",
    "promoter_holding_decrease_under_5",
)
EXCEL_RULE_COLUMNS = ("rule_12_ssgr", "rule_13_cfo_ebitda")
TOTAL_RULES = len(FIRST_STAGE_RULE_COLUMNS) + len(EXCEL_RULE_COLUMNS)
FINAL_RULE_PASS_PERCENTAGE = 100
FINAL_RULE_PASS_THRESHOLD = math.ceil(TOTAL_RULES * FINAL_RULE_PASS_PERCENTAGE / 100)
PIPELINE_STEPS = (
    "login",
    "screens",
    "profiles",
    "html_rules",
    "excel_downloads",
    "excel_rules",
)


@dataclass(frozen=True)
class PipelinePaths:
    run_dir: Path
    screens_dir: Path
    companies_dir: Path
    html_analysis_dir: Path
    excel_dir: Path
    excel_analysis_dir: Path
    final_dir: Path


def current_paths() -> PipelinePaths:
    return PipelinePaths(
        run_dir=CURRENT_RUN_DIR,
        screens_dir=CURRENT_RUN_DIR / "screens",
        companies_dir=CURRENT_RUN_DIR / "companies",
        html_analysis_dir=CURRENT_RUN_DIR / "companies" / "analysis",
        excel_dir=CURRENT_RUN_DIR / "companies" / "excel",
        excel_analysis_dir=CURRENT_RUN_DIR / "companies" / "excel" / "analysis",
        final_dir=CURRENT_RUN_DIR / "final",
    )


def clean_current_run() -> PipelinePaths:
    # Uploaded documents, embeddings, and AI results live outside current_run and
    # intentionally survive a fresh quantitative pipeline run.
    if CURRENT_RUN_DIR.exists():
        shutil.rmtree(CURRENT_RUN_DIR)
    paths = current_paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.final_dir.mkdir(parents=True, exist_ok=True)
    return paths


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def clean_for_step(step_id: str, paths: PipelinePaths) -> None:
    # Invalidate only downstream products. Profile and Excel download steps use
    # their manifests to retain successful files when a partial batch is resumed.
    if step_id == "screens":
        remove_path(paths.screens_dir)
        remove_path(paths.companies_dir)
        remove_path(paths.final_dir)
    elif step_id == "profiles":
        remove_path(paths.companies_dir / "analysis")
        remove_path(paths.companies_dir / "excel")
        remove_path(paths.final_dir)
    elif step_id == "html_rules":
        remove_path(paths.companies_dir / "analysis")
        remove_path(paths.final_dir)
    elif step_id == "excel_downloads":
        remove_path(paths.final_dir)
    elif step_id == "excel_rules":
        remove_path(paths.excel_analysis_dir)
        remove_path(paths.final_dir)


def run_command(args: list[str], log: Callable[[str], None]) -> None:
    log(f"$ {' '.join(args)}")
    process = subprocess.Popen(
        args,
        cwd=PROJECT_ROOT,
        stdout=subprocess.PIPE,
        # Preserve subprocess output order for the live UI log.
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"Command failed with exit code {return_code}: {' '.join(args)}")


def pipeline_commands(paths: PipelinePaths) -> list[tuple[str, list[str]]]:
    return [
        login_command(),
        (
            "Download Screener screen pages",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.screen_page_crawler",
                "--output-dir",
                str(paths.screens_dir),
                "--delay-seconds",
                "3",
            ],
        ),
        (
            "Download company profile HTML",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_profile_crawler",
                "--screen-dir",
                str(paths.screens_dir),
                "--output-dir",
                str(paths.companies_dir),
                "--delay-seconds",
                "5",
                "--quick-ratio-wait-seconds",
                "8",
            ],
        ),
        (
            "Run first 11 HTML rules",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_rule_analyzer",
                "--company-dir",
                str(paths.companies_dir),
                "--output-dir",
                str(paths.html_analysis_dir),
            ],
        ),
        (
            "Download Screener Excel exports",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_excel_crawler",
                "--analysis-csv",
                str(paths.html_analysis_dir / "company_rule_results.csv"),
                "--output-dir",
                str(paths.excel_dir),
                "--min-passing-rules",
                "6",
                "--delay-seconds",
                "10",
            ],
        ),
        (
            "Run SSGR and CFO/EBITDA Excel rules",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_excel_rule_analyzer",
                "--excel-dir",
                str(paths.excel_dir),
                "--output-dir",
                str(paths.excel_analysis_dir),
            ],
        ),
    ]


def login_command() -> tuple[str, list[str]]:
    return (
        "Login / verify Screener session",
        [
            sys.executable,
            "-m",
            "stock_screener_filter.screener_login",
            "--url",
            "https://www.screener.in/",
            "--timeout-seconds",
            "300",
        ],
    )


def step_command(step_id: str, paths: PipelinePaths) -> tuple[str, list[str]]:
    commands = {
        "login": login_command(),
        "screens": pipeline_commands(paths)[1],
        "profiles": (
            "Download company profile HTML",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_profile_crawler",
                "--screen-dir",
                str(paths.screens_dir),
                "--output-dir",
                str(paths.companies_dir),
                "--delay-seconds",
                "5",
                "--quick-ratio-wait-seconds",
                "8",
                "--resume",
            ],
        ),
        "html_rules": pipeline_commands(paths)[3],
        "excel_downloads": (
            "Download Screener Excel exports",
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_excel_crawler",
                "--analysis-csv",
                str(paths.html_analysis_dir / "company_rule_results.csv"),
                "--output-dir",
                str(paths.excel_dir),
                "--min-passing-rules",
                "6",
                "--delay-seconds",
                "10",
                "--resume",
            ],
        ),
        "excel_rules": pipeline_commands(paths)[5],
    }
    if step_id not in commands:
        raise ValueError(f"Unknown pipeline step: {step_id}")
    return commands[step_id]


def pass_count(row: dict[str, str], columns: Iterable[str]) -> int:
    return sum(row.get(column) == "pass" for column in columns)


def value(row: dict[str, str], key: str) -> str:
    item = row.get(key, "")
    return "" if item is None else str(item)


def numeric(row: dict[str, str], key: str) -> float | None:
    raw = value(row, key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def fmt_number(number: float | None) -> str:
    if number is None:
        return "missing"
    return f"{number:.2f}".rstrip("0").rstrip(".")


def fmt_percent(number: float | None) -> str:
    return "missing" if number is None else f"{fmt_number(number)}%"


def count_over(values: Iterable[float | None], threshold: float) -> int:
    return sum(item is not None and item > threshold for item in values)


def count_price_below_profit(html_row: dict[str, str]) -> int:
    periods = ("10_years", "5_years", "3_years", "ttm")
    return sum(
        numeric(html_row, f"stock_cagr_{period}") is not None
        and numeric(html_row, f"profit_growth_{period}") is not None
        and numeric(html_row, f"stock_cagr_{period}") < numeric(html_row, f"profit_growth_{period}")
        for period in periods
    )


def rule_detail(rule: str, html_row: dict[str, str], excel_row: dict[str, str]) -> str:
    stock_pe = numeric(html_row, "stock_pe")
    industry_pe = numeric(html_row, "industry_pe")
    historical = value(html_row, "historical_pe")
    profit_values = [
        numeric(html_row, "profit_growth_10_years"),
        numeric(html_row, "profit_growth_5_years"),
        numeric(html_row, "profit_growth_3_years"),
        numeric(html_row, "profit_growth_ttm"),
    ]
    price_values = [
        numeric(html_row, "stock_cagr_10_years"),
        numeric(html_row, "stock_cagr_5_years"),
        numeric(html_row, "stock_cagr_3_years"),
        numeric(html_row, "stock_cagr_ttm"),
    ]

    details = {
        "pe_vs_industry": (
            f"Stock PE {fmt_number(stock_pe)} <= 1.25 * Industry PE "
            f"{fmt_number(industry_pe)} = {fmt_number(industry_pe * 1.25 if industry_pe is not None else None)}"
        ),
        "pe_vs_historical": (
            f"Stock PE {fmt_number(stock_pe)} vs historical PE {historical or 'missing'}; "
            f"passes {value(html_row, 'historical_pe_passes') or 'missing'} of "
            f"{value(html_row, 'historical_pe_available') or 'missing'} available"
        ),
        "roce_over_10": f"ROCE {fmt_percent(numeric(html_row, 'roce'))} > 10%",
        "roe_over_10": f"ROE {fmt_percent(numeric(html_row, 'roe'))} > 10%",
        "debt_to_equity_under_0_5": f"Debt to equity {fmt_number(numeric(html_row, 'debt_to_equity'))} < 0.5",
        "dpr_yoy_positive": f"DPR YoY {fmt_percent(numeric(html_row, 'dpr_yoy'))} > 0%",
        "pledged_zero": f"Pledged percentage {fmt_percent(numeric(html_row, 'pledged_percentage'))} = 0%",
        "sales_yoy_growth": (
            f"{value(html_row, 'sales_yoy_over_10_count') or '0'} of "
            f"{value(html_row, 'sales_yoy_observations') or '0'} available YoY sales observations >= 10%; "
            f"hit rate {fmt_percent(numeric(html_row, 'sales_yoy_over_10_percentage'))}, threshold 70%"
        ),
        "profit_growth_over_10": (
            "Profit growth 10Y/5Y/3Y/TTM = "
            f"{fmt_percent(profit_values[0])}, {fmt_percent(profit_values[1])}, "
            f"{fmt_percent(profit_values[2])}, {fmt_percent(profit_values[3])}; "
            f"{count_over(profit_values, 10)} periods > 10%, need at least 2"
        ),
        "stock_cagr_below_profit_growth": (
            "Stock CAGR 10Y/5Y/3Y/TTM = "
            f"{fmt_percent(price_values[0])}, {fmt_percent(price_values[1])}, "
            f"{fmt_percent(price_values[2])}, {fmt_percent(price_values[3])}; "
            f"{count_price_below_profit(html_row)} periods below matching profit growth, need at least 2"
        ),
        "promoter_holding_decrease_under_5": (
            f"Promoter holding {value(html_row, 'promoter_first_period') or 'first'} "
            f"{fmt_percent(numeric(html_row, 'promoter_first_value'))} -> "
            f"{value(html_row, 'promoter_last_period') or 'last'} "
            f"{fmt_percent(numeric(html_row, 'promoter_last_value'))}; "
            f"decrease {fmt_percent(numeric(html_row, 'promoter_decrease'))}, threshold < 5%"
        ),
        "rule_12_ssgr": (
            f"SSGR E27:K27 values {value(excel_row, 'ssgr_values_e27_k27') or 'missing'}; "
            f"{value(excel_row, 'ssgr_years_at_or_above_10') or '0'} years >= 10, "
            f"need {value(excel_row, 'ssgr_minimum_years_at_or_above_10') or 'missing'}; "
            f"all years positive = {value(excel_row, 'ssgr_all_years_positive') or 'missing'}"
        ),
        "rule_13_cfo_ebitda": (
            f"CFO/EBITDA last five values {value(excel_row, 'cfo_ebitda_last_five_values') or 'missing'}; "
            f"average {fmt_percent(numeric(excel_row, 'cfo_ebitda_last_five_average'))}, threshold > 50%"
        ),
    }
    return details.get(rule, "")


def build_rule_details(html_row: dict[str, str], excel_row: dict[str, str]) -> list[dict[str, str]]:
    return [
        {
            "name": column,
            "status": html_row.get(column, ""),
            "detail": rule_detail(column, html_row, excel_row),
        }
        for column in FIRST_STAGE_RULE_COLUMNS
    ] + [
        {
            "name": column,
            "status": excel_row.get(column, ""),
            "detail": rule_detail(column, html_row, excel_row),
        }
        for column in EXCEL_RULE_COLUMNS
    ]


def combine_rule_outputs(paths: PipelinePaths, pass_percentage: float | None = None) -> list[dict[str, object]]:
    pct = pass_percentage if pass_percentage is not None else FINAL_RULE_PASS_PERCENTAGE
    threshold = math.ceil(TOTAL_RULES * pct / 100)
    html_csv = paths.html_analysis_dir / "company_rule_results.csv"
    excel_csv = paths.excel_analysis_dir / "excel_rule_results.csv"
    if not html_csv.is_file() or not excel_csv.is_file():
        raise FileNotFoundError("Expected both HTML-rule and Excel-rule CSV outputs.")

    with html_csv.open(newline="", encoding="utf-8") as csv_file:
        html_rows = {row["company_url"]: row for row in csv.DictReader(csv_file)}
    with excel_csv.open(newline="", encoding="utf-8") as csv_file:
        excel_rows = list(csv.DictReader(csv_file))

    combined: list[dict[str, object]] = []
    for excel_row in excel_rows:
        html_row = html_rows.get(excel_row["company_url"])
        if not html_row:
            continue
        first_passes = pass_count(html_row, FIRST_STAGE_RULE_COLUMNS)
        excel_passes = pass_count(excel_row, EXCEL_RULE_COLUMNS)
        total_passes = first_passes + excel_passes
        # Quantitative rules contribute exactly half of the final 100-point score.
        rule_score = round((total_passes / TOTAL_RULES) * 50, 2)
        combined.append(
            {
                "stock_id": stock_id(html_row["company_name"], html_row["company_url"]),
                "company_name": html_row["company_name"],
                "company_url": html_row["company_url"],
                "html_file": html_row["html_file"],
                "excel_file": excel_row["excel_file"],
                "market_categories": html_row.get("market_categories", ""),
                "first_11_pass_count": first_passes,
                "excel_rule_pass_count": excel_passes,
                "total_rule_pass_count": total_passes,
                "total_rule_count": TOTAL_RULES,
                "rule_pass_percentage": round((total_passes / TOTAL_RULES) * 100, 2),
                "rule_score_out_of_50": rule_score,
                "passes_final_rule_filter": total_passes >= threshold,
                **{column: html_row.get(column, "") for column in FIRST_STAGE_RULE_COLUMNS},
                **{column: excel_row.get(column, "") for column in EXCEL_RULE_COLUMNS},
                "rule_details": build_rule_details(html_row, excel_row),
            }
        )

    combined.sort(key=lambda row: (-int(row["total_rule_pass_count"]), str(row["company_name"])))
    paths.final_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.final_dir / "rule_filtered_stocks.json"
    csv_path = paths.final_dir / "rule_filtered_stocks.csv"
    filtered = [row for row in combined if row["passes_final_rule_filter"]]
    json_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
    if filtered:
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=list(filtered[0]))
            writer.writeheader()
            writer.writerows(filtered)
    else:
        csv_path.write_text("", encoding="utf-8")
    return filtered


def read_json_list(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return data if isinstance(data, list) else []


def csv_row_count(path: Path) -> int:
    if not path.is_file():
        return 0
    with path.open(newline="", encoding="utf-8-sig") as csv_file:
        return sum(1 for _ in csv.DictReader(csv_file))


def company_code(company_url: str) -> str:
    parts = [part for part in company_url.rstrip("/").split("/") if part]
    if "company" in parts:
        index = parts.index("company")
        if index + 1 < len(parts):
            return parts[index + 1]
    return parts[-1] if parts else "company"


def pipeline_status(pass_percentage: float | None = None) -> list[dict[str, object]]:
    pct = pass_percentage if pass_percentage is not None else FINAL_RULE_PASS_PERCENTAGE
    paths = current_paths()
    screen_manifest = read_json_list(paths.screens_dir / "manifest.json")
    company_manifest = read_json_list(paths.companies_dir / "manifest.json")
    excel_manifest = read_json_list(paths.excel_dir / "manifest.json")
    filtered = load_current_rule_filtered()

    screen_pages = len(screen_manifest)
    profile_total = len(company_manifest)
    profile_done = sum(bool(record.get("html_file")) for record in company_manifest)
    profile_errors = sum(bool(record.get("error")) for record in company_manifest)
    profile_error_items = [
        f"{company_code(str(record.get('company_url', '')))}: {record.get('error')}"
        for record in company_manifest
        if record.get("error")
    ][:3]
    html_rows = csv_row_count(paths.html_analysis_dir / "company_rule_results.csv")
    excel_total = len(excel_manifest)
    excel_done = sum(bool(record.get("download_file")) for record in excel_manifest)
    excel_errors = sum(bool(record.get("error")) for record in excel_manifest)
    excel_error_items = [
        f"{record.get('company_name') or record.get('company_url')}: {record.get('error')}"
        for record in excel_manifest
        if record.get("error")
    ][:3]
    excel_rows = csv_row_count(paths.excel_analysis_dir / "excel_rule_results.csv")

    # A saved browser directory is only a UI progress hint. Each crawler still
    # verifies the live authenticated session before making protected requests.
    return [
        {
            "id": "login",
            "label": "Login",
            "complete": Path(".browser").exists(),
            "summary": "Saved browser profile present" if Path(".browser").exists() else "Not verified",
        },
        {
            "id": "screens",
            "label": "Screen pages",
            "complete": screen_pages > 0,
            "summary": f"{screen_pages} screen pages fetched",
        },
        {
            "id": "profiles",
            "label": "Company profiles",
            "complete": profile_total > 0 and profile_done == profile_total and profile_errors == 0,
            "summary": (
                f"{profile_done}/{profile_total} profiles fetched"
                + (f", {profile_errors} errors: {'; '.join(profile_error_items)}" if profile_errors else "")
            ),
        },
        {
            "id": "html_rules",
            "label": "Company rules",
            "complete": html_rows > 0,
            "summary": f"{html_rows} non-financial companies analyzed",
        },
        {
            "id": "excel_downloads",
            "label": "Excel downloads",
            "complete": excel_total > 0 and excel_done == excel_total and excel_errors == 0,
            "summary": (
                f"{excel_done}/{excel_total} Excel files fetched"
                + (f", {excel_errors} errors: {'; '.join(excel_error_items)}" if excel_errors else "")
            ),
        },
        {
            "id": "excel_rules",
            "label": "Excel rules",
            "complete": excel_rows > 0,
            "summary": f"{excel_rows} Excel files analyzed; {len(filtered)} stocks pass >={pct:g}%",
        },
    ]


def stock_id(company_name: str, company_url: str) -> str:
    import hashlib
    import re

    base = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-") or "stock"
    # The URL digest disambiguates companies whose normalized names collide.
    digest = hashlib.sha256(company_url.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{digest}"


def run_full_pipeline(
    log: Callable[[str], None],
    progress: Callable[[int, int, str], None] | None = None,
    pass_percentage: float | None = None,
) -> list[dict[str, object]]:
    paths = clean_current_run()
    log(f"Cleaned fresh run directory: {paths.run_dir}")
    commands = pipeline_commands(paths)
    total_steps = len(commands) + 1
    for index, (label, command) in enumerate(commands, start=1):
        if progress:
            progress(index - 1, total_steps, label)
        log(f"Starting: {label}")
        run_command(command, log)
        if progress:
            progress(index, total_steps, label)
    if progress:
        progress(len(commands), total_steps, "Combine 13-rule outputs")
    filtered = combine_rule_outputs(paths, pass_percentage=pass_percentage)
    if progress:
        progress(total_steps, total_steps, "Pipeline complete")
    target_pct = pass_percentage if pass_percentage is not None else FINAL_RULE_PASS_PERCENTAGE
    log(
        f"Saved final >={target_pct}% rule-filtered stocks: "
        f"{paths.final_dir / 'rule_filtered_stocks.csv'}"
    )
    log(f"Rule-filtered stock count: {len(filtered)}")
    return filtered


def run_step(
    step_id: str,
    log: Callable[[str], None],
    progress: Callable[[int, int, str], None] | None = None,
    pass_percentage: float | None = None,
) -> list[dict[str, object]]:
    paths = current_paths()
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    clean_for_step(step_id, paths)
    label, command = step_command(step_id, paths)
    if progress:
        progress(0, 1, label)
    log(f"Starting: {label}")
    run_command(command, log)
    filtered: list[dict[str, object]] = []
    if step_id == "excel_rules":
        filtered = combine_rule_outputs(paths, pass_percentage=pass_percentage)
        log(f"Rule-filtered stock count: {len(filtered)}")
    if progress:
        progress(1, 1, f"{label} complete")
    return filtered


def load_current_rule_filtered() -> list[dict[str, object]]:
    path = current_paths().final_dir / "rule_filtered_stocks.json"
    if not path.is_file():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    paths = current_paths()
    html_rows: dict[str, dict[str, str]] = {}
    excel_rows: dict[str, dict[str, str]] = {}
    html_csv = paths.html_analysis_dir / "company_rule_results.csv"
    excel_csv = paths.excel_analysis_dir / "excel_rule_results.csv"
    if html_csv.is_file():
        with html_csv.open(newline="", encoding="utf-8") as csv_file:
            html_rows = {row["company_url"]: row for row in csv.DictReader(csv_file)}
    if excel_csv.is_file():
        with excel_csv.open(newline="", encoding="utf-8") as csv_file:
            excel_rows = {row["company_url"]: row for row in csv.DictReader(csv_file)}
    for row in rows:
        html_row = html_rows.get(row.get("company_url", ""), {})
        excel_row = excel_rows.get(row.get("company_url", ""), {})
        row["rule_details"] = build_rule_details(
            {**{column: str(row.get(column, "")) for column in FIRST_STAGE_RULE_COLUMNS}, **html_row},
            {**{column: str(row.get(column, "")) for column in EXCEL_RULE_COLUMNS}, **excel_row},
        )
    return rows


CUSTOM_STOCKS_DIR = PROJECT_ROOT / "data" / "custom_stocks"


def load_custom_stocks() -> list[dict[str, object]]:
    if not CUSTOM_STOCKS_DIR.exists():
        return []
    results: list[dict[str, object]] = []
    for json_file in CUSTOM_STOCKS_DIR.glob("*.json"):
        try:
            stock = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(stock, dict) and "stock_id" in stock:
                results.append(stock)
        except Exception:
            continue
    return results


def analyze_single_stock(ticker_or_url: str, log: Callable[[str], None], force: bool = False) -> dict[str, object]:
    input_str = ticker_or_url.strip()
    if not input_str:
        raise ValueError("Ticker or URL cannot be empty.")

    if input_str.startswith("http://") or input_str.startswith("https://"):
        company_url = input_str if input_str.endswith("/") else f"{input_str}/"
    else:
        symbol = input_str.upper().strip("/")
        company_url = f"https://www.screener.in/company/{symbol}/"

    CUSTOM_STOCKS_DIR.mkdir(parents=True, exist_ok=True)

    # Check if stock already exists in custom_stocks cache
    if not force:
        for json_file in CUSTOM_STOCKS_DIR.glob("*.json"):
            try:
                cached = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("company_url", "").rstrip("/") == company_url.rstrip("/"):
                    log(f"Found cached single stock analysis for {cached.get('company_name', symbol)} ({json_file.name}). Skipping re-download.")
                    return cached
            except Exception:
                continue

    log(f"Analyzing single stock ticker from Screener: {company_url}")
    temp_dir = CUSTOM_STOCKS_DIR / "_temp_run"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: Download Profile HTML (Headless mode)
        profiles_dir = temp_dir / "profiles"
        profiles_dir.mkdir(parents=True, exist_ok=True)
        log(f"Fetching profile HTML for {company_url}...")
        run_command(
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_profile_crawler",
                "--company-urls",
                company_url,
                "--output-dir",
                str(profiles_dir),
                "--delay-seconds",
                "1",
            ],
            log,
        )

        # Step 2: Analyze HTML Rules
        html_analysis_dir = profiles_dir / "analysis"
        log("Running 11 quantitative HTML rules...")
        run_command(
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_rule_analyzer",
                "--company-dir",
                str(profiles_dir),
                "--output-dir",
                str(html_analysis_dir),
            ],
            log,
        )

        html_csv = html_analysis_dir / "company_rule_results.csv"
        if not html_csv.is_file():
            raise RuntimeError(f"HTML rule analysis failed to produce results for {company_url}")

        with html_csv.open(newline="", encoding="utf-8") as csv_file:
            html_rows = list(csv.DictReader(csv_file))
        if not html_rows:
            raise RuntimeError(f"No HTML rule output found for {company_url}")
        html_row = html_rows[0]

        # Step 3: Download Excel Export
        excel_dir = profiles_dir / "excel"
        log("Downloading Screener Excel export...")
        run_command(
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_excel_crawler",
                "--analysis-csv",
                str(html_csv),
                "--output-dir",
                str(excel_dir),
                "--min-passing-rules",
                "0",  # Always fetch Excel for single stock analysis
                "--delay-seconds",
                "1",
            ],
            log,
        )

        # Step 4: Analyze Excel Rules
        excel_analysis_dir = excel_dir / "analysis"
        log("Running Excel SSGR & CFO/EBITDA rules...")
        run_command(
            [
                sys.executable,
                "-m",
                "stock_screener_filter.company_excel_rule_analyzer",
                "--excel-dir",
                str(excel_dir),
                "--output-dir",
                str(excel_analysis_dir),
            ],
            log,
        )

        excel_csv = excel_analysis_dir / "excel_rule_results.csv"
        excel_row: dict[str, str] = {}
        if excel_csv.is_file():
            with excel_csv.open(newline="", encoding="utf-8") as csv_file:
                excel_rows = list(csv.DictReader(csv_file))
                if excel_rows:
                    excel_row = excel_rows[0]

        first_passes = pass_count(html_row, FIRST_STAGE_RULE_COLUMNS)
        excel_passes = pass_count(excel_row, EXCEL_RULE_COLUMNS)
        total_passes = first_passes + excel_passes
        rule_score = round((total_passes / TOTAL_RULES) * 50, 2)

        s_id = stock_id(html_row["company_name"], html_row["company_url"])
        stock_data: dict[str, object] = {
            "stock_id": s_id,
            "company_name": html_row["company_name"],
            "company_url": html_row["company_url"],
            "html_file": html_row.get("html_file", ""),
            "excel_file": excel_row.get("excel_file", ""),
            "market_categories": html_row.get("market_categories", ""),
            "first_11_pass_count": first_passes,
            "excel_rule_pass_count": excel_passes,
            "total_rule_pass_count": total_passes,
            "total_rule_count": TOTAL_RULES,
            "rule_pass_percentage": round((total_passes / TOTAL_RULES) * 100, 2),
            "rule_score_out_of_50": rule_score,
            "passes_final_rule_filter": True,
            "is_custom_single_stock": True,
            **{column: html_row.get(column, "") for column in FIRST_STAGE_RULE_COLUMNS},
            **{column: excel_row.get(column, "") for column in EXCEL_RULE_COLUMNS},
            "rule_details": build_rule_details(html_row, excel_row),
        }

        save_path = CUSTOM_STOCKS_DIR / f"{s_id}.json"
        save_path.write_text(json.dumps(stock_data, indent=2), encoding="utf-8")
        log(f"Successfully analyzed {html_row['company_name']}! Saved to custom stocks.")
        return stock_data
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Stock Screener & Rules Pipeline Runner")
    parser.add_argument("action", nargs="?", default="run", choices=["run", "status"])
    parser.add_argument("--step", help="Specific step to run: login, screens, profiles, html_rules, excel_downloads, excel_rules")
    parser.add_argument("--ticker", help="Analyze a single stock ticker or URL (e.g. TCS or https://www.screener.in/company/TCS/)")
    parser.add_argument("--force", action="store_true", help="Bypass cache and force re-fetching single stock analysis")
    args = parser.parse_args()

    def log(msg: str) -> None:
        print(f"[PIPELINE] {msg}")

    if args.ticker:
        analyze_single_stock(args.ticker, log, force=args.force)
        return 0

    if args.action == "status":
        print(json.dumps(pipeline_status(), indent=2))
        return 0

    if args.step:
        run_step(args.step, log)
    else:
        run_full_pipeline(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())

