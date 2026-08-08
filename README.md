# AI Stock Researcher

AI Stock Researcher is a local research assistant for automating the stock
screening workflow I used to do manually for investments. It starts with
rule-based filtering from Screener.in, narrows the universe to companies that
pass quantitative checks, and then uses uploaded reports and concalls to judge
management commentary, execution quality, and
business outlook.

The project is local-first. Screener HTML, Excel exports, uploaded documents,
Chroma vector indexes, model cache files, and AI evaluations stay on your
machine under `data/`. Secrets stay in `.env`, which is gitignored.

## What This App Does

This app has two stages.

Stage 1 is the quantitative Screener pipeline:

```text
Your Screener screens
-> all paginated screen result pages
-> every company profile linked from those screens
-> HTML-based rule checks
-> Screener Excel exports
-> Excel-based rule checks
-> companies that pass at least 10 of 13 rules
```

The 13 rules cover valuation, historical P/E comparison, ROCE, ROE, debt,
pledging, sales growth consistency, profit growth, price CAGR vs profit growth,
promoter holding change, SSGR, and CFO/EBITDA.

Stage 2 is document-based research:

```text
Upload reports/transcripts for filtered stocks
-> deduplicate files
-> extract and chunk text with page ranges
-> embed chunks with local BGE-M3
-> store vectors in ChromaDB
-> ask questions with citations
-> run AI evaluation and final scoring
```

The app is useful only if Screener is configured correctly before crawling.
The company profile quick-ratio card must contain the required custom ratios,
otherwise the crawler cannot collect the inputs needed for the rules.

This method is designed for non-financial operating companies. Do not use the
rule output for banks, NBFCs, insurance companies, financial-services firms, or
similar financial businesses. Their balance-sheet, debt, cash-flow, and return
metrics are not comparable to normal operating companies.

## Quantitative Rules

The pipeline evaluates these 13 rules for each non-financial company:

1. **P/E versus industry:** Stock P/E must be less than or equal to `1.25 x`
   Industry PE.
2. **P/E versus historical valuation:** Stock P/E must be less than or equal to
   `1.1 x` the historical P/E for at least two of the available 3-year, 5-year,
   and 7-year periods. At least two historical values must be available.
3. **ROCE:** ROCE must be greater than `10%`.
4. **ROE:** ROE must be greater than `10%`.
5. **Debt to equity:** Debt to equity must be less than `0.5`.
6. **DPR YOY:** DPR YOY must be greater than `0%`.
7. **Promoter pledging:** Pledged percentage must equal `0%`.
8. **Sales-growth consistency:** The crawler uses up to the latest 11 annual
   sales observations, which produces up to 10 year-over-year comparisons. At
   least `70%` of the available comparisons must show sales growth of `10%` or
   more. If fewer years are available, the same 70% test is applied to all
   available annual comparisons.
9. **Compounded profit growth:** At least two available periods among 3 years,
   5 years, 10 years, and TTM must have profit growth greater than `10%`.
10. **Price CAGR versus profit growth:** Stock Price CAGR must be lower than the
    corresponding compounded profit growth for at least two periods among
    3 years, 5 years, 10 years, and TTM. Screener's 1-year stock return is used
    for the TTM comparison.
11. **Promoter holding stability:** The decrease between the first and latest
    available annual promoter-holding observations must be less than
    `5 percentage points`.
12. **SSGR:** SSGR must be greater than `0` for every evaluated year and at
    least `10` for at least half of those years.
13. **CFO as a percentage of EBITDA:** The average CFO/EBITDA percentage over
    the latest five available years must be greater than `50%`.

An unavailable input is recorded as `missing`; it is not counted as a passed
rule. The final quantitative filter requires at least `10 of 13` rules, which
is the configured `>=75%` pass threshold. Excel rules 12 and 13 are downloaded
and evaluated only for companies that pass at least 6 of the first 11 rules.

