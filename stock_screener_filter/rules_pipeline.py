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

from bs4 import BeautifulSoup

from stock_screener_filter import company_rule_analyzer


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CURRENT_RUN_DIR = PROJECT_ROOT / "data" / "current_run"

FIRST_STAGE_RULE_COLUMNS = (
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
            "Run first 12 HTML rules",
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


_FALLBACK_CACHE: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}


def get_cached_html_fallback(valid_path: Path, sales_cagr_3y: float | None, roe: float | None) -> tuple[dict[str, Any], dict[str, Any]]:
    key = str(valid_path.resolve())
    if key not in _FALLBACK_CACHE:
        try:
            soup = BeautifulSoup(valid_path.read_text(encoding="utf-8"), "html.parser")
            cfo_fb = company_rule_analyzer.calculate_fallback_cfo_ebitda_pat(soup)
            ssgr_fb = company_rule_analyzer.calculate_fallback_ssgr(soup, sales_cagr_3y, roe)
            _FALLBACK_CACHE[key] = (cfo_fb, ssgr_fb)
        except Exception:
            _FALLBACK_CACHE[key] = ({}, {})
    return _FALLBACK_CACHE[key]


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

    mcap_val = numeric(html_row, "Market Cap") or numeric(html_row, "market_cap") or numeric(excel_row, "excel_market_cap")
    if mcap_val is None:
        html_file = html_row.get("html_file")
        if html_file:
            html_path = current_paths().companies_dir / "html" / html_file
            if html_path.is_file():
                try:
                    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
                    mcap_val = company_rule_analyzer.quick_ratios(soup).get("Market Cap")
                except Exception:
                    pass

    if mcap_val is not None and mcap_val > 0:
        market_cat = "Large-Cap" if mcap_val > 50000 else ("Mid-Cap" if mcap_val >= 10000 else "Small-Cap")
        mcap_str = f"₹{fmt_number(mcap_val)} Cr"
    else:
        market_cat = value(html_row, 'sales_market_category') or 'Small-Cap'
        mcap_str = 'N/A'

    ssgr_val = numeric(excel_row, 'ssgr_3y_avg') if numeric(excel_row, 'ssgr_3y_avg') is not None else numeric(html_row, 'ssgr_3y_avg')
    ssgr_sales_cagr = numeric(excel_row, 'excel_sales_cagr_3y') if numeric(excel_row, 'excel_sales_cagr_3y') is not None else numeric(html_row, 'sales_growth_3_years')
    cfo_val = numeric(excel_row, 'cum_cfo_5y') if numeric(excel_row, 'cum_cfo_5y') is not None else numeric(html_row, 'cum_cfo_5y')
    ebitda_val = numeric(excel_row, 'cum_ebitda_5y') if numeric(excel_row, 'cum_ebitda_5y') is not None else numeric(html_row, 'cum_ebitda_5y')
    cfo_ebitda_ratio = numeric(excel_row, 'cfo_ebitda_cum_ratio') if numeric(excel_row, 'cfo_ebitda_cum_ratio') is not None else numeric(html_row, 'cfo_ebitda_cum_ratio')
    cfo_pat_ratio = numeric(excel_row, 'cfo_pat_cum_ratio') if numeric(excel_row, 'cfo_pat_cum_ratio') is not None else numeric(html_row, 'cfo_pat_cum_ratio')

    if (ssgr_val is None or cfo_val is None) and (html_row.get("html_file") or html_row.get("company_url") or html_row.get("company_name")):
        html_file = str(html_row.get("html_file") or "")
        target_sym = normalize_company_symbol(html_row.get("company_url")) or normalize_company_symbol(html_row.get("stock_id"))
        prefix = html_file.split("_")[0].upper() if html_file else ""
        possible_paths = [
            current_paths().companies_dir / "html" / html_file if html_file else None,
            current_paths().companies_dir / "html" / f"{target_sym}.html" if target_sym else None,
            current_paths().companies_dir / "html" / f"{prefix}.html" if prefix else None,
            CUSTOM_STOCKS_DIR / "html" / html_file if html_file else None,
            CUSTOM_STOCKS_DIR / "html" / f"{target_sym}.html" if target_sym else None,
            CUSTOM_STOCKS_DIR / "html" / f"{prefix}.html" if prefix else None,
        ]
        valid_path = next((p for p in possible_paths if p and p.is_file()), None)

        if valid_path:
            cfo_fb, ssgr_fb = get_cached_html_fallback(valid_path, ssgr_sales_cagr, numeric(html_row, "roe"))
            if cfo_val is None:
                cfo_val = numeric(cfo_fb, "cum_cfo_5y")
                ebitda_val = numeric(cfo_fb, "cum_ebitda_5y")
                cfo_ebitda_ratio = numeric(cfo_fb, "cfo_ebitda_cum_ratio")
                cfo_pat_ratio = numeric(cfo_fb, "cfo_pat_cum_ratio")
            if ssgr_val is None:
                ssgr_val = numeric(ssgr_fb, "ssgr_3y_avg")

    details = {
        "pe_vs_industry": (
            f"Stock PE {fmt_number(stock_pe)} <= 1.1 * Industry PE "
            f"{fmt_number(industry_pe)} = {fmt_number(industry_pe * 1.1 if industry_pe is not None else None)}"
        ),
        "pe_vs_historical": (
            f"Stock PE {fmt_number(stock_pe)} vs historical PE {historical or 'missing'}; "
            f"passes {value(html_row, 'historical_pe_passes') or 'missing'} of "
            f"{value(html_row, 'historical_pe_available') or 'missing'} available"
        ),
        "roce_over_15": f"ROCE {fmt_percent(numeric(html_row, 'roce'))} > 15%",
        "roe_over_15": f"ROE {fmt_percent(numeric(html_row, 'roe'))} > 15%",
        "debt_to_equity_under_0_5": f"Debt to equity {fmt_number(numeric(html_row, 'debt_to_equity'))} < 0.5",
        "pledged_zero": f"Pledged percentage {fmt_percent(numeric(html_row, 'pledged_percentage'))} = 0%",
        "sales_yoy_growth": (
            f"Sales CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'sales_growth_3_years'))}, {fmt_percent(numeric(html_row, 'sales_growth_5_years'))} (need >= {fmt_number(numeric(html_row, 'sales_min_cagr')) or ('10' if market_cat == 'Large-Cap' else ('12' if market_cat == 'Mid-Cap' else '15'))}%); "
            f"YoY hit rate {value(html_row, 'sales_yoy_over_10_count') or '0'}/{value(html_row, 'sales_yoy_observations') or '0'} >= {fmt_number(numeric(html_row, 'sales_min_yoy_growth')) or ('10' if market_cat in ('Large-Cap', 'Mid-Cap') else '12')}% "
            f"({fmt_percent(numeric(html_row, 'sales_yoy_over_10_percentage'))}, threshold >= 60%)"
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
        "peg_ratio_under_1_5": (
            (
                f"Stock PE {fmt_number(numeric(html_row, 'stock_pe'))}, "
                f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))}; "
                f"Data missing; threshold <= 1.5"
            )
            if numeric(html_row, "stock_pe") is None or numeric(html_row, "profit_growth_3_years") is None or numeric(html_row, "profit_growth_5_years") is None
            else (
                (
                    f"Stock PE {fmt_number(numeric(html_row, 'stock_pe'))}, "
                    f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))}; "
                    f"Fail: non-positive profit growth; threshold <= 1.5"
                )
                if (numeric(html_row, "profit_growth_3_years") or 0) <= 0 or (numeric(html_row, "profit_growth_5_years") or 0) <= 0
                else (
                    (
                        f"Stock PE {fmt_number(numeric(html_row, 'stock_pe'))}, "
                        f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))}; "
                        f"Effective CAGR {fmt_percent(numeric(html_row, 'effective_cagr'))}, Raw PEG {fmt_number(numeric(html_row, 'peg_ratio'))}; "
                        f"Fail: 3Y growth ({fmt_percent(numeric(html_row, 'profit_growth_3_years'))}) < 75% of 5Y growth ({fmt_percent((numeric(html_row, 'profit_growth_5_years') or 0) * 0.75)}); threshold <= 1.5"
                    )
                    if (numeric(html_row, "profit_growth_3_years") or 0) < 0.75 * (numeric(html_row, "profit_growth_5_years") or 0)
                    else (
                        f"Stock PE {fmt_number(numeric(html_row, 'stock_pe'))}, "
                        f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))}; "
                        f"Effective CAGR {fmt_percent(numeric(html_row, 'effective_cagr'))}, "
                        f"PEG {fmt_number(numeric(html_row, 'peg_ratio'))}; threshold <= 1.5"
                    )
                )
            )
        ),
        "revenue_quality_guard": (
            (
                f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))} vs "
                f"Sales CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'sales_growth_3_years'))}, {fmt_percent(numeric(html_row, 'sales_growth_5_years'))}; "
                f"Data missing"
            )
            if numeric(html_row, "profit_growth_3_years") is None and numeric(html_row, "profit_growth_5_years") is None
            else (
                (
                    f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))} vs "
                    f"Sales CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'sales_growth_3_years'))}, {fmt_percent(numeric(html_row, 'sales_growth_5_years'))}; "
                    f"Operating leverage confirmed (Profit >= Sales for 3Y or 5Y)"
                )
                if ((numeric(html_row, "profit_growth_3_years") or -999) >= (numeric(html_row, "sales_growth_3_years") or 999)) or ((numeric(html_row, "profit_growth_5_years") or -999) >= (numeric(html_row, "sales_growth_5_years") or 999))
                else (
                    f"Profit CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'profit_growth_3_years'))}, {fmt_percent(numeric(html_row, 'profit_growth_5_years'))} vs "
                    f"Sales CAGR 3Y/5Y = {fmt_percent(numeric(html_row, 'sales_growth_3_years'))}, {fmt_percent(numeric(html_row, 'sales_growth_5_years'))}; "
                    f"Fail: Profit growth lagged Sales growth for both 3Y and 5Y"
                )
            )
        ),
        "rule_12_ssgr": (
            f"3Y Avg SSGR {fmt_percent(ssgr_val)} > 10% "
            f"and >= 3Y Sales CAGR {fmt_percent(ssgr_sales_cagr)}"
        ),
        "rule_13_cfo_ebitda": (
            f"Cum 5Y CFO/EBITDA = {fmt_number(cfo_val)} / "
            f"{fmt_number(ebitda_val)} = "
            f"{fmt_percent(cfo_ebitda_ratio)} (>= 65%); "
            f"CFO/PAT = {fmt_percent(cfo_pat_ratio)} (>= 80%)"
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
            "status": excel_row.get(column, "") or html_row.get(column, ""),
            "detail": rule_detail(column, html_row, excel_row),
        }
        for column in EXCEL_RULE_COLUMNS
    ]


