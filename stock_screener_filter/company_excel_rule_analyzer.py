"""Evaluate SSGR and CFO/EBITDA rules from Screener Excel exports."""

from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
from statistics import mean
from typing import Any, Optional

from openpyxl import load_workbook


DEFAULT_COMPANY_DIR = Path("data") / "runs" / "20260625_updated_company_profiles" / "companies"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Evaluate SSGR and CFO/EBITDA rules from downloaded Screener Excel files."
    )
    parser.add_argument(
        "--excel-dir",
        type=Path,
        default=DEFAULT_COMPANY_DIR / "excel",
        help="Directory containing the downloaded Excel files and manifest.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Directory for Excel-rule CSV and JSON. Defaults to <excel-dir>/analysis.",
    )
    return parser.parse_args()


def number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def data_row(sheet: Any, row: int) -> list[Optional[float]]:
    return [number(sheet.cell(row, column).value) for column in range(2, 12)]


def average(values: list[Optional[float]]) -> Optional[float]:
    return mean(values) if all(value is not None for value in values) else None


def safe_ratio(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def zero_blank(value: Optional[float]) -> float:
    return 0.0 if value is None else value


def operating_profit(data_sheet: Any) -> list[Optional[float]]:
    sales = data_row(data_sheet, 17)
    raw_material = data_row(data_sheet, 18)
    inventory_change = data_row(data_sheet, 19)
    power_fuel = data_row(data_sheet, 20)
    manufacturing = data_row(data_sheet, 21)
    employees = data_row(data_sheet, 22)
    selling = data_row(data_sheet, 23)
    other_expenses = data_row(data_sheet, 24)

    values: list[Optional[float]] = []
    for index in range(len(sales)):
        if sales[index] is None:
            values.append(None)
            continue
        # Matches Profit & Loss!5: Excel SUM ignores blank expense cells.
        expenses = (
            zero_blank(raw_material[index])
            - zero_blank(inventory_change[index])
            + zero_blank(power_fuel[index])
            + zero_blank(manufacturing[index])
            + zero_blank(employees[index])
            + zero_blank(selling[index])
            + zero_blank(other_expenses[index])
        )
        values.append(sales[index] - expenses)
    return values


def trailing_operating_profit(data_sheet: Any) -> Optional[float]:
    quarter_operating_profit = [
        number(data_sheet.cell(50, column).value) for column in range(8, 12)
    ]
    return sum(quarter_operating_profit) if all(value is not None for value in quarter_operating_profit) else None


def ssgr_values(data_sheet: Any) -> list[Optional[float]]:
    sales = data_row(data_sheet, 17)
    net_profit = data_row(data_sheet, 30)
    dividend = data_row(data_sheet, 31)
    depreciation = data_row(data_sheet, 26)
    net_block = data_row(data_sheet, 62)

    # Profit & Loss!31 stores NPM as a percentage, not a decimal fraction.
    npm = [
        safe_ratio(profit, sale) * 100
        if safe_ratio(profit, sale) is not None
        else None
        for profit, sale in zip(net_profit, sales)
    ]
    # Profit & Loss!18 is IF(net_profit > 0, dividend / net_profit, 0).
    # Excel treats blank dividend cells as zero in the division.
    dpr = [
        0.0
        if profit is None or profit <= 0
        else safe_ratio(zero_blank(dividend_value), profit)
        for dividend_value, profit in zip(dividend, net_profit)
    ]
    depreciation_rate = [
        safe_ratio(depreciation_value, block) * 100
        if safe_ratio(depreciation_value, block) is not None
        else None
        for depreciation_value, block in zip(depreciation, net_block)
    ]
    nfat = [
        safe_ratio(sales[index], average([net_block[index], net_block[index - 1]]))
        if index > 0
        else None
        for index in range(len(sales))
    ]

    values: list[Optional[float]] = []
    # Excel cells E27:K27 correspond to annual-data indexes 3 through 9.
    for index in range(3, 10):
        npm_average = average(npm[index - 2 : index + 1])
        dpr_average = average(dpr[index - 2 : index + 1])
        nfat_average = average(nfat[index - 2 : index + 1])
        depreciation_average = average(depreciation_rate[index - 2 : index + 1])
        if any(value is None for value in (npm_average, dpr_average, nfat_average, depreciation_average)):
            values.append(None)
            continue
        values.append(nfat_average * npm_average * (1 - dpr_average) - depreciation_average)
    return values


def analyze_workbook(path: Path) -> dict[str, Any]:
    # openpyxl does not calculate formulas. Read the source Data Sheet and recreate
    # the template formulas so results do not depend on cached Excel values.
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        data_sheet = workbook["Data Sheet"]
        ssgr = ssgr_values(data_sheet)
        ssgr_complete = all(value is not None for value in ssgr)
        ssgr_positive = ssgr_complete and all(value > 0 for value in ssgr if value is not None)
        ssgr_over_10_count = sum(value >= 10 for value in ssgr if value is not None)
        ssgr_minimum = math.ceil(len(ssgr) / 2)
        ssgr_rule = (
            "missing"
            if not ssgr_complete
            else "pass"
            if ssgr_positive and ssgr_over_10_count >= ssgr_minimum
            else "fail"
        )

        cfo = data_row(data_sheet, 82)
        operating_profit_values = operating_profit(data_sheet)
        ebitda = operating_profit_values[1:] + [trailing_operating_profit(data_sheet)]
        cfo_ebitda_values = [
            safe_ratio(cfo_value, ebitda_value) * 100
            if safe_ratio(cfo_value, ebitda_value) is not None
            else None
            for cfo_value, ebitda_value in zip(cfo, ebitda)
        ]
        latest_five = cfo_ebitda_values[-5:]
        cfo_ebitda_average = average(latest_five)
        cfo_rule = "missing" if cfo_ebitda_average is None else "pass" if cfo_ebitda_average > 50 else "fail"

        return {
            "ssgr_values_e27_k27": ssgr,
            "ssgr_all_years_positive": ssgr_positive if ssgr_complete else None,
            "ssgr_years_at_or_above_10": ssgr_over_10_count,
            "ssgr_minimum_years_at_or_above_10": ssgr_minimum,
            "rule_12_ssgr": ssgr_rule,
            "cfo_ebitda_last_five_values": latest_five,
            "cfo_ebitda_last_five_average": cfo_ebitda_average,
            "rule_13_cfo_ebitda": cfo_rule,
        }
    finally:
        workbook.close()


def main() -> int:
    args = parse_args()
    excel_dir = args.excel_dir.resolve()
    manifest_path = excel_dir / "manifest.json"
    output_dir = (args.output_dir or excel_dir / "analysis").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Excel manifest not found: {manifest_path}")

    downloads = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    for download in downloads:
        filename = download.get("download_file")
        if not filename or not (excel_dir / filename).is_file():
            continue
        result = {
            "company_name": download["company_name"],
            "company_url": download["company_url"],
            "prior_rule_pass_count": int(download["prior_rule_pass_count"]),
            "excel_file": filename,
            **analyze_workbook(excel_dir / filename),
        }
        results.append(result)

    json_path = output_dir / "excel_rule_results.json"
    csv_path = output_dir / "excel_rule_results.csv"
    json_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    fieldnames = list(results[0]) if results else []
    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(results)

    print(f"Analyzed {len(results)} Excel exports.")
    print(f"SSGR passes: {sum(result['rule_12_ssgr'] == 'pass' for result in results)}.")
    print(f"CFO/EBITDA passes: {sum(result['rule_13_cfo_ebitda'] == 'pass' for result in results)}.")
    print(f"Saved CSV: {csv_path}")
    print(f"Saved JSON: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
