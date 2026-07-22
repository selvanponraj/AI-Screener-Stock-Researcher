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