def combine_rule_outputs(paths: PipelinePaths, pass_percentage: float | None = None) -> list[dict[str, object]]:
    pct = pass_percentage if pass_percentage is not None else FINAL_RULE_PASS_PERCENTAGE
    html_csv = paths.html_analysis_dir / "company_rule_results.csv"
    if not html_csv.is_file():
        return []

    excel_csv = paths.excel_analysis_dir / "excel_rule_results.csv"
    excel_by_url: dict[str, dict[str, str]] = {}
    excel_by_symbol: dict[str, dict[str, str]] = {}
    if excel_csv.is_file():
        with excel_csv.open(newline="", encoding="utf-8") as csv_file:
            for row in csv.DictReader(csv_file):
                url = row.get("company_url", "").rstrip("/")
                if url:
                    excel_by_url[url] = row
                    sym = normalize_company_symbol(url)
                    if sym:
                        excel_by_symbol[sym] = row

    with html_csv.open(newline="", encoding="utf-8") as csv_file:
        html_rows = list(csv.DictReader(csv_file))

    combined: list[dict[str, object]] = []
    for html_row in html_rows:
        company_url = html_row.get("company_url", "")
        clean_url = company_url.rstrip("/")
        target_sym = normalize_company_symbol(company_url) or normalize_company_symbol(html_row.get("company_name", ""))
        
        excel_row = excel_by_url.get(clean_url) or excel_by_symbol.get(target_sym, {})

        html_file_name = f"{target_sym}.html" if (paths.companies_dir / "html" / f"{target_sym}.html").is_file() else html_row.get("html_file", "")
        excel_file_name = f"{target_sym}.xlsx" if (paths.companies_dir / "excel" / f"{target_sym}.xlsx").is_file() else excel_row.get("excel_file", "")

        first_passes = pass_count(html_row, FIRST_STAGE_RULE_COLUMNS)
        excel_passes = pass_count(excel_row, EXCEL_RULE_COLUMNS)
        total_passes = first_passes + excel_passes

        has_excel = bool(excel_row)
        total_rule_count = TOTAL_RULES if has_excel else len(FIRST_STAGE_RULE_COLUMNS)
        pass_pct = round((total_passes / total_rule_count) * 100, 2)
        rule_score = round((total_passes / total_rule_count) * 50, 2)
        passes_filter = pass_pct >= pct

        combined.append(
            {
                "stock_id": stock_id(html_row.get("company_name", ""), company_url),
                "company_name": html_row.get("company_name", ""),
                "company_url": company_url,
                "market_categories": html_row.get("market_categories", ""),
                "first_12_pass_count": first_passes,
                "excel_rule_pass_count": excel_passes,
                "total_rule_pass_count": total_passes,
                "total_rule_count": total_rule_count,
                "rule_pass_percentage": pass_pct,
                "rule_score_out_of_50": rule_score,
                "passes_final_rule_filter": passes_filter,
                **html_row,
                **excel_row,
                "html_file": html_file_name,
                "excel_file": excel_file_name,
                "rule_details": build_rule_details(html_row, excel_row),
            }
        )

    combined.sort(key=lambda row: (-float(row["rule_pass_percentage"]), str(row["company_name"])))
    paths.final_dir.mkdir(parents=True, exist_ok=True)
    json_path = paths.final_dir / "rule_filtered_stocks.json"
    csv_path = paths.final_dir / "rule_filtered_stocks.csv"
    filtered = [row for row in combined if row["passes_final_rule_filter"]]
    json_path.write_text(json.dumps(filtered, indent=2), encoding="utf-8")
    if filtered:
        fieldnames: list[str] = []
        for row in filtered:
            for k in row.keys():
                if k not in fieldnames:
                    fieldnames.append(k)
        with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
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
    sym = normalize_company_symbol(company_url) or normalize_company_symbol(company_name)
    if sym:
        return sym
    import hashlib
    import re

    base = re.sub(r"[^a-z0-9]+", "-", company_name.lower()).strip("-") or "stock"
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
            {**html_row, **{column: str(row.get(column, "")) for column in FIRST_STAGE_RULE_COLUMNS}},
            {**excel_row, **{column: str(row.get(column, "")) for column in EXCEL_RULE_COLUMNS}},
        )
    return rows


