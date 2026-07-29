# AI Stock Researcher

AI Stock Researcher is a local research assistant for automating the stock
screening workflow I used to do manually for investments. The goal is to start
with rule-based filtering from Screener.in, narrow the universe to companies
that pass quantitative checks, then use uploaded annual reports, concalls, and
quarterly updates to judge whether management commentary is credible and whether
the business outlook is attractive.

The project is intentionally built as a local-first tool. It keeps Screener
HTML, Excel exports, uploaded documents, vector indexes, and AI evaluations on
your machine.

## Architecture

The system is an ETL plus RAG pipeline:

```text
Screener screens
-> screen page HTML
-> company profile HTML
-> 11 HTML rules
-> Screener Excel exports
-> 2 Excel rules
-> >=75% rule-filtered stocks
-> document upload per stock
-> Chroma vector store
-> question-by-question AI evaluation
-> final score out of 100
```

The local app UI is served by `stock_screener_filter.app_server`. It starts
background pipeline steps, tracks logs/progress, shows rule details for each
stock, accepts document uploads, and triggers AI evaluation.

Core components:

```text
stock_screener_filter/screener_login.py
```

Opens a persistent Playwright browser profile and verifies Screener login. The
saved browser profile is reused by crawlers so you do not need to log in for
every request.

```text
stock_screener_filter/screen_page_crawler.py
```

Visits the configured Screener screen URLs, follows pagination, and saves each
rendered screen page HTML.

```text
stock_screener_filter/company_profile_crawler.py
```

Extracts company links from the screen pages, visits every company profile, and
saves the full rendered HTML with a delay between requests.

```text
stock_screener_filter/company_rule_analyzer.py
```

Parses the downloaded company profile HTML and evaluates the first 11 rules,
excluding banks, financial services, NBFCs, insurers, and similar companies.

```text
stock_screener_filter/company_excel_crawler.py
```

Downloads Screener Excel exports only for companies that pass enough of the HTML
rules.

```text
stock_screener_filter/company_excel_rule_analyzer.py
```

Reads the Excel exports and evaluates the SSGR and CFO/EBITDA rules.

```text
stock_screener_filter/rules_pipeline.py
```

Coordinates the ETL steps, cleans `data/current_run/` for fresh full runs, and
combines the HTML and Excel rule outputs into the final rule-filtered stock
list.

```text
stock_screener_filter/document_store.py
```

Deduplicates uploaded documents by SHA-256, extracts text, chunks it, embeds it
with local `BAAI/bge-m3`, and stores vectors in ChromaDB.

```text
stock_screener_filter/ai_evaluator.py
```

Runs the qualitative RAG evaluation. It retrieves focused evidence for each
question, uses DuckDuckGo snippets for news and industry context, calls Gemini
for structured analysis, and assigns the AI score.

Design decisions:

- **Local first:** generated data stays under `data/`, and secrets stay in
  `.env`.
- **Idempotent current run:** the app cleans and recreates `data/current_run/`
  for a fresh pipeline run, while old archived runs can remain under
  `data/runs/`.
- **Persistent browser login:** Screener auth is stored in `.browser/`, avoiding
  repeated login prompts during scraping.
- **Polite crawling:** crawlers use delays between requests to reduce timeout
  and throttling risk.
- **Rules before AI:** AI/RAG is only run after the quantitative filter, keeping
  document work focused on better candidates.
- **One stock, one vector collection:** uploaded documents are indexed separately
  per stock so retrieval does not mix evidence across companies.
- **Question-by-question RAG:** each qualitative question retrieves its own
  relevant chunks instead of answering every question from one broad context.
- **No financial firms:** banks, NBFCs, insurance, and financial-services
  businesses are excluded because the valuation, debt, cash-flow, and return
  rules are not comparable to normal operating companies.

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
rules, and the final >=75% rule filter. Older timestamped folders under
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
