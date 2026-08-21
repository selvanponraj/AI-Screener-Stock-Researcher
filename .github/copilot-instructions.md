# GitHub Copilot / Pilot Instructions

> **Single Source of Truth**: Refer to [`CLAUDE.md`](file:///Users/suchi.thamarai/AI/2026/AI-Screener-Stock-Researcher/.worktree/lite-llm/CLAUDE.md) for complete project architecture, 14 quantitative rules, market-cap tiering, and HTML parsing contracts.

## Token Optimization & Performance Best Practices

1. **Local Editing**: Do not re-run heavy background crawlers or full batch pipelines when performing code edits unless explicitly prompted by the user.
2. **LLM Provider**: Maintain LiteLLM proxy integration (`LITELLM_BASE_URL`, `LITELLM_API_KEY`, `LITELLM_MODEL`). Do not import or instantiate Google Gemini SDK modules directly.
3. **Data Inspection**:
   - Primary data location: `data/current_run/companies/`.
   - HTML profiles: `data/current_run/companies/html/*.html`.
   - Excel exports: `data/current_run/companies/excel/*.xlsx`.
   - Quantitative Analysis Results: `data/current_run/companies/analysis/company_rule_results.csv`.

---

## Code Base Map

- `run_app.py` & `stock_screener_filter/app_server.py`: Single-page web dashboard server running on HTTP port 8765.
- `stock_screener_filter/company_rule_analyzer.py`: Stage 1 rule evaluator (12 HTML rules).
- `stock_screener_filter/company_excel_rule_analyzer.py`: Stage 2 rule evaluator (2 Excel rules).
- `stock_screener_filter/rules_pipeline.py`: Pipeline coordinator, CSV/JSON rule results combiner, and threshold manager.
- `stock_screener_filter/ai_evaluator.py`: LiteLLM qualitative document & financial report evaluator.
