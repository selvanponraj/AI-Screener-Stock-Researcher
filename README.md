# AI Stock Researcher

Small automation steps for research workflows on Screener.

## Screener Login

Install dependencies:

```powershell
python -m pip install -r requirements.txt
```

Run the login helper:

```powershell
$env:SCREENER_EMAIL = "your-email@example.com"
$env:SCREENER_PASSWORD = "your-password"
python -m stock_screener_filter.screener_login --url https://www.screener.in/
```

The first run opens a real Microsoft Edge browser window and logs in using
Screener's email/password form. The browser session is saved under
`.browser/screener-profile-edge` and reused on future runs. Do not commit real
credentials into this project.

If you prefer Chrome:

```powershell
python -m stock_screener_filter.screener_login --browser-channel chrome
```

## Screener Setup

The HTML rule scraper depends on Screener's company profile quick-ratio section.
Configure your Screener company profile so the quick-ratio card matches this
format before scraping:

![Required Screener company profile quick-ratio format](<docs/images/Screenshot 2026-07-24 053742.png>)

At minimum, these ratios must be visible with these names:

```text
Stock P/E
Industry PE
3Yrs PE
5Yrs PE
7Yrs PE
ROCE
ROE
Debt to equity
DPR YOY
Pledged percentage
Promoter holding
```

The project also reads sales, profit growth, stock price CAGR, and promoter
holding from the standard Screener company profile sections. It is designed for
non-financial operating companies and should not be used for banks, NBFCs,
financial services firms, insurers, or similar financial companies. Those
companies have different balance-sheet and cash-flow economics, so the rules are
not comparable.

Add this custom ratio in Screener before running the pipeline:

```text
Ratio name: DPR YOY
Short name: DPR YOY
Ratio unit: Percentage
Formula: ((Depreciation -Depreciation last year)/Depreciation last year)*100
Description: Depreciation -Depreciation last year
```

The current default screens use these Screener queries:

```text
Market cap to profit <10
AND
Market cap to profit >0
AND
Market Capitalization >5000
```

```text
Return over 5years <Profit growth 5Years
AND
Market Capitalization >5000
```

```text
DPR YOY >0
AND

Market Capitalization >5000

AND
Depreciation >100
```

You can use your own Screener screens. Put their links in `.env` as a
comma-separated list:

```text
SCREENER_SCREEN_URLS=https://www.screener.in/screens/your-first-screen/,https://www.screener.in/screens/your-second-screen/
```

You can also pass screen links for one command-line run:

```powershell
python -m stock_screener_filter.screen_page_crawler --screens https://www.screener.in/screens/your-first-screen/ https://www.screener.in/screens/your-second-screen/
```

## Screen HTML Download

After signing in, download every paginated page of the configured screens:

```powershell
python -m stock_screener_filter.screen_page_crawler
```

Each run saves full rendered HTML and a `manifest.json` under
`data/runs/<timestamp>/screens/`.

## Company Profile HTML Download

Download the full profile HTML for every unique company linked by the latest
screen run. Requests are made one at a time with a 1.5-second pause:

```powershell
python -m stock_screener_filter.company_profile_crawler
```

Use `--dry-run` to report the number of unique company profiles before making
any requests.

If a run is interrupted, resume it without re-fetching profiles that were
already saved:

```powershell
python -m stock_screener_filter.company_profile_crawler --output-dir data/runs/20260624_company_profiles/companies --resume
```

The profile crawler waits for the configured Screener quick-ratio labels before
saving each document. To test one profile without reading a screen page:

```powershell
python -m stock_screener_filter.company_profile_crawler --company-urls https://www.screener.in/company/ALEMBICLTD/consolidated/
```

## Rule Analysis

Evaluate the downloaded company profiles against the screening rules without
making any additional web requests:

```powershell
python -m stock_screener_filter.company_rule_analyzer
```

The analysis CSV and JSON are written under
`data/runs/20260624_company_profiles/companies/analysis/`.

Companies classified by Screener's local market taxonomy as financial services,
banks, NBFCs, or insurers are excluded from the final eligibility field.

## Excel Rules

Download Screener's Excel export for non-financial companies with at least six
prior rule passes, then evaluate SSGR and five-year CFO/EBITDA:

```powershell
python -m stock_screener_filter.company_excel_crawler
python -m stock_screener_filter.company_excel_rule_analyzer
```

## Local App UI

Start the browser UI:

```powershell
python -m stock_screener_filter.app_server
```

Open `http://127.0.0.1:8765`.

The **Run Screener Pipeline** button cleans and recreates `data/current_run/`,
then runs screen crawling, profile crawling, HTML rules, Excel downloads, Excel
rules, and the final >=65% rule filter. Older timestamped folders under
`data/runs/` are left alone.

For AI/RAG evaluation, create a local `.env` file from `.env.example`:

```powershell
Copy-Item .env.example .env
notepad .env
```

`.env` is gitignored. The UI reads Gemini credentials only from `.env` or the
server environment. Uploaded documents are deduplicated by SHA-256 per stock under
`data/document_store/`.

The RAG layer uses LangChain's recursive text splitter, local
`sentence-transformers` embeddings, and ChromaDB. By default the embedding model
is fixed to `BAAI/bge-m3`, downloaded into `data/model_cache/` on first use.
Extracted chunks are stored in ChromaDB under `data/chroma_db/`, with a separate
collection per stock. AI evaluation is orchestrated as a question-by-question
LangGraph workflow:

```text
future outlook -> initiatives -> promise delivery -> exaggeration risk
-> news sentiment -> industry outlook -> final scoring
```

The first four questions retrieve focused chunks from Chroma. The news and
industry questions fetch DuckDuckGo snippets with `ddgs`, cache those search
results under `data/search_cache/`, and pass the snippets/URLs to Gemini as
source context. The final node combines the sub-answers into the 50-point AI
score.

AI results are saved under `data/ai_evaluations/`.