## Prerequisites

Install these before running the project:

- Python 3.9 or newer
- Git
- Microsoft Edge or Google Chrome
- A Screener.in account
- A Gemini API key for AI/RAG evaluation

The scraper uses Playwright. By default this repo uses Microsoft Edge because it
has worked reliably with Screener login in this project. Other users can switch
to Chrome or Playwright Chromium by changing the browser channel.

## Tech Stack

- **Language:** Python 3.9+
- **Local web app:** Python standard-library HTTP server in
  `stock_screener_filter.app_server`
- **Browser automation / scraping:** Playwright with persistent browser
  profiles for Screener login sessions
- **Supported scraper browsers:** Microsoft Edge, Google Chrome, or
  Playwright Chromium
- **HTML parsing:** Beautiful Soup and Python HTML parsing utilities
- **Excel parsing:** OpenPyXL for Screener Excel exports
- **PDF text extraction:** PyPDF for uploaded reports and transcripts
- **Document chunking:** LangChain text splitters
- **Embedding model:** local `BAAI/bge-m3` through `sentence-transformers`
- **Vector database:** ChromaDB stored locally under `data/chroma_db/`
- **RAG orchestration:** LangGraph for multi-step AI evaluation flow
- **LLM:** Gemini, configured through `GEMINI_MODEL` in `.env`
- **Structured AI outputs:** Pydantic schemas plus JSON validation
- **External search:** DuckDuckGo search through `ddgs`
- **Local config/secrets:** `.env` loaded with `python-dotenv`
- **Current local state storage:** JSON files under `data/`

## Critical Screener Setup

Do this before running the full pipeline. This is the most important setup step.
The scraper reads rule inputs from Screener's company profile quick-ratio card.
If the required ratios are not visible in the browser profile used by Playwright,
profile crawling will fail or later rule outputs will be missing.

Configure your Screener company profile so the quick-ratio section matches this
format:

![Required Screener company profile quick-ratio format](<docs/images/Screenshot 2026-07-24 053742.png>)

These labels must be visible with these exact names:

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

Add this custom ratio in Screener:

```text
Ratio name: DPR YOY
Short name: DPR YOY
Ratio unit: Percentage
Formula: ((Depreciation -Depreciation last year)/Depreciation last year)*100
Description: Depreciation -Depreciation last year
```

The setup must be done in the same browser profile that the scraper uses. Open
that profile with:

```bash
python -m stock_screener_filter.screener_login --manual --keep-open
```

In the browser window that opens:

1. Log in to Screener.
2. Open any company page.
3. Configure the quick-ratio card.
4. Confirm all required labels are visible.
5. Press `Ctrl+C` in the terminal when done.

The app also verifies login before crawling. If Screener is logged out, it opens
the login window and waits for you to finish login.

### Required Screener Excel Template

The repository includes the custom Excel export template used by this project:

```text
excel_template_screener/TCS.xlsx
```

Upload this file as your Excel template in the Screener.in website before
running the pipeline. Although the file is named `TCS.xlsx`, it is the reusable
template Screener applies when exporting any company from your account.

This setup is required for Excel rules 12 and 13. The Excel analyzer expects the
worksheets, rows, formulas, SSGR values, and CFO/EBITDA values provided by this
template. Screener's default Excel export or a differently structured template
may cause those rules to be reported as `missing` or parsed incorrectly.

## Fresh Setup

Clone the repository:

```bash
git clone https://github.com/arakshay60/AI-Screener-Stock-Researcher.git
cd AI-Screener-Stock-Researcher
```

Create a virtual environment.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install Python dependencies:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Install Playwright browser support:

```bash
python -m playwright install chromium
```

This installs Playwright's bundled Chromium. The project can still use Edge or
Chrome if those browsers are already installed.

Create your local `.env` file:

Windows PowerShell:

```powershell
Copy-Item .env.example .env
notepad .env
```