def load_all_current_run_stocks(pass_percentage: float = 100.0) -> list[dict[str, object]]:
    paths = current_paths()
    html_csv = paths.html_analysis_dir / "company_rule_results.csv"
    if not html_csv.is_file():
        return []

    excel_csv = paths.excel_analysis_dir / "excel_rule_results.csv"
    excel_by_url: dict[str, dict[str, str]] = {}
    if excel_csv.is_file():
        with excel_csv.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                url = row.get("company_url", "").rstrip("/")
                if url:
                    excel_by_url[url] = row

    results: list[dict[str, object]] = []
    with html_csv.open(newline="", encoding="utf-8") as f:
        for html_row in csv.DictReader(f):
            company_url = html_row.get("company_url", "")
            clean_url = company_url.rstrip("/")
            excel_row = excel_by_url.get(clean_url, {})

            first_passes = pass_count(html_row, FIRST_STAGE_RULE_COLUMNS)
            excel_passes = pass_count(excel_row, EXCEL_RULE_COLUMNS)
            total_passes = first_passes + excel_passes

            has_excel = bool(excel_row)
            total_rule_count = TOTAL_RULES if has_excel else len(FIRST_STAGE_RULE_COLUMNS)
            pass_pct = round((total_passes / total_rule_count) * 100, 2)
            rule_score = round((total_passes / total_rule_count) * 50, 2)
            passes_filter = pass_pct >= pass_percentage

            s_id = stock_id(html_row.get("company_name", ""), company_url)
            stock_data: dict[str, object] = {
                "stock_id": s_id,
                "company_name": html_row.get("company_name", ""),
                "company_url": company_url,
                "first_12_pass_count": first_passes,
                "excel_rule_pass_count": excel_passes,
                "total_rule_pass_count": total_passes,
                "total_rule_count": total_rule_count,
                "rule_pass_percentage": pass_pct,
                "rule_score_out_of_50": rule_score,
                "passes_final_rule_filter": passes_filter,
                "stock_source": "current_run",
                **html_row,
                **excel_row,
                "rule_details": build_rule_details(html_row, excel_row),
            }
            results.append(stock_data)
    return results


