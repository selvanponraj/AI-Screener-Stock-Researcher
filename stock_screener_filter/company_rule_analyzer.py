"""Evaluate screening rules from locally saved Screener company-profile HTML."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from pathlib import Path
from typing import Any, Optional

from bs4 import BeautifulSoup, Tag


DEFAULT_COMPANY_DIR = Path("data") / "runs" / "20260624_company_profiles" / "companies"
NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
FINANCIAL_MARKERS = ("financial services", "bank", "insurance", "nbfc", "finance")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate stock-selection rules from downloaded Screener company HTML."
    )
    parser.add_argument(
        "--company-dir",
        type=Path,
        default=DEFAULT_COMPANY_DIR,
        help=f"Directory containing company HTML and manifest.json. Default: {DEFAULT_COMPANY_DIR}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for analysis CSV and JSON. Defaults to <company-dir>/analysis.",
    )
    return parser.parse_args()


def clean_text(node: Tag | str | None) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return " ".join(node.split())
    return " ".join(node.get_text(" ", strip=True).split())


def parse_number(value: Tag | str | None) -> Optional[float]:
    text = clean_text(value).replace(",", "")
    if not text or text in {"-", "%", "NA", "N/A"}:
        return None

    match = NUMBER_PATTERN.search(text)
    return float(match.group()) if match else None


def status(value: Optional[bool]) -> str:
    if value is None:
        return "missing"
    return "pass" if value else "fail"


def all_present(*values: Optional[float]) -> bool:
    return all(value is not None and math.isfinite(value) for value in values)


def quick_ratios(soup: BeautifulSoup) -> dict[str, Optional[float]]:
    ratios: dict[str, Optional[float]] = {}
    for item in soup.select("li"):
        name = item.select_one("span.name")
        value = item.select_one("span.value span.number")
        if name and value:
            ratios[clean_text(name)] = parse_number(value)
    return ratios


def ranges_table(soup: BeautifulSoup, heading: str) -> dict[str, Optional[float]]:
    for table in soup.select("table.ranges-table"):
        title = table.select_one("th")
        if clean_text(title) != heading:
            continue

        values: dict[str, Optional[float]] = {}
        for row in table.select("tr"):
            cells = row.find_all("td")
            if len(cells) == 2:
                values[clean_text(cells[0]).rstrip(":")] = parse_number(cells[1])
        return values
    return {}


def sales_history(soup: BeautifulSoup) -> list[tuple[str, float]]:
    section = soup.select_one("section#profit-loss")
    if not section:
        return []

    table = section.select_one("table.data-table")
    if not table:
        return []

    headers = [clean_text(header) for header in table.select("thead th[data-date-key]")]
    sales_row = next(
        (
            row
            for row in table.select("tbody tr")
            if clean_text(row.select_one("td.text")).startswith("Sales")
        ),
        None,
    )
    if not sales_row:
        return []

    values = [parse_number(cell) for cell in sales_row.find_all("td")[1:]]
    return [
        (period, value)
        for period, value in zip(headers, values)
        if value is not None
    ]


def sales_growth_rule(soup: BeautifulSoup) -> dict[str, Any]:
    history = sales_history(soup)
    # Eleven annual values yield at most ten YoY comparisons; newer companies are
    # judged against 70% of however many valid comparisons are available.
    recent_history = history[-11:]
    growth_rates: list[float] = []
    for (_, previous), (_, current) in zip(recent_history, recent_history[1:]):
        if previous > 0:
            growth_rates.append((current / previous - 1) * 100)

    if not growth_rates:
        return {
            "status": "missing",
            "observations": 0,
            "over_10_count": 0,
            "over_10_percentage": None,
            "rates": growth_rates,
        }

    over_10_count = sum(rate >= 10 for rate in growth_rates)
    over_10_percentage = over_10_count / len(growth_rates) * 100
    return {
        "status": status(over_10_percentage >= 70),
        "observations": len(growth_rates),
        "over_10_count": over_10_count,
        "over_10_percentage": over_10_percentage,
        "rates": growth_rates,
    }


def promoter_holding_rule(soup: BeautifulSoup) -> dict[str, Any]:
    yearly = soup.select_one("#yearly-shp")
    if not yearly:
        return {"status": "missing"}

    table = yearly.select_one("table.data-table")
    if not table:
        return {"status": "missing"}

    periods = [clean_text(header) for header in table.select("thead th")[1:]]
    promoter_row = next(
        (
            row
            for row in table.select("tbody tr")
            if clean_text(row.select_one("td.text")).startswith("Promoters")
        ),
        None,
    )
    if not promoter_row:
        return {"status": "missing"}

    values = [parse_number(cell) for cell in promoter_row.find_all("td")[1:]]
    points = [(period, value) for period, value in zip(periods, values) if value is not None]
    if len(points) < 2:
        return {"status": "missing", "observations": len(points)}

    first_period, first_value = points[0]
    last_period, last_value = points[-1]
    decrease = first_value - last_value
    return {
        "status": status(decrease < 5),
        "first_period": first_period,
        "first_value": first_value,
        "last_period": last_period,
        "last_value": last_value,
        "decrease": decrease,
    }


def company_name(soup: BeautifulSoup, html_path: Path) -> str:
    heading = soup.select_one("h1.h2") or soup.select_one("h1")
    return clean_text(heading) or html_path.stem


def market_classification(soup: BeautifulSoup) -> dict[str, Any]:
    # Screener's market hierarchy is deterministic and avoids an AI call for the
    # financial-company exclusion policy.
    categories: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for anchor in soup.select('a[href^="/market/"][title]'):
        level = clean_text(anchor.get("title"))
        value = clean_text(anchor)
        key = (level, value)
        if level and value and key not in seen:
            categories.append({"level": level, "value": value})
            seen.add(key)

    financial_matches = [
        f"{category['level']}: {category['value']}"
        for category in categories
        if any(marker in category["value"].lower() for marker in FINANCIAL_MARKERS)
    ]
    return {
        "categories": categories,
        "excluded_financial": bool(financial_matches),
        "matches": financial_matches,
    }


def analyze_company(html_path: Path, company_url: str | None) -> dict[str, Any]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    ratios = quick_ratios(soup)
    market = market_classification(soup)

    stock_pe = ratios.get("Stock P/E")
    industry_pe = ratios.get("Industry PE")
    historical_pe = {period: ratios.get(f"{period}Yrs PE") for period in (3, 5, 7)}
    historical_pe_passes = sum(
        stock_pe <= 1.1 * value
        for value in historical_pe.values()
        if all_present(stock_pe, value)
    )
    historical_pe_available = sum(value is not None for value in historical_pe.values())

    profit_growth = ranges_table(soup, "Compounded Profit Growth")
    stock_cagr = ranges_table(soup, "Stock Price CAGR")
    profit_periods = {"10 Years": None, "5 Years": None, "3 Years": None, "TTM": None}
    profit_periods.update({period: profit_growth.get(period) for period in profit_periods})
    profit_over_10 = sum(value > 10 for value in profit_periods.values() if value is not None)
    profit_available = sum(value is not None for value in profit_periods.values())

    # Screener calls the current-year stock-price field "1 Year", not "TTM".
    price_periods = {
        "10 Years": stock_cagr.get("10 Years"),
        "5 Years": stock_cagr.get("5 Years"),
        "3 Years": stock_cagr.get("3 Years"),
        "TTM": stock_cagr.get("1 Year"),
    }
    price_vs_profit_passes = sum(
        price_periods[period] < profit_periods[period]
        for period in profit_periods
        if all_present(price_periods[period], profit_periods[period])
    )
    price_vs_profit_available = sum(
        all_present(price_periods[period], profit_periods[period])
        for period in profit_periods
    )

    rule_statuses = {
        "pe_vs_industry": status(
            stock_pe <= 1.25 * industry_pe if all_present(stock_pe, industry_pe) else None
        ),
        "pe_vs_historical": status(
            historical_pe_passes >= 2 if historical_pe_available >= 2 else None
        ),
        "roce_over_10": status(ratios.get("ROCE") > 10 if ratios.get("ROCE") is not None else None),
        "roe_over_10": status(ratios.get("ROE") > 10 if ratios.get("ROE") is not None else None),
        "debt_to_equity_under_0_5": status(
            ratios.get("Debt to equity") < 0.5 if ratios.get("Debt to equity") is not None else None
        ),
        "dpr_yoy_positive": status(
            ratios.get("DPR YOY") > 0 if ratios.get("DPR YOY") is not None else None
        ),
        "pledged_zero": status(
            math.isclose(ratios.get("Pledged percentage"), 0.0, abs_tol=1e-9)
            if ratios.get("Pledged percentage") is not None
            else None
        ),
        "sales_yoy_growth": sales_growth_rule(soup)["status"],
        "profit_growth_over_10": status(profit_over_10 >= 2 if profit_available else None),
        "stock_cagr_below_profit_growth": status(
            price_vs_profit_passes >= 2 if price_vs_profit_available else None
        ),
        "promoter_holding_decrease_under_5": promoter_holding_rule(soup)["status"],
    }

    sales = sales_growth_rule(soup)
    promoter = promoter_holding_rule(soup)
    all_rules_pass = all(rule == "pass" for rule in rule_statuses.values())
    return {
        "company_name": company_name(soup, html_path),
        "company_url": company_url,
        "html_file": html_path.name,
        "ratios": {
            "stock_pe": stock_pe,
            "industry_pe": industry_pe,
            "historical_pe": historical_pe,
            "roce": ratios.get("ROCE"),
            "roe": ratios.get("ROE"),
            "debt_to_equity": ratios.get("Debt to equity"),
            "dpr_yoy": ratios.get("DPR YOY"),
            "pledged_percentage": ratios.get("Pledged percentage"),
        },
        "profit_growth": profit_periods,
        "stock_price_cagr": price_periods,
        "comparison_counts": {
            "historical_pe_passes": historical_pe_passes,
            "historical_pe_available": historical_pe_available,
            "profit_growth_over_10": profit_over_10,
            "profit_growth_available": profit_available,
            "stock_cagr_below_profit_growth": price_vs_profit_passes,
            "stock_cagr_comparisons": price_vs_profit_available,
        },
        "sales_yoy": sales,
        "promoter_holding": promoter,
        "rules": rule_statuses,
        "market_classification": market,
        "all_rules_pass": all_rules_pass,
        "eligible_after_financial_exclusion": all_rules_pass and not market["excluded_financial"],
    }


def flatten_result(result: dict[str, Any]) -> dict[str, Any]:
    ratios = result["ratios"]
    counts = result["comparison_counts"]
    sales = result["sales_yoy"]
    promoter = result["promoter_holding"]
    market = result["market_classification"]
    row: dict[str, Any] = {
        "company_name": result["company_name"],
        "company_url": result["company_url"],
        "html_file": result["html_file"],
        "all_rules_pass": result["all_rules_pass"],
        "excluded_financial": market["excluded_financial"],
        "financial_exclusion_matches": "; ".join(market["matches"]),
        "market_categories": " > ".join(category["value"] for category in market["categories"]),
        "eligible_after_financial_exclusion": result["eligible_after_financial_exclusion"],
        **ratios,
        **{f"profit_growth_{key.replace(' ', '_').lower()}": value for key, value in result["profit_growth"].items()},
        **{f"stock_cagr_{key.replace(' ', '_').lower()}": value for key, value in result["stock_price_cagr"].items()},
        **counts,
        "sales_yoy_observations": sales.get("observations"),
        "sales_yoy_over_10_count": sales.get("over_10_count"),
        "sales_yoy_over_10_percentage": sales.get("over_10_percentage"),
        "promoter_first_period": promoter.get("first_period"),
        "promoter_first_value": promoter.get("first_value"),
        "promoter_last_period": promoter.get("last_period"),
        "promoter_last_value": promoter.get("last_value"),
        "promoter_decrease": promoter.get("decrease"),
    }
    row.update(result["rules"])
    return row


def main() -> int:
    args = parse_args()
    company_dir = args.company_dir.resolve()
    html_dir = company_dir / "html"
    manifest_path = company_dir / "manifest.json"
    output_dir = (args.output_dir or company_dir / "analysis").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not html_dir.is_dir() or not manifest_path.is_file():
        raise FileNotFoundError("Expected both html/ and manifest.json in --company-dir.")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    url_by_file = {
        Path(record["html_file"]).name: record["company_url"]
        for record in manifest
        if record.get("html_file") and record.get("company_url")
    }

    all_results = [
        analyze_company(html_path, url_by_file.get(html_path.name))
        for html_path in sorted(html_dir.glob("*.html"))
    ]
    results = [
        result
        for result in all_results
        if not result["market_classification"]["excluded_financial"]
    ]
    rows = [flatten_result(result) for result in results]

    json_path = output_dir / "company_rule_results.json"
    csv_path = output_dir / "company_rule_results.csv"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    fieldnames = list(rows[0]) if rows else []
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    fully_passing = sum(result["all_rules_pass"] for result in results)
    excluded_financial = len(all_results) - len(results)
    eligible = sum(result["eligible_after_financial_exclusion"] for result in results)
    print(f"Analyzed {len(all_results)} company profiles.")
    print(f"Excluded financial/bank/insurance companies: {excluded_financial}.")
    print(f"Included in reports: {len(results)}.")
    print(f"All 11 rules passed: {fully_passing}.")
    print(f"Eligible after exclusion: {eligible}.")
    print(f"Saved CSV: {csv_path}")
    print(f"Saved JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