macOS/Linux:

```bash
cp .env.example .env
nano .env
```

Set at least:

```env
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
STOCK_RESEARCHER_PORT=8765
```

Optional but recommended:

```env
SCREENER_SCREEN_URLS=https://www.screener.in/screens/your-first-screen/,https://www.screener.in/screens/your-second-screen/
EMBEDDING_DEVICE=cpu
EMBEDDING_BATCH_SIZE=16
RAG_CHUNK_SIZE=2500
RAG_CHUNK_OVERLAP=350
RAG_MIN_SIMILARITY_SCORE=0.30
DDG_RESULTS_PER_QUERY=5
DDG_SEARCH_TIMEOUT_SECONDS=15
```

Do not commit `.env`. It is intentionally ignored by Git.

## Browser Choice

The full pipeline reads the browser channel and persistent profile directory
from `.env`. To use Microsoft Edge, set:

```env
SCREENER_BROWSER_CHANNEL=msedge
SCREENER_PROFILE_DIR=.browser/screener-profile-edge
```

To use Google Chrome instead, set:

```env
SCREENER_BROWSER_CHANNEL=chrome
SCREENER_PROFILE_DIR=.browser/screener-profile-chrome
```

Keep the channel and profile directory paired as shown. Each profile has its
own Screener cookies and login session, so switching browsers may require one
fresh login. The same pair is used by the login helper, screen crawler, company
profile crawler, and Excel crawler.

To open the configured Chrome profile explicitly for setup or troubleshooting:

```bash
python -m stock_screener_filter.screener_login --browser-channel chrome --manual --keep-open
```

If you want to use Playwright's bundled Chromium:

```bash
python -m stock_screener_filter.screener_login --browser-channel chromium --manual --keep-open
```

If Screener login or custom ratios behave differently in one browser, use the
browser where your Screener setup works reliably.

## Screener Screens

The default screens use these Screener queries:

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

You can use your own Screener screens. Put the screen links in `.env`:

```env
SCREENER_SCREEN_URLS=https://www.screener.in/screens/your-first-screen/,https://www.screener.in/screens/your-second-screen/
```

You can also pass screen links directly for a one-off command:

```bash
python -m stock_screener_filter.screen_page_crawler --screens https://www.screener.in/screens/your-first-screen/ https://www.screener.in/screens/your-second-screen/
```

## Run The App

Start the local web UI:

```bash
python -m stock_screener_filter.app_server
```

Or use the cross-platform launcher:

```bash
python run_app.py
```

Open:

```text
http://127.0.0.1:8765
```

Click **Run Screener Pipeline**.

Expect the Screener crawling stage to take a long time. A full three-screen run
can take almost 2 hours depending on the number of companies, retries, internet
speed, and Screener response time. This is intentional: the crawler processes
requests one at a time and keeps several seconds between requests to respect
Screener's rate limits and reduce the risk of temporary throttling or blocking.
Do not close the app or stop its terminal while the pipeline is running.

The app will:

1. Clean and recreate `data/current_run/`.
2. Verify Screener login.
3. If not logged in, open the browser and wait for you to log in.
4. Download configured Screener screen pages.
5. Download company profile HTML.
6. Run the first 11 HTML rules.
7. Download Screener Excel exports for eligible companies.
8. Run the 2 Excel rules.
9. Save final stocks that pass the >=75% rule filter.

The crawler runs headlessly after login, so you usually will not see a browser
window during scraping. This is normal. The saved browser profile still carries
your Screener session cookies.

To stop the app server, return to the terminal and press:

```text
Ctrl+C
```

## Running Pipeline Steps Manually

The UI has individual step buttons, but you can also run commands yourself.

Verify/login to Screener:

```bash
python -m stock_screener_filter.screener_login --manual --keep-open
```

Download screen pages:

```bash
python -m stock_screener_filter.screen_page_crawler --output-dir data/current_run/screens --delay-seconds 3
```