CUSTOM_STOCKS_DIR = PROJECT_ROOT / "data" / "custom_stocks"


def load_custom_stocks() -> list[dict[str, object]]:
    if not CUSTOM_STOCKS_DIR.exists():
        return []
    
    seen_symbols: dict[str, tuple[Path, dict[str, object]]] = {}
    for json_file in CUSTOM_STOCKS_DIR.rglob("*.json"):
        if json_file.name == "custom_rule_results.json":
            continue
        try:
            stock = json.loads(json_file.read_text(encoding="utf-8"))
            if isinstance(stock, dict) and "stock_id" in stock:
                sym = normalize_company_symbol(stock.get("company_url")) or normalize_company_symbol(stock.get("stock_id")) or json_file.stem.upper()
                if not sym:
                    continue
                
                stock["stock_id"] = sym
                if (CUSTOM_STOCKS_DIR / "html" / f"{sym}.html").is_file():
                    stock["html_file"] = f"{sym}.html"
                if (CUSTOM_STOCKS_DIR / "excel" / f"{sym}.xlsx").is_file():
                    stock["excel_file"] = f"{sym}.xlsx"

                # Deduplicate: If duplicate symbol found, keep the newer file and delete the older one
                if sym in seen_symbols:
                    old_path, old_stock = seen_symbols[sym]
                    if json_file.stat().st_mtime > old_path.stat().st_mtime:
                        old_path.unlink(missing_ok=True)
                        seen_symbols[sym] = (json_file, stock)
                    else:
                        json_file.unlink(missing_ok=True)
                else:
                    seen_symbols[sym] = (json_file, stock)
        except Exception:
            continue

    results: list[dict[str, object]] = []
    for json_path, stock in seen_symbols.values():
        stock_str_dict = {k: str(v) if v is not None else "" for k, v in stock.items()}
        stock["rule_details"] = build_rule_details(stock_str_dict, stock_str_dict)
        results.append(stock)
    return results


