# Stock Researcher & Quantitative Screener Filter

## Project Architecture Overview

This project evaluates Screener.in stock exports against a **14-rule quantitative filter** (12 Stage-1 HTML rules + 2 Stage-2 Excel rules) and provides an AI stock research dashboard powered strictly by **LiteLLM**.

---

## Standardization & Pipeline Conventions

1. **Stock ID & Filename Conventions**:
   - `stock_id` is strictly standardized to clean uppercase ticker symbols (`INDIAMART`, `ACE`, `TCS`, `RELIANCE`, `NATCOPHARM`).
   - HTML files are stored as `{TICKER}.html` and Excel exports as `{TICKER}.xlsx`. Hash suffixes (`_a830221c4b`) are stripped and disallowed.

2. **Consolidated Screener URLs**:
   - All Screener company URLs generated, fetched, or stored across pipeline crawlers, helpers, and datasets strictly follow the consolidated format:
     `https://www.screener.in/company/{SYMBOL}/consolidated/`

3. **Target Scope Isolation**:
   - `current_run` stock refetches write **ONLY** to `data/current_run/companies/` (`html/`, `excel/`, `analysis/`, `excel/analysis/`) and `data/current_run/final/`.
   - `custom_stocks` write **ONLY** to `data/custom_stocks/`.

4. **Complete Output Synchronization on Refetch**:
   - Single-stock refetches for `current_run` automatically synchronize all 4 output subdirectories:
     - HTML files (`data/current_run/companies/html/{TICKER}.html`)
     - Excel files (`data/current_run/companies/excel/{TICKER}.xlsx`)
     - Excel rule analysis (`data/current_run/companies/excel/analysis/excel_rule_results.csv` and `.json`)
     - Final merged output (`data/current_run/final/rule_filtered_stocks.csv` and `.json`)

5. **Document Store & RAG AI Evaluation**:
   - Document uploads (concalls, annual reports) are saved under `data/document_store/raw/{TICKER}/`, chunked, embedded via `BAAI/bge-m3`, and indexed in `data/document_store/documents.json`.
   - AI evaluations run across all uploaded documents per stock and save results under `data/ai_evaluations/{TICKER}.json`.

---

## Market-Cap Tier Thresholds

Market-Cap tiers dynamically set Sales CAGR and YoY Growth thresholds for Rule 7:

| Market-Cap Tier | Market Cap Range (₹ Cr) | Sales CAGR Threshold (Rule 7) | YoY Growth Floor (Rule 7) | YoY Hit Rate Required |
| :--- | :--- | :--- | :--- | :--- |
| **Large-Cap** | $> \text{₹}50,000\text{ Cr}$ | $\ge 10\%$ | $\ge 10\%$ | $\ge 60\%$ of observations |
| **Mid-Cap** | $\text{₹}10,000\text{ Cr} - \text{₹}50,000\text{ Cr}$ | $\ge 12\%$ | $\ge 10\%$ | $\ge 60\%$ of observations |
| **Small-Cap** | $< \text{₹}10,000\text{ Cr}$ | $\ge 15\%$ | $\ge 12\%$ | $\ge 60\%$ of observations |

---

## 14 Quantitative Rules Summary

### Stage 1: HTML Rules (12 Rules)
1. `pe_vs_industry`: Stock P/E $\le 1.1 \times$ Industry P/E.
2. `pe_vs_historical`: Stock P/E $\le 1.1 \times$ historical P/E for $\ge 2$ of 3 available periods (3Y, 5Y, 7Y).
3. `roce_over_15`: ROCE $> 15\%$.
4. `roe_over_15`: ROE $> 15\%$.
5. `debt_to_equity_under_0_5`: Debt to Equity $< 0.5$.
6. `pledged_zero`: Pledged Shares Percentage $= 0\%$.
7. `sales_yoy_growth`: Tiered Market-Cap Sales CAGR & YoY floor with $\ge 60\%$ hit rate over 10 YoY observations.
8. `profit_growth_over_10`: Profit growth $> 10\%$ for $\ge 2$ of 4 available periods (10Y, 5Y, 3Y, TTM).
9. `stock_cagr_below_profit_growth`: Stock Price CAGR $<$ Profit Growth for $\ge 2$ matching periods.
10. `promoter_holding_decrease_under_5`: Promoter holding decrease $< 5\%$.
11. `peg_ratio_under_1_5`: Stock P/E / Effective Profit CAGR (avg 3Y & 5Y) $\le 1.5$.
12. `revenue_quality_guard`: Operating Leverage confirmed: Profit CAGR $\ge$ Sales CAGR for 3Y **OR** 5Y.

### Stage 2: Excel Rules (2 Rules)
13. `rule_12_ssgr`: 3Y Avg SSGR $> 10\%$ and $\ge$ 3Y Sales CAGR.
14. `rule_13_cfo_ebitda`: 5Y Cumulative CFO / EBITDA $\ge 65\%$ and CFO / PAT $\ge 80\%$.

---

## Key Environments & Commands

- **Python Interpreter**: Use virtualenv at `~/AI/2026/AI-Screener-Stock-Researcher/.venv/bin/python`.
- **Run Dashboard App**:
  ```bash
  python run_app.py
  ```
  Dashboard UI runs on `http://127.0.0.1:8765`.

- **LiteLLM Configuration**: Read strictly from `.env` (`LITELLM_BASE_URL`, `LITELLM_API_KEY`, `LITELLM_MODEL`). Do **not** use Gemini SDK directly.

---

## Important HTML Parsing Gotchas

1. **`ranges-table` colons**: Table keys in Screener HTML `ranges-table` include trailing colons (e.g. `"3 Years:"`). Always use `clean_text(cells[0]).rstrip(":")` to ensure keys match `"3 Years"`.
2. **Sales Row matching**: Screener P&L tables render row headers with expand buttons (e.g. `"Sales +"`). Always match row title using `title.lower().startswith("sales")`.