Download company profiles:

```bash
python -m stock_screener_filter.company_profile_crawler --screen-dir data/current_run/screens --output-dir data/current_run/companies --delay-seconds 5 --quick-ratio-wait-seconds 8 --resume
```

Run HTML rules:

```bash
python -m stock_screener_filter.company_rule_analyzer --company-dir data/current_run/companies --output-dir data/current_run/companies/analysis
```

Download Excel exports:

```bash
python -m stock_screener_filter.company_excel_crawler --analysis-csv data/current_run/companies/analysis/company_rule_results.csv --output-dir data/current_run/companies/excel --min-passing-rules 6 --delay-seconds 10 --resume
```

Run Excel rules:

```bash
python -m stock_screener_filter.company_excel_rule_analyzer --excel-dir data/current_run/companies/excel --output-dir data/current_run/companies/excel/analysis
```

On Windows PowerShell, quote paths if they contain spaces:

```powershell
python -m stock_screener_filter.company_profile_crawler --screen-dir "C:\path with spaces\data\current_run\screens" --output-dir "C:\path with spaces\data\current_run\companies"
```

## Documents, RAG, And AI Evaluation

After the quantitative pipeline finishes, the UI shows the stocks that passed
the rule filter.

For each stock, you can upload reports or concalls in PDF, text, HTML, CSV, JSON,
or Markdown format.

During upload, the app:

1. Calculates a SHA-256 hash to deduplicate documents per stock.
2. Extracts text.
3. Chunks the document.
4. Creates embeddings using local `BAAI/bge-m3`.
5. Stores vectors in ChromaDB under `data/chroma_db/`.

For PDFs, chunks preserve page ranges:

```text
page_start=14
page_end=15
pages=14,15
```

So answers can cite:

```text
Report FY25, pp. 14-15
```

You can upload multiple documents for the same stock in one action. After you
select files, the UI creates one metadata row per file. For every document,
select:

- Document type: report or concall
- Year: FY format, such as FY2026
- Quarter/period: FY, Q1, Q2, Q3, or Q4

Annual reports should be uploaded as `Report` with period `FY`. Quarterly
concall transcripts should be uploaded as `Concall` with the relevant quarter.
These metadata fields are mandatory and are stored on each vector chunk, which
helps the app retrieve the right evidence when your question mentions a document
type or period.

The **Ask Documents** box lets you ask custom questions against a stock's
uploaded knowledge base. Answers are based only on retrieved document chunks and
include citations. You do not need to manually choose filters while asking. If
your question says something like `FY25`, `2025`, `Mar-25`, `Q2 FY25`,
`compare 2023 with 2024`, `from the report`, or `in the concall`, the app
infers the explicit periods and document types from the question and first tries
to retrieve matching uploaded documents. If no matching chunks are found, it
falls back to searching all uploaded documents for that stock. Relative phrases
such as `latest quarter` or `previous year` are not resolved yet.

When the app starts, the stock table includes both:

- Stocks from the current 75% rule-filtered output.
- Stocks that already have at least one uploaded document in
  `data/document_store/documents.json`.

That means you can ask document Q&A for previously uploaded stocks without
rerunning the Screener pipeline. AI scoring is still intended for rule-filtered
stocks because it combines the quantitative rule score with qualitative RAG
analysis.

### Document Q&A Observability

Every **Ask Documents** request creates a local trace JSON file under:

```text
data/qa_runs/
```

The UI also shows an expandable **Trace** panel below each answer. Use this to
debug whether the answer is trustworthy. It includes:

- Inferred metadata filters.
- Whether the app had to fall back from filtered retrieval to all documents.
- Raw retrieved chunks, accepted chunks sent to Gemini, and chunks rejected by
  the similarity threshold.
