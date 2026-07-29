# AI Stock Researcher

AI Stock Researcher is a local research assistant for automating the stock
screening workflow I used to do manually for investments. It starts with
rule-based filtering from Screener.in, narrows the universe to companies that
pass quantitative checks, and then uses uploaded annual reports, concalls, and
quarterly reports to judge management commentary, execution quality, and
business outlook.

The project is local-first. Screener HTML, Excel exports, uploaded documents,
Chroma vector indexes, model cache files, and AI evaluations stay on your
machine under `data/`. Secrets stay in `.env`, which is gitignored.

## What This App Does

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
-> document Q&A and AI evaluation
-> final score out of 100
```

The final quantitative filter currently requires at least 75% of the 13 rules,
which means a company must pass at least 10 out of 13 rules.

This method is designed for non-financial operating companies. Do not use the
rule output for banks, NBFCs, insurance companies, financial-services firms, or
similar financial businesses. Their balance-sheet, debt, cash-flow, and return
metrics are not comparable to normal operating companies.

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
GEMINI_MODEL=gemini-flash-lite-latest
STOCK_RESEARCHER_PORT=8765
```

Optional but recommended:

```env
SCREENER_SCREEN_URLS=https://www.screener.in/screens/your-first-screen/,https://www.screener.in/screens/your-second-screen/
EMBEDDING_DEVICE=cpu
EMBEDDING_BATCH_SIZE=16
RAG_CHUNK_SIZE=2500
RAG_CHUNK_OVERLAP=350
DDG_RESULTS_PER_QUERY=5
DDG_SEARCH_TIMEOUT_SECONDS=15
```

Do not commit `.env`. It is intentionally ignored by Git.

## Browser Choice

The default browser channel is currently:

```text
msedge
```

That means Playwright uses installed Microsoft Edge and stores the Screener
session under:

```text
.browser/screener-profile-edge
```

If you want to use Chrome manually:

```bash
python -m stock_screener_filter.screener_login --browser-channel chrome --manual --keep-open
```

If you want to use Playwright's bundled Chromium:

```bash
python -m stock_screener_filter.screener_login --browser-channel chromium --manual --keep-open
```

If Screener login or custom ratios behave differently in one browser, use the
browser where your Screener setup works reliably.

## Screener Setup

The scraper depends on Screener's company profile quick-ratio card. Configure
your Screener company profile so the quick-ratio section matches this format:

![Required Screener company profile quick-ratio format](<docs/images/Screenshot 2026-07-24 053742.png>)

At minimum, these ratios must be visible with these exact names:

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

Important: configure this in the same browser profile used by the scraper. The
easiest way is:

```bash
python -m stock_screener_filter.screener_login --manual --keep-open
```

In the opened browser window:

1. Log in to Screener.
2. Open any company page.
3. Configure the quick-ratio card.
4. Confirm the required labels are visible.
5. Press `Ctrl+C` in the terminal when done.

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

Windows users can also use:

```powershell
.\run_app.ps1
```

Open:

```text
http://127.0.0.1:8765
```

Click **Run Screener Pipeline**.

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

For each stock, you can upload:

- Annual reports
- Concall transcripts
- Quarterly reports
- Text, HTML, CSV, JSON, or PDF files

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
Annual Report FY25, pp. 14-15
```

When uploading, you can optionally enter:

- Year
- Quarter

Annual reports usually only need year. Quarterly reports can use year and
quarter. These metadata fields can later be used as filters when asking
questions.

The **Ask Documents** box lets you ask custom questions against a stock's
uploaded knowledge base. Answers are based only on retrieved document chunks and
include citations.

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
GEMINI_MODEL=gemini-flash-lite-latest
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
