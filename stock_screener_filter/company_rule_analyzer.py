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


def calculate_fallback_debt_to_equity(soup: BeautifulSoup) -> float | None:
    bs = soup.find("section", id="balance-sheet")
    if not bs:
        return None
    data: dict[str, float] = {}
    for r in bs.select("tbody tr"):
        tds = r.find_all("td")
        if tds:
            title = clean_text(tds[0]).lower().rstrip(" +")
            val = parse_number(tds[-1])
            if val is not None:
                data[title] = val
    eq = data.get("equity capital", 0.0) + data.get("reserves", 0.0)
    bor = data.get("borrowings", 0.0)
    if eq > 0:
        return round(bor / eq, 2)
    return None


def calculate_fallback_pledged_percentage(soup: BeautifulSoup) -> float | None:
    sh = soup.find("section", id="shareholding")
    if not sh:
        return None
    for r in sh.select("tbody tr"):
        tds = r.find_all("td")
        if tds:
            title = clean_text(tds[0]).lower()
            if "pledge" in title:
                val = parse_number(tds[-1])
                if val is not None:
                    return val
    return 0.0


def quick_ratios(soup: BeautifulSoup) -> dict[str, float]:
    ratios: dict[str, float] = {}
    for item in soup.select("li, div.ratio-item, tr"):
        name = item.select_one("span.name, span.title, td.name, span.label")
        value = item.select_one("span.value span.number, span.number, td.number, span.value")
        if name and value:
            clean_name = clean_text(name)
            parsed_val = parse_number(value)
            if parsed_val is not None:
                ratios[clean_name] = parsed_val

    if "Debt to equity" not in ratios:
        de_fallback = calculate_fallback_debt_to_equity(soup)
        if de_fallback is not None:
            ratios["Debt to equity"] = de_fallback

    if "Pledged percentage" not in ratios:
        pledged_fallback = calculate_fallback_pledged_percentage(soup)
        if pledged_fallback is not None:
            ratios["Pledged percentage"] = pledged_fallback

    if "Market Cap" not in ratios:
        for k, v in ratios.items():
            if "market cap" in k.lower():
                ratios["Market Cap"] = v
                break

    if "Market Cap" not in ratios or ratios["Market Cap"] is None:
        for el in soup.find_all(text=re.compile(r"Market\s+Cap", re.I)):
            container = el.find_parent(["li", "div", "tr"]) or el.parent
            if container:
                numbers = re.findall(r"[\d,]+(?:\.\d+)?", container.get_text())
                if numbers:
                    val = parse_number(numbers[0])
                    if val is not None and val > 0:
                        ratios["Market Cap"] = val
                        break

    return ratios


def ranges_table(soup: BeautifulSoup, heading: str) -> dict[str, Optional[float]]:
    for table in soup.select("table.ranges-table"):
        title = table.select_one("th")
        if clean_text(title) != heading:
            continue

        ratios: dict[str, Optional[float]] = {}
        for row in table.select("tr"):
            cells = row.select("td")
            if len(cells) == 2:
                ratios[clean_text(cells[0]).rstrip(":")] = parse_number(cells[1])
        return ratios
    return {}


def sales_history(soup: BeautifulSoup) -> list[tuple[str, float]]:
    section = soup.find("section", id="profit-loss")
    if not section:
        return []

    headers_row = section.find("thead")
    if not headers_row:
        return []

    headers = [clean_text(th) for th in headers_row.find_all("th")[1:]]

    sales_row = None
    for tr in section.select("tbody tr"):
        title = clean_text(tr.find("td"))
        if title.lower().startswith("sales") or title.lower().rstrip(" +") == "sales":
            sales_row = tr
            break

    if not sales_row:
        return []

    values = [parse_number(cell) for cell in sales_row.find_all("td")[1:]]
    return [
        (period, value)
        for period, value in zip(headers, values)
        if value is not None
    ]


