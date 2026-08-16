# Single Stock Ticker Analysis Feature Design

## Overview
Add the capability to perform end-to-end quantitative rule analysis (HTML + Excel rules) on a single specified stock ticker (e.g. `TCS`, `INFY`) directly from the Web UI or CLI.

Existing batch scanner outputs in `data/current_run/` remain completely untouched. Single-stock analysis outputs are stored in `data/custom_stocks/` and automatically displayed alongside batch results in the Web UI.

## Scope & User Workflow
1. User enters a ticker name (e.g., `TCS` or `https://www.screener.in/company/TCS/`) into the UI toolbar.
2. Clicking **Analyze Ticker** triggers a background runner.
3. The runner:
   - Derives the company URL: `https://www.screener.in/company/<TICKER>/`.
   - Downloads profile HTML using Playwright (`company_profile_crawler.py`).
   - Analyzes 11 HTML rules (`company_rule_analyzer.py`).
   - Downloads the Excel export (`company_excel_crawler.py`).
   - Runs 2 Excel rules (`company_excel_rule_analyzer.py`).
   - Merges results into `data/custom_stocks/<stock_id>.json`.
4. The Web UI updates the table, displaying the ticker with a `Single stock` badge, full 13-rule breakdown, document upload, and AI evaluation features.

## Data Isolation & Architecture
- **Batch Screener Data**: `data/current_run/` (Unchanged)
- **Single-Stock Data**: `data/custom_stocks/` (Persisted individually per ticker)
- **Document Store & AI Evaluations**: Uses existing `stock_id` keying (`<name>-<hash>`) to enable seamlessly uploading reports/concalls and running LangGraph RAG AI evaluations.

## Testing & Verification
- Unit test single-stock analysis execution in `rules_pipeline.py`.
- Test UI trigger via `/api/analyze-ticker`.
- Verify UI stock list rendering with both batch and single-stock entries.