def analyze_single_stock(ticker_or_url: str, log: Callable[[str], None], force: bool = False, target_scope: str | None = None) -> dict[str, object]:
    input_str = ticker_or_url.strip()
    if not input_str:
        raise ValueError("Ticker or URL cannot be empty.")

    company_url, symbol = resolve_screener_url(input_str)

    CUSTOM_STOCKS_DIR.mkdir(parents=True, exist_ok=True)
    paths = current_paths()

    # Determine target scope if unspecified
    if not target_scope:
        current_csv = paths.html_analysis_dir / "company_rule_results.csv"
        is_curr = False
        if current_csv.is_file():
            with current_csv.open(newline="", encoding="utf-8") as f:
                is_curr = any(
                    normalize_company_symbol(r.get("company_url")) == symbol or
                    normalize_company_symbol(r.get("stock_id")) == symbol
                    for r in csv.DictReader(f)
                )
        target_scope = "current_run" if is_curr else "custom_stocks"

    # Check if stock already exists in custom_stocks cache
    if not force and target_scope == "custom_stocks":
        for json_file in CUSTOM_STOCKS_DIR.glob("*.json"):
            try:
                cached = json.loads(json_file.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("company_url", "").rstrip("/") == company_url.rstrip("/"):
                    log(f"Found cached single stock analysis for {cached.get('company_name', symbol)} ({json_file.name}). Skipping re-download.")
                    return cached
            except Exception:
                continue

    log(f"Analyzing single stock ticker from Screener: {company_url} [Target Scope: {target_scope}]")
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
        log("Running quantitative HTML rules...")
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
        excel_passes = pass_count(excel_row, EXCEL_RULE_COLUMNS) if excel_row else pass_count(html_row, EXCEL_RULE_COLUMNS)
        total_passes = first_passes + excel_passes
        rule_score = round((total_passes / TOTAL_RULES) * 50, 2)

        # Determine target directory
        target_sym = normalize_company_symbol(company_url) or normalize_company_symbol(html_row.get("company_name", ""))

        s_id = stock_id(html_row["company_name"], html_row["company_url"])
        stock_data: dict[str, object] = {
            "stock_id": s_id,
            "company_name": html_row["company_name"],
            "company_url": html_row["company_url"],
            "market_categories": html_row.get("market_categories", ""),
            "first_12_pass_count": first_passes,
            "excel_rule_pass_count": excel_passes,
            "total_rule_pass_count": total_passes,
            "total_rule_count": TOTAL_RULES,
            "rule_pass_percentage": round((total_passes / TOTAL_RULES) * 100, 2),
            "rule_score_out_of_50": rule_score,
            "passes_final_rule_filter": True,
            "is_custom_single_stock": True,
            **html_row,
            **excel_row,
            "html_file": f"{target_sym}.html",
            "excel_file": f"{target_sym}.xlsx" if excel_row else "",
            "rule_details": build_rule_details(html_row, excel_row),
        }

        target_base = paths.companies_dir if target_scope == "current_run" else CUSTOM_STOCKS_DIR
        target_html_dir = target_base / "html"
        target_excel_dir = target_base / "excel"
        target_html_dir.mkdir(parents=True, exist_ok=True)
        target_excel_dir.mkdir(parents=True, exist_ok=True)

        temp_html_name = html_row.get("html_file", "")
        if temp_html_name:
            possible_html_paths = [
                profiles_dir / temp_html_name,
                profiles_dir / "html" / temp_html_name,
                profiles_dir / "html" / Path(temp_html_name).name,
                profiles_dir / Path(temp_html_name).name,
            ]
            valid_html_path = next((p for p in possible_html_paths if p.is_file()), None)
            if valid_html_path:
                for old_f in target_html_dir.glob(f"{target_sym}*"):
                    old_f.unlink(missing_ok=True)
                shutil.copy2(valid_html_path, target_html_dir / f"{target_sym}.html")
                stock_data["html_file"] = f"{target_sym}.html"

        # Copy any downloaded Excel exports from temporary excel_dir to target_excel_dir
        if excel_dir.is_dir():
            for xfile in excel_dir.glob("*.xlsx"):
                for old_f in target_excel_dir.glob(f"{target_sym}*"):
                    old_f.unlink(missing_ok=True)
                shutil.copy2(xfile, target_excel_dir / f"{target_sym}.xlsx")
                stock_data["excel_file"] = f"{target_sym}.xlsx"

        stock_data["is_current_run"] = (target_scope == "current_run")
        upsert_current_run_stock(stock_data, target_scope=target_scope)
        log(f"Successfully analyzed {html_row['company_name']}! Target scope: {target_scope}")
        return stock_data
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


def normalize_company_symbol(url_or_name: str | None) -> str:
    import re
    if not url_or_name:
        return ""
    clean_url = re.sub(r"/consolidated/?$", "", str(url_or_name).strip().rstrip("/"), flags=re.I)
    return clean_url.split("/")[-1].upper()


def resolve_screener_url(identifier: str) -> tuple[str, str]:
    """Resolve an input string, stock_id, or company name to a valid Screener consolidated company URL and symbol."""
    import re
    cleaned = identifier.strip()
    if not cleaned:
        return "", ""

    sym = ""
    if cleaned.startswith("http://") or cleaned.startswith("https://"):
        sym = normalize_company_symbol(cleaned)
    else:
        paths = current_paths()
        csv_paths = [
            CUSTOM_STOCKS_DIR / "analysis" / "custom_rule_results.csv",
            paths.html_analysis_dir / "company_rule_results.csv",
        ]
        for c_path in csv_paths:
            if c_path.is_file():
                try:
                    with c_path.open(newline="", encoding="utf-8") as f:
                        for row in csv.DictReader(f):
                            row_name = row.get("company_name", "")
                            row_url = row.get("company_url", "")
                            row_id = row.get("stock_id", "") or (stock_id(row_name, row_url) if row_name and row_url else "")
                            if (row_id and row_id.lower() == cleaned.lower()) or (row_name and row_name.lower() == cleaned.lower()):
                                sym = normalize_company_symbol(row_url) or normalize_company_symbol(row_name)
                                break
                except Exception:
                    continue
            if sym:
                break

    if not sym:
        sym = normalize_company_symbol(cleaned)

    return f"https://www.screener.in/company/{sym}/consolidated/", sym


def _upsert_csv_json_entry(csv_path: Path, json_path: Path, entry: dict[str, object], target_sym: str) -> None:
    """Upsert a single record into specified CSV and JSON files matching target_sym."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)

    if csv_path.is_file():
        with csv_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys()) if rows else list(entry.keys())

        updated = False
        for row in rows:
            row_sym = normalize_company_symbol(row.get("company_url")) or normalize_company_symbol(row.get("stock_id"))
            if target_sym and row_sym == target_sym:
                for k in fieldnames:
                    if k in entry and entry[k] is not None:
                        row[k] = str(entry[k])
                updated = True
                break

        if not updated:
            new_row = {k: (str(entry.get(k, "")) if entry.get(k) is not None else "") for k in fieldnames}
            rows.append(new_row)

        with csv_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            fieldnames = list(entry.keys())
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow({k: (str(v) if v is not None else "") for k, v in entry.items()})

    if json_path.is_file():
        try:
            records = json.loads(json_path.read_text(encoding="utf-8"))
            updated_json = False
            for i, rec in enumerate(records):
                rec_sym = normalize_company_symbol(rec.get("company_url")) or normalize_company_symbol(rec.get("stock_id"))
                if target_sym and rec_sym == target_sym:
                    records[i] = entry
                    updated_json = True
                    break
            if not updated_json:
                records.append(entry)
            json_path.write_text(json.dumps(records, indent=2), encoding="utf-8")
        except Exception:
            json_path.write_text(json.dumps([entry], indent=2), encoding="utf-8")
    else:
        json_path.write_text(json.dumps([entry], indent=2), encoding="utf-8")


def upsert_current_run_stock(stock_data: dict[str, object], target_scope: str | None = None) -> None:
    """Upsert an evaluated stock entry strictly into target_scope (current_run vs custom_stocks)."""
    paths = current_paths()
    s_id = stock_data.get("stock_id")
    c_url = stock_data.get("company_url")
    target_sym = normalize_company_symbol(c_url) or normalize_company_symbol(s_id)

    if not target_scope:
        is_current = bool(stock_data.get("is_current_run"))
        if not is_current:
            current_csv = paths.html_analysis_dir / "company_rule_results.csv"
            if current_csv.is_file():
                with current_csv.open(newline="", encoding="utf-8") as f:
                    is_current = any(
                        normalize_company_symbol(r.get("company_url")) == target_sym or
                        normalize_company_symbol(r.get("stock_id")) == target_sym
                        for r in csv.DictReader(f)
                    )
        target_scope = "current_run" if is_current else "custom_stocks"

    if target_scope == "current_run":
        # 1. Update company_rule_results (HTML analysis)
        html_csv = paths.html_analysis_dir / "company_rule_results.csv"
        html_json = paths.html_analysis_dir / "company_rule_results.json"
        _upsert_csv_json_entry(html_csv, html_json, stock_data, target_sym)

        # 2. Update excel_rule_results (Excel analysis) if excel results exist
        if stock_data.get("excel_file"):
            excel_csv = paths.excel_analysis_dir / "excel_rule_results.csv"
            excel_json = paths.excel_analysis_dir / "excel_rule_results.json"
            excel_entry = {
                "company_name": stock_data.get("company_name", ""),
                "company_url": stock_data.get("company_url", ""),
                "excel_file": stock_data.get("excel_file", ""),
                "rule_12_ssgr": stock_data.get("rule_12_ssgr", ""),
                "rule_13_cfo_ebitda": stock_data.get("rule_13_cfo_ebitda", ""),
                "ssgr": stock_data.get("ssgr", ""),
                "cum_5y_cfo_ebitda": stock_data.get("cum_5y_cfo_ebitda", ""),
                "cfo_pat": stock_data.get("cfo_pat", ""),
            }
            _upsert_csv_json_entry(excel_csv, excel_json, excel_entry, target_sym)

        # 3. Combine outputs & update final/rule_filtered_stocks.csv and final/rule_filtered_stocks.json
        combine_rule_outputs(paths)
    else:
        custom_analysis_dir = CUSTOM_STOCKS_DIR / "analysis"
        custom_analysis_dir.mkdir(parents=True, exist_ok=True)

        single_json = custom_analysis_dir / f"{target_sym}.json"
        single_json.write_text(json.dumps(stock_data, indent=2), encoding="utf-8")

        for old_j in CUSTOM_STOCKS_DIR.glob(f"{target_sym}*.json"):
            if old_j.resolve() != single_json.resolve():
                old_j.unlink(missing_ok=True)

        c_csv = custom_analysis_dir / "custom_rule_results.csv"
        c_json = custom_analysis_dir / "custom_rule_results.json"
        _upsert_csv_json_entry(c_csv, c_json, stock_data, target_sym)


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