- Source IDs, page ranges, document metadata, and similarity scores.
- Which retrieved chunks Gemini cited and which retrieved chunks it ignored.
- The exact prompt sent to Gemini.
- Parsed model response, raw model text, and raw API payload.

This is the main debugging path for RAG accuracy. If an answer looks wrong,
first check whether the right chunks were retrieved and whether the
`RAG_MIN_SIMILARITY_SCORE` threshold is too strict or too loose. If retrieval
looks good but the answer is weak, inspect the prompt and the cited source IDs.

The **Evaluate** button runs the qualitative AI analysis for a stock. The
question-by-question workflow is:

```text
future outlook
-> initiatives
-> promise delivery
-> exaggeration risk
-> news sentiment
-> industry outlook
-> final scoring
```

AI evaluations are cached by uploaded document fingerprint. If no new document
has been uploaded for a stock, the app reuses the old AI evaluation instead of
calling Gemini again.

## Generated Data

These folders are generated locally and ignored by Git:

```text
.browser/
data/
.env
.venv/
```

Important generated locations:

```text
data/current_run/              latest pipeline run
data/document_store/           uploaded document metadata and raw files
data/chroma_db/                Chroma vector database
data/model_cache/              downloaded BGE-M3 embedding model
data/ai_evaluations/           saved AI outputs
data/search_cache/             DuckDuckGo search cache
```

`data/current_run/` is cleaned on every full app pipeline run. Other generated
folders are kept so uploaded documents, vectors, model files, and AI evaluations
can be reused.

## Architecture

The local app UI is served by:

```text
stock_screener_filter/app_server.py
```

It starts background pipeline steps, tracks logs/progress, shows rule details,
accepts document uploads, serves document Q&A, and triggers AI evaluation.

Core components:

```text
stock_screener_filter/screener_login.py
```

Opens a persistent Playwright browser profile and verifies Screener login.

```text
stock_screener_filter/screen_page_crawler.py
```

Visits configured Screener screen URLs, follows pagination, and saves rendered
screen HTML.

```text
stock_screener_filter/company_profile_crawler.py
```

Extracts company links from screen pages, visits each company profile, waits for
required quick-ratio labels, and saves full rendered HTML.

```text
stock_screener_filter/company_rule_analyzer.py
```

Parses company profile HTML and evaluates the first 11 rules.

```text
stock_screener_filter/company_excel_crawler.py
```

Downloads Screener Excel exports for companies that pass enough HTML rules.

```text
stock_screener_filter/company_excel_rule_analyzer.py
```

Reads Excel files and evaluates SSGR and CFO/EBITDA rules.

```text
stock_screener_filter/rules_pipeline.py
```

Coordinates full and step-by-step ETL execution.

```text
stock_screener_filter/document_store.py
```

Deduplicates uploads, extracts text, chunks documents, creates BGE-M3
embeddings, and stores vectors in ChromaDB.

```text
stock_screener_filter/ai_evaluator.py
```

Runs document Q&A and qualitative RAG evaluation with Gemini.

Design decisions:

- **Local first:** generated data stays on your machine.
- **Persistent browser profile:** Screener login lives in `.browser/`.
- **Login preflight:** the pipeline checks Screener login before crawling.
- **Polite crawling:** requests run one at a time with delays.
- **Rules before AI:** RAG is only used after quantitative filtering.
- **Per-stock vector collections:** documents for one stock do not mix with
  documents for another stock.
- **Question-by-question RAG:** each qualitative question retrieves its own
  evidence.
- **Document fingerprint caching:** AI evaluations are reused until new
  documents are uploaded.

## Troubleshooting

### The app says Screener is not logged in

Run:

```bash
python -m stock_screener_filter.screener_login --manual --keep-open
```

Log in in the opened browser. Then rerun the app pipeline.

### Login completed but the app still waits

Close the login browser and run the helper again:

```bash
python -m stock_screener_filter.screener_login --manual --timeout-seconds 300
```

It should print:

```text
Already logged in to Screener.
```