def sales_growth_rule(soup: BeautifulSoup, market_cap: float | None = None) -> dict[str, Any]:
    history = sales_history(soup)
    recent_history = history[-11:]
    growth_rates: list[float] = []
    for (_, previous), (_, current) in zip(recent_history, recent_history[1:]):
        if previous > 0:
            growth_rates.append((current / previous - 1) * 100)

    compounded_sales = ranges_table(soup, "Compounded Sales Growth")
    sales_3y = compounded_sales.get("3 Years")
    sales_5y = compounded_sales.get("5 Years")

    if market_cap is not None and market_cap > 50000:
        category = "Large-Cap"
        min_sales_cagr = 10.0
        min_yoy_growth = 10.0
    elif market_cap is not None and market_cap >= 10000:
        category = "Mid-Cap"
        min_sales_cagr = 12.0
        min_yoy_growth = 10.0
    else:
        category = "Small-Cap"
        min_sales_cagr = 15.0
        min_yoy_growth = 12.0

    min_hit_rate = 60.0

    if len(growth_rates) < 3:
        return {
            "status": "missing",
            "market_category": category,
            "min_sales_cagr": min_sales_cagr,
            "min_yoy_growth": min_yoy_growth,
            "min_hit_rate": min_hit_rate,
            "observations": len(growth_rates),
            "over_10_count": 0,
            "over_10_percentage": None,
            "sales_3y": sales_3y,
            "sales_5y": sales_5y,
            "rates": growth_rates,
        }

    over_floor_count = sum(rate >= min_yoy_growth for rate in growth_rates)
    over_floor_percentage = over_floor_count / len(growth_rates) * 100

    if sales_3y is not None and sales_5y is not None:
        compounded_pass = sales_3y >= min_sales_cagr and sales_5y >= min_sales_cagr
    elif sales_3y is not None:
        compounded_pass = sales_3y >= min_sales_cagr
    else:
        compounded_pass = False

    yoy_pass = over_floor_percentage >= min_hit_rate

    return {
        "status": status(compounded_pass and yoy_pass),
        "market_category": category,
        "min_sales_cagr": min_sales_cagr,
        "min_yoy_growth": min_yoy_growth,
        "min_hit_rate": min_hit_rate,
        "observations": len(growth_rates),
        "over_10_count": over_floor_count,
        "over_10_percentage": over_floor_percentage,
        "sales_3y": sales_3y,
        "sales_5y": sales_5y,
        "compounded_pass": compounded_pass,
        "yoy_pass": yoy_pass,
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


def calculate_fallback_historical_pe(soup: BeautifulSoup, cur_price: float | None) -> dict[int, float | None]:
    if not cur_price or cur_price <= 0:
        return {3: None, 5: None, 7: None}

    pnl = soup.find("section", id="profit-loss")
    if not pnl:
        return {3: None, 5: None, 7: None}

    eps_tr = next((tr for tr in pnl.select("tbody tr") if "eps" in clean_text(tr.find("td")).lower()), None)
    if not eps_tr:
        return {3: None, 5: None, 7: None}

    eps_vals = [parse_number(td) for td in eps_tr.find_all("td")[1:]]
    eps_vals = [v for v in eps_vals if v is not None and v > 0]
    if not eps_vals:
        return {3: None, 5: None, 7: None}

    pes = [round(cur_price / v, 1) for v in eps_vals]
    import statistics

    res: dict[int, float | None] = {}
    for period in (3, 5, 7):
        if len(pes) >= period:
            res[period] = round(float(statistics.median(pes[-period:])), 1)
        else:
            res[period] = None
    return res


def calculate_industry_pe_fallback(market: dict[str, Any], html_path: Path) -> float | None:
    csv_path = Path("data/current_run/companies/analysis/company_rule_results.csv")
    if not csv_path.is_file():
        csv_path = html_path.parent.parent / "analysis" / "company_rule_results.csv"
    if not csv_path.is_file():
        return None

    industry_names = [cat["value"].lower() for cat in market.get("categories", [])]
    if not industry_names:
        return None

    import csv
    import statistics

    matching_pes: list[float] = []
    try:
        with csv_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                cats = row.get("market_categories", "").lower()
                stock_pe = parse_number(row.get("stock_pe"))
                if stock_pe and stock_pe > 0:
                    if any(ind in cats for ind in industry_names):
                        matching_pes.append(stock_pe)
    except Exception:
        pass

    if matching_pes:
        return round(float(statistics.median(matching_pes)), 1)
    return None


def calculate_fallback_cfo_ebitda_pat(soup: BeautifulSoup) -> dict[str, Any]:
    """Calculate Rule 14 (CFO/EBITDA >= 65% and CFO/PAT >= 80%) from HTML Cash Flow and P&L tables."""
    cfo_values: list[float] = []
    cf_section = soup.find("section", id="cash-flow")
    if cf_section:
        cf_table = cf_section.find("table")
        if cf_table:
            for row in cf_table.find_all("tr"):
                row_text = row.get_text(" ", strip=True).lower()
                if "cash from operating activity" in row_text or "operating activity" in row_text:
                    cols = [td.get_text(strip=True).replace(",", "") for td in row.find_all(["td", "th"])[1:]]
                    for c in cols:
                        clean_c = c.replace("-", "").replace(".", "")
                        if clean_c.isdigit():
                            try:
                                cfo_values.append(float(c))
                            except ValueError:
                                pass
                    break

    ebitda_values: list[float] = []
    pat_values: list[float] = []
    pnl_section = soup.find("section", id="profit-loss")
    if pnl_section:
        pnl_table = pnl_section.find("table")
        if pnl_table:
            for row in pnl_table.find_all("tr"):
                row_text = row.get_text(" ", strip=True).lower()
                if "operating profit" in row_text and not ebitda_values:
                    cols = [td.get_text(strip=True).replace(",", "") for td in row.find_all(["td", "th"])[1:]]
                    for c in cols:
                        if c.replace("-", "").replace(".", "").isdigit():
                            try:
                                ebitda_values.append(float(c))
                            except ValueError:
                                pass
                elif "net profit" in row_text and not pat_values:
                    cols = [td.get_text(strip=True).replace(",", "") for td in row.find_all(["td", "th"])[1:]]
                    for c in cols:
                        if c.replace("-", "").replace(".", "").isdigit():
                            try:
                                pat_values.append(float(c))
                            except ValueError:
                                pass

    if not cfo_values or not ebitda_values or not pat_values:
        return {"rule_13_cfo_ebitda": "missing"}

    cfo_5y = sum(cfo_values[-5:])
    ebitda_5y = sum(ebitda_values[-5:])
    pat_5y = sum(pat_values[-5:])

    cfo_ebitda_ratio = (cfo_5y / ebitda_5y * 100.0) if ebitda_5y > 0 else 0.0
    cfo_pat_ratio = (cfo_5y / pat_5y * 100.0) if pat_5y > 0 else 0.0

    rule_pass = (cfo_ebitda_ratio >= 65.0) and (cfo_pat_ratio >= 80.0)
    return {
        "rule_13_cfo_ebitda": "pass" if rule_pass else "fail",
        "cum_cfo_5y": str(round(cfo_5y, 2)),
        "cum_ebitda_5y": str(round(ebitda_5y, 2)),
        "cum_pat_5y": str(round(pat_5y, 2)),
        "cfo_ebitda_cum_ratio": str(round(cfo_ebitda_ratio, 2)),
        "cfo_pat_cum_ratio": str(round(cfo_pat_ratio, 2)),
    }


def calculate_fallback_ssgr(soup: BeautifulSoup, sales_cagr_3y: float | None, roe: float | None) -> dict[str, Any]:
    """Calculate Rule 13 (SSGR > 10% and >= 3Y Sales CAGR) from HTML tables."""
    dpr_3y: list[float] = []
    pnl_section = soup.find("section", id="profit-loss")
    if pnl_section:
        pnl_table = pnl_section.find("table")
        if pnl_table:
            for row in pnl_table.find_all("tr"):
                row_text = row.get_text(" ", strip=True).lower()
                if "dividend payout" in row_text:
                    cols = [td.get_text(strip=True).replace("%", "").replace(",", "") for td in row.find_all(["td", "th"])[1:]]
                    nums = []
                    for c in cols:
                        if c.replace("-", "").replace(".", "").isdigit():
                            try:
                                nums.append(float(c))
                            except ValueError:
                                pass
                    dpr_3y = nums[-3:]
                    break

    if roe is None or not dpr_3y or len(dpr_3y) < 3:
        return {"rule_12_ssgr": "missing"}

    avg_dpr = sum(dpr_3y) / len(dpr_3y) / 100.0
    ssgr_3y_avg = roe * (1.0 - avg_dpr)

    pass_rule = (ssgr_3y_avg > 10.0) and (sales_cagr_3y is not None and ssgr_3y_avg >= sales_cagr_3y)
    return {
        "rule_12_ssgr": "pass" if pass_rule else "fail",
        "ssgr_3y_avg": str(round(ssgr_3y_avg, 2)),
        "excel_sales_cagr_3y": str(round(sales_cagr_3y, 2)) if sales_cagr_3y is not None else "",
    }


def analyze_company(html_path: Path, company_url: str | None) -> dict[str, Any]:
    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    ratios = quick_ratios(soup)
    market = market_classification(soup)

    stock_pe = ratios.get("Stock P/E")
    industry_pe = ratios.get("Industry PE")
    if industry_pe is None:
        industry_pe = calculate_industry_pe_fallback(market, html_path)

    historical_pe = {period: ratios.get(f"{period}Yrs PE") for period in (3, 5, 7)}
    if all(v is None for v in historical_pe.values()):
        cur_price = ratios.get("Current Price")
        fallback_pe = calculate_fallback_historical_pe(soup, cur_price)
        historical_pe.update({k: v for k, v in fallback_pe.items() if v is not None})

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

    profit_3y = profit_periods.get("3 Years")
    profit_5y = profit_periods.get("5 Years")
    peg_ratio: float | None = None
    effective_cagr: float | None = None
    peg_pass: bool | None = None

    if all_present(stock_pe, profit_3y, profit_5y):
        if profit_3y > 0 and profit_5y > 0:
            effective_cagr = (profit_3y + profit_5y) / 2.0
            if effective_cagr > 0:
                peg_ratio = stock_pe / effective_cagr
            peg_pass = (
                profit_3y >= 0.75 * profit_5y
                and peg_ratio is not None
                and peg_ratio <= 1.5
            )
        else:
            peg_pass = False

    compounded_sales = ranges_table(soup, "Compounded Sales Growth")
    sales_3y = compounded_sales.get("3 Years")
    sales_5y = compounded_sales.get("5 Years")

    rev_quality_pass: bool | None = None
    pass_3y = (profit_3y >= sales_3y) if all_present(profit_3y, sales_3y) else None
    pass_5y = (profit_5y >= sales_5y) if all_present(profit_5y, sales_5y) else None
    if pass_3y is True or pass_5y is True:
        rev_quality_pass = True
    elif pass_3y is False and pass_5y is False:
        rev_quality_pass = False
    elif pass_3y is not None:
        rev_quality_pass = pass_3y
    elif pass_5y is not None:
        rev_quality_pass = pass_5y

    market_cap = ratios.get("Market Cap")
    sales = sales_growth_rule(soup, market_cap)
    promoter = promoter_holding_rule(soup)

    rule_statuses = {
        "pe_vs_industry": status(
            stock_pe <= 1.1 * industry_pe if all_present(stock_pe, industry_pe) else None
        ),
        "pe_vs_historical": status(
            historical_pe_passes >= 2 if historical_pe_available >= 2 else None
        ),
        "roce_over_15": status(ratios.get("ROCE") > 15 if ratios.get("ROCE") is not None else None),
        "roe_over_15": status(ratios.get("ROE") > 15 if ratios.get("ROE") is not None else None),
        "debt_to_equity_under_0_5": status(
            ratios.get("Debt to equity") < 0.5 if ratios.get("Debt to equity") is not None else None
        ),
        "pledged_zero": status(
            math.isclose(ratios.get("Pledged percentage"), 0.0, abs_tol=1e-9)
            if ratios.get("Pledged percentage") is not None
            else None
        ),
        "sales_yoy_growth": sales["status"],
        "profit_growth_over_10": status(profit_over_10 >= 2 if profit_available else None),
        "stock_cagr_below_profit_growth": status(
            price_vs_profit_passes >= 2 if price_vs_profit_available else None
        ),
        "promoter_holding_decrease_under_5": promoter["status"],
        "peg_ratio_under_1_5": status(peg_pass),
        "revenue_quality_guard": status(rev_quality_pass),
    }

    # HTML fallbacks for Rule 13 (SSGR) & Rule 14 (CFO/EBITDA)
    cfo_fallback = calculate_fallback_cfo_ebitda_pat(soup)
    ssgr_fallback = calculate_fallback_ssgr(soup, sales_3y, ratios.get("ROE"))
    rule_statuses.update({
        "rule_12_ssgr": ssgr_fallback.get("rule_12_ssgr", "missing"),
        "rule_13_cfo_ebitda": cfo_fallback.get("rule_13_cfo_ebitda", "missing"),
    })

    all_rules_pass = all(rule == "pass" for rule in rule_statuses.values())
    return {
        "company_name": company_name(soup, html_path),
        "company_url": company_url,
        "html_file": html_path.name,
        "ratios": {
            "market_cap": ratios.get("Market Cap"),
            "stock_pe": stock_pe,
            "industry_pe": industry_pe,
            "historical_pe": historical_pe,
            "roce": ratios.get("ROCE"),
            "roe": ratios.get("ROE"),
            "debt_to_equity": ratios.get("Debt to equity"),
            "dpr_yoy": ratios.get("DPR YOY"),
            "pledged_percentage": ratios.get("Pledged percentage"),
            "peg_ratio": peg_ratio,
            "effective_cagr": effective_cagr,
            **{k: v for k, v in cfo_fallback.items() if k != "rule_13_cfo_ebitda"},
            **{k: v for k, v in ssgr_fallback.items() if k != "rule_12_ssgr"},
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
        "sales_market_category": sales.get("market_category"),
        "sales_min_cagr": sales.get("min_sales_cagr"),
        "sales_min_yoy_growth": sales.get("min_yoy_growth"),
        "sales_yoy_observations": sales.get("observations"),
        "sales_yoy_over_10_count": sales.get("over_10_count"),
        "sales_yoy_over_10_percentage": sales.get("over_10_percentage"),
        "sales_growth_3_years": sales.get("sales_3y"),
        "sales_growth_5_years": sales.get("sales_5y"),
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
    print(f"All 12 HTML rules passed: {fully_passing}.")
    print(f"Eligible after exclusion: {eligible}.")
    print(f"Saved CSV: {csv_path}")
    print(f"Saved JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