### Required quick-ratio labels are missing

Open the scraper browser profile:

```bash
python -m stock_screener_filter.screener_login --manual --keep-open
```

In that browser, open a company profile and confirm the required ratios are
visible. If they are missing, configure the Screener quick-ratio card again in
that browser profile.

### No browser appears while scraping

That is normal. Crawlers run headlessly by default after login. To watch a
manual crawler command, pass:

```bash
--headed
```

Example:

```bash
python -m stock_screener_filter.company_profile_crawler --screen-dir data/current_run/screens --output-dir data/current_run/companies --headed
```

### Screener is slow or starts timing out

Increase delays:

```bash
--delay-seconds 8
```

Avoid rerunning large crawls repeatedly in a short period.

The scraper is deliberately conservative. A complete run can take almost 2
hours because the app spaces out requests to respect Screener's rate limits and
reduce the chance of Screener temporarily blocking or throttling your
connection.

### First document upload is slow

The first RAG upload may download the `BAAI/bge-m3` embedding model into:

```text
data/model_cache/
```

After that, uploads reuse the local model cache.

### Gemini errors

Check `.env`:

```env
GEMINI_API_KEY=your-gemini-api-key
GEMINI_MODEL=gemini-3.5-flash
```

Restart the app after changing `.env`.

### Port already in use

Change the port in `.env`:

```env
STOCK_RESEARCHER_PORT=8766
```

Then restart:

```bash
python -m stock_screener_filter.app_server
```

## Validation

To quickly check that Python files compile:

```bash
python -m compileall stock_screener_filter
```

This does not test Screener access, Gemini access, or document upload behavior,
but it catches syntax/import issues.

## Roadmap

These are planned features and architecture upgrades for future versions:

1. **Hybrid retrieval with BM25, RRF, and cross-encoder reranking**

   Add keyword retrieval alongside semantic search, fuse both rankings with
   Reciprocal Rank Fusion, and rerank candidate chunks with a cross-encoder.
   This should improve retrieval for exact metric names, management phrases,
   project names, years, quarters, and financial terms.

   Issue: https://github.com/arakshay60/AI-Screener-Stock-Researcher/issues/1

2. **Table-aware chunking for financial PDFs**

   Extract tables separately from reports and concall PDFs, convert them to
   Markdown, and store each table as its own retrievable chunk with nearby
   heading/paragraph context. This is important for P&L tables, segment revenue,
   guidance tables, order books, and other numeric disclosures.

   Issue: https://github.com/arakshay60/AI-Screener-Stock-Researcher/issues/2

3. **Agentic query router and multi-step RAG orchestration**

   Replace the single fixed retrieve-and-answer flow with a lightweight
   upfront router plus an agentic RAG orchestrator. The orchestrator should be
   able to decompose compound questions, walk across periods, summarize whole
   documents, verify absence/negative queries, handle verbatim quote requests,
   and run deterministic compute steps.

   Issue: https://github.com/arakshay60/AI-Screener-Stock-Researcher/issues/3

4. **Migrate app state from JSON files to SQLite**

   Move document metadata, deduplication hashes, AI evaluation cache, Q&A
   traces, and pipeline run state from JSON files into a local SQLite database.
   Chroma should remain responsible for vector storage, while SQLite becomes
   the source of truth for structured local app state.

   Issue: https://github.com/arakshay60/AI-Screener-Stock-Researcher/issues/4

5. **Whole-knowledge-base Q&A across stocks**

   Add the ability to ask questions across the full uploaded knowledge base,
   not just one stock at a time. This should support comparing different
   companies, comparing companies in the same industry, identifying common
   themes, and contrasting management commentary across peers.

6. **Web search inside Document Q&A**

   Add an optional search tool to Document Q&A for questions that require
   latest news, recent market sentiment, industry trends, regulatory updates,
   or other online context that is not present in uploaded documents.
