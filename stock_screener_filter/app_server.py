"""Local web UI for the stock research workflow."""

from __future__ import annotations

import cgi
import json
import os
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from stock_screener_filter.config import load_env
from stock_screener_filter import ai_evaluator, document_store, rules_pipeline


load_env()
HOST = "127.0.0.1"
PORT = int(os.environ.get("STOCK_RESEARCHER_PORT", "8765"))


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.running = False
        self.phase = "idle"
        self.logs: list[str] = []
        self.stocks: list[dict[str, Any]] = rules_pipeline.load_current_rule_filtered()
        self.error: str | None = None
        self.error_trace: str | None = None
        self.progress_current = 0
        self.progress_total = 1
        self.current_step = "Idle"

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            if not self.running:
                disk_stocks = rules_pipeline.load_current_rule_filtered()
                if disk_stocks:
                    self.stocks = disk_stocks
                steps = rules_pipeline.pipeline_status()
                if self.error and failed_step_is_now_complete(self.error, steps):
                    self.error = None
                    self.error_trace = None
                    self.phase = "idle"
                    self.current_step = "Idle"
            else:
                steps = rules_pipeline.pipeline_status()
            return {
                "running": self.running,
                "phase": self.phase,
                "current_step": self.current_step,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "progress_percent": round(
                    self.progress_current / max(1, self.progress_total) * 100,
                    1,
                ),
                "logs": list(self.logs),
                "pipeline_steps": steps,
                "stocks": enrich_stocks(self.stocks),
                "error": self.error,
                "error_trace": self.error_trace,
            }

    def progress(self, current: int, total: int, step: str) -> None:
        with self.lock:
            self.progress_current = current
            self.progress_total = max(1, total)
            self.current_step = step


STATE = AppState()


def failed_step_is_now_complete(error: str, steps: list[dict[str, Any]]) -> bool:
    module_to_step = {
        "screener_login": "login",
        "screen_page_crawler": "screens",
        "company_profile_crawler": "profiles",
        "company_rule_analyzer": "html_rules",
        "company_excel_crawler": "excel_downloads",
        "company_excel_rule_analyzer": "excel_rules",
    }
    complete_by_id = {str(step["id"]): bool(step["complete"]) for step in steps}
    for module, step_id in module_to_step.items():
        if module in error:
            return complete_by_id.get(step_id, False)
    return False


def enrich_stocks(stocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched = []
    for stock in stocks:
        stock_id = str(stock["stock_id"])
        enriched.append(
            {
                **stock,
                "documents": document_store.stock_documents(stock_id),
                "ai_evaluation": ai_evaluator.load_evaluation(stock_id),
            }
        )
    return enriched


def run_pipeline_background() -> None:
    with STATE.lock:
        STATE.running = True
        STATE.phase = "running screener/rules pipeline"
        STATE.error = None
        STATE.error_trace = None
        STATE.logs = []
        STATE.progress_current = 0
        STATE.progress_total = 7
        STATE.current_step = "Starting"
    try:
        stocks = rules_pipeline.run_full_pipeline(STATE.log, progress=STATE.progress)
        with STATE.lock:
            STATE.stocks = stocks
            STATE.phase = "pipeline complete"
            STATE.current_step = "Upload documents for filtered stocks"
    except Exception as exc:
        trace = traceback.format_exc()
        STATE.log(trace)
        with STATE.lock:
            STATE.error = str(exc)
            STATE.error_trace = trace
            STATE.phase = "failed"
            STATE.current_step = "Failed"
    finally:
        with STATE.lock:
            STATE.running = False


def run_step_background(step_id: str) -> None:
    with STATE.lock:
        STATE.running = True
        STATE.phase = f"running step: {step_id}"
        STATE.error = None
        STATE.error_trace = None
        STATE.logs = []
        STATE.progress_current = 0
        STATE.progress_total = 1
        STATE.current_step = step_id
    try:
        stocks = rules_pipeline.run_step(step_id, STATE.log, progress=STATE.progress)
        with STATE.lock:
            if stocks:
                STATE.stocks = stocks
            else:
                STATE.stocks = rules_pipeline.load_current_rule_filtered()
            STATE.phase = f"step complete: {step_id}"
            STATE.current_step = "Step complete"
    except Exception as exc:
        trace = traceback.format_exc()
        STATE.log(trace)
        with STATE.lock:
            STATE.error = str(exc)
            STATE.error_trace = trace
            STATE.phase = f"step failed: {step_id}"
            STATE.current_step = "Failed"
    finally:
        with STATE.lock:
            STATE.running = False


def evaluate_background(stock_id: str | None) -> None:
    with STATE.lock:
        STATE.running = True
        STATE.phase = "running AI/RAG evaluation"
        STATE.error = None
        STATE.error_trace = None
        stocks = list(STATE.stocks)
    try:
        selected = [stock for stock in stocks if not stock_id or stock["stock_id"] == stock_id]
        for index, stock in enumerate(selected, start=1):
            current = ai_evaluator.current_evaluation(str(stock["stock_id"]))
            action = "Using cached AI evaluation" if current else "AI evaluating"
            STATE.progress(index - 1, max(1, len(selected)), f"{action} {stock['company_name']}")
            STATE.log(f"[{index}/{len(selected)}] {action} {stock['company_name']}")
            ai_evaluator.evaluate_stock(stock)
        with STATE.lock:
            STATE.phase = "AI evaluation complete"
            STATE.current_step = "AI evaluation complete"
            STATE.progress_current = STATE.progress_total
    except Exception as exc:
        trace = traceback.format_exc()
        STATE.log(trace)
        with STATE.lock:
            STATE.error = str(exc)
            STATE.error_trace = trace
            STATE.phase = "AI evaluation failed"
            STATE.current_step = "AI evaluation failed"
    finally:
        with STATE.lock:
            STATE.running = False


class Handler(BaseHTTPRequestHandler):
    server_version = "StockResearcher/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
        elif parsed.path == "/api/status":
            self.send_json(STATE.snapshot())
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/run":
            self.start_pipeline()
        elif parsed.path == "/api/run-step":
            self.start_step()
        elif parsed.path == "/api/upload":
            self.upload_document()
        elif parsed.path == "/api/evaluate":
            self.start_evaluation()
        elif parsed.path == "/api/ask":
            self.ask_documents()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def start_pipeline(self) -> None:
        with STATE.lock:
            if STATE.running:
                self.send_json({"ok": False, "error": "A job is already running."}, status=409)
                return
        thread = threading.Thread(target=run_pipeline_background, daemon=True)
        thread.start()
        self.send_json({"ok": True})

    def start_step(self) -> None:
        with STATE.lock:
            if STATE.running:
                self.send_json({"ok": False, "error": "A job is already running."}, status=409)
                return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        step_id = payload.get("step_id")
        if step_id not in rules_pipeline.PIPELINE_STEPS:
            self.send_json({"ok": False, "error": f"Unknown step: {step_id}"}, status=400)
            return
        thread = threading.Thread(target=run_step_background, args=(step_id,), daemon=True)
        thread.start()
        self.send_json({"ok": True})

    def start_evaluation(self) -> None:
        with STATE.lock:
            if STATE.running:
                self.send_json({"ok": False, "error": "A job is already running."}, status=409)
                return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        thread = threading.Thread(
            target=evaluate_background,
            args=(payload.get("stock_id") or None,),
            daemon=True,
        )
        thread.start()
        self.send_json({"ok": True})

    def upload_document(self) -> None:
        try:
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                },
            )
            stock_id = form.getfirst("stock_id", "")
            company_name = form.getfirst("company_name", "")
            document_year = form.getfirst("document_year", "").strip()
            document_quarter = form.getfirst("document_quarter", "").strip()
            if not stock_id or "file" not in form:
                self.send_json({"ok": False, "error": "stock_id and file are required."}, status=400)
                return
            files = form["file"]
            if not isinstance(files, list):
                files = [files]
            stored = []
            for item in files:
                if not item.filename:
                    continue
                stored_doc = document_store.save_upload(
                    stock_id,
                    company_name,
                    item.filename,
                    item.file,
                    document_year=document_year,
                    document_quarter=document_quarter,
                )
                stored.append(stored_doc.__dict__)
            self.send_json({"ok": True, "documents": stored})
        except Exception as exc:
            trace = traceback.format_exc()
            STATE.log(trace)
            with STATE.lock:
                STATE.error = str(exc)
                STATE.error_trace = trace
            self.send_json({"ok": False, "error": str(exc), "trace": trace}, status=500)

    def ask_documents(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            stock_id = str(payload.get("stock_id", ""))
            question = str(payload.get("question", ""))
            document_year = str(payload.get("document_year", "")).strip()
            document_quarter = str(payload.get("document_quarter", "")).strip()
            if not stock_id or not question.strip():
                self.send_json({"ok": False, "error": "stock_id and question are required."}, status=400)
                return
            with STATE.lock:
                stocks = list(STATE.stocks) or rules_pipeline.load_current_rule_filtered()
            stock = next((item for item in stocks if str(item.get("stock_id")) == stock_id), None)
            if not stock:
                self.send_json({"ok": False, "error": f"Unknown stock: {stock_id}"}, status=404)
                return
            answer = ai_evaluator.answer_document_question(
                stock,
                question,
                document_year=document_year,
                document_quarter=document_quarter,
            )
            self.send_json({"ok": True, "answer": answer})
        except Exception as exc:
            trace = traceback.format_exc()
            STATE.log(trace)
            with STATE.lock:
                STATE.error = str(exc)
                STATE.error_trace = trace
            self.send_json({"ok": False, "error": str(exc), "trace": trace}, status=500)

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(format % args)


INDEX_HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Stock Researcher</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f7f4;
      --panel: #ffffff;
      --ink: #202124;
      --muted: #626b74;
      --line: #d9ded8;
      --accent: #176b5f;
      --accent-strong: #0d4f45;
      --warn: #9a5b05;
      --bad: #a7332f;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Arial, Helvetica, sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 16px 22px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 { font-size: 20px; margin: 0; }
    main {
      display: grid;
      grid-template-columns: 320px 1fr;
      min-height: calc(100vh - 65px);
    }
    aside {
      padding: 18px;
      border-right: 1px solid var(--line);
      background: #fbfbf9;
    }
    section { padding: 18px 22px; }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      height: 36px;
      padding: 0 12px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 700;
    }
    button.secondary {
      background: #fff;
      color: var(--accent);
    }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type="password"], input[type="text"], input[type="number"], textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
    }
    input[type="password"], input[type="text"], input[type="number"], select { height: 34px; padding-top: 0; padding-bottom: 0; }
    textarea { min-height: 64px; resize: vertical; font-family: inherit; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 14px 0 6px; }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; }
    .step-list {
      display: grid;
      gap: 8px;
      margin-top: 14px;
    }
    .step-row {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 9px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .step-title {
      font-weight: 700;
      font-size: 13px;
    }
    .step-summary {
      margin-top: 3px;
      color: var(--muted);
      font-size: 12px;
    }
    .step-check {
      color: #116149;
      font-weight: 900;
      margin-right: 5px;
    }
    .status {
      margin-top: 16px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--panel);
      font-size: 13px;
      color: var(--muted);
    }
    pre {
      max-height: 260px;
      overflow: auto;
      padding: 12px;
      border: 1px solid var(--line);
      background: #101514;
      color: #edf5f1;
      border-radius: 8px;
      font-size: 12px;
      white-space: pre-wrap;
    }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
      border: 1px solid var(--line);
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 9px 10px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }
    th { background: #eef3ef; color: #2c403b; position: sticky; top: 65px; }
    .stock-name { font-weight: 700; font-size: 14px; }
    .muted { color: var(--muted); }
    .badge {
      display: inline-block;
      padding: 3px 7px;
      border-radius: 999px;
      background: #e8f4ef;
      color: var(--accent-strong);
      font-weight: 700;
      font-size: 12px;
    }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .docs { margin-top: 8px; font-size: 12px; color: var(--muted); }
    .upload { display: grid; gap: 7px; min-width: 220px; }
    .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 7px; }
    .qa-box { margin-top: 12px; display: grid; gap: 7px; min-width: 260px; }
    .qa-answer {
      margin-top: 8px;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fbfbf9;
      font-size: 12px;
      white-space: pre-wrap;
    }
    .progress-shell {
      width: 100%;
      height: 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      overflow: hidden;
      background: #eef0ec;
      margin: 10px 0 6px;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      background: var(--accent);
      transition: width 0.2s ease;
    }
    .error-box {
      margin-top: 12px;
      border: 1px solid #e3b4b1;
      background: #fff6f5;
      color: var(--bad);
      border-radius: 8px;
      padding: 10px;
      display: none;
      white-space: pre-wrap;
      max-height: 180px;
      overflow: auto;
    }
    .prompt {
      margin-bottom: 14px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      color: var(--muted);
    }
    .rule-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 5px;
      margin-top: 8px;
    }
    .rule-chip {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 4px 6px;
      font-size: 11px;
      background: #fff;
    }
    .rule-detail {
      color: var(--muted);
      margin-top: 3px;
      line-height: 1.3;
    }
    .rule-chip.pass { border-color: #b8d7c8; color: #116149; background: #edf8f2; }
    .rule-chip.fail { border-color: #e1bbb6; color: var(--bad); background: #fff5f3; }
    .rule-chip.missing { border-color: #e0c996; color: var(--warn); background: #fff9e8; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      th { position: static; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Stock Researcher</h1>
      <div class="toolbar">
      <button id="refreshBtn" class="secondary">Refresh</button>
      <button id="runBtn">Run Screener Pipeline</button>
    </div>
  </header>
  <main>
    <aside>
      <div class="status">
        Gemini credentials are read from <strong>.env</strong>.
      </div>
      <label>Pipeline Steps</label>
      <div id="steps" class="step-list"></div>
      <div class="toolbar" style="margin-top: 12px;">
        <button id="evalAllBtn" class="secondary">Evaluate All</button>
      </div>
      <div class="status">
        <div><strong>Phase:</strong> <span id="phase">idle</span></div>
        <div><strong>Step:</strong> <span id="step">Idle</span></div>
        <div class="progress-shell"><div id="progressFill" class="progress-fill"></div></div>
        <div><span id="progressText">0%</span></div>
        <div><strong>Running:</strong> <span id="running">false</span></div>
        <div><strong>Filtered stocks:</strong> <span id="count">0</span></div>
        <div id="error" class="bad"></div>
      </div>
      <div id="errorBox" class="error-box"></div>
      <label>Logs</label>
      <pre id="logs"></pre>
    </aside>
    <section>
      <div id="uploadPrompt" class="prompt">
        Run the Screener pipeline. Once stocks pass the 75% rule filter, upload annual reports, concalls, or quarterly reports here for each stock.
      </div>
      <table>
        <thead>
          <tr>
            <th>Stock</th>
            <th>Rules</th>
            <th>Documents</th>
            <th>AI Score</th>
            <th>Verdict</th>
          </tr>
        </thead>
        <tbody id="stocks"></tbody>
      </table>
    </section>
  </main>
  <script>
    let latest = {};
    let qaAnswers = {};
    const $ = (id) => document.getElementById(id);

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    async function refresh() {
      latest = await api('/api/status');
      $('phase').textContent = latest.phase;
      $('step').textContent = latest.current_step || '';
      $('progressFill').style.width = `${latest.progress_percent || 0}%`;
      $('progressText').textContent = `${latest.progress_percent || 0}% (${latest.progress_current || 0}/${latest.progress_total || 1})`;
      $('running').textContent = latest.running;
      $('count').textContent = latest.stocks.length;
      $('error').textContent = latest.error || '';
      $('errorBox').style.display = latest.error ? 'block' : 'none';
      $('errorBox').textContent = latest.error_trace || latest.error || '';
      $('logs').textContent = (latest.logs || []).slice(-120).join('\n');
      $('runBtn').disabled = latest.running;
      $('evalAllBtn').disabled = latest.running || !latest.stocks.length;
      renderSteps(latest.pipeline_steps || []);
      renderStocks(latest.stocks || []);
    }

    function renderSteps(steps) {
      const container = $('steps');
      container.innerHTML = '';
      for (const step of steps) {
        const row = document.createElement('div');
        row.className = 'step-row';
        row.innerHTML = `
          <div>
            <div class="step-title">${step.complete ? '<span class="step-check">✓</span>' : ''}${escapeHtml(step.label)}</div>
            <div class="step-summary">${escapeHtml(step.summary || '')}</div>
          </div>
          <button class="secondary step-run" data-step="${escapeAttr(step.id)}">Run</button>
        `;
        container.appendChild(row);
      }
      document.querySelectorAll('.step-run').forEach(button => {
        button.disabled = latest.running;
        button.addEventListener('click', async () => {
          await api('/api/run-step', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ step_id: button.dataset.step })
          });
          await refresh();
        });
      });
    }

    function renderStocks(stocks) {
      const body = $('stocks');
      const openDetails = new Set(
        Array.from(document.querySelectorAll('details.rule-details[open]'))
          .map(detail => detail.dataset.stock)
      );
      body.innerHTML = '';
      $('uploadPrompt').textContent = stocks.length
        ? `Upload annual reports, concalls, or quarterly reports for the ${stocks.length} stocks that passed the 75% rule filter. Duplicate files are skipped automatically.`
        : 'Run the Screener pipeline. Once stocks pass the 75% rule filter, upload annual reports, concalls, or quarterly reports here for each stock.';
      for (const stock of stocks) {
        const tr = document.createElement('tr');
        const docs = stock.documents || [];
        const ai = stock.ai_evaluation || {};
        const rules = stock.rule_details || ruleDetailsFromStock(stock);
        tr.innerHTML = `
          <td>
            <div class="stock-name">${escapeHtml(stock.company_name)}</div>
            <div class="muted">${escapeHtml(stock.market_categories || '')}</div>
            <div class="muted">${escapeHtml(stock.company_url || '')}</div>
          </td>
          <td>
            <span class="badge">${stock.total_rule_pass_count}/${stock.total_rule_count}</span>
            <div class="muted">${stock.rule_pass_percentage}% passed</div>
            <div class="muted">Rule score: ${stock.rule_score_out_of_50}/50</div>
            <details class="rule-details" data-stock="${escapeAttr(stock.stock_id)}" ${openDetails.has(stock.stock_id) ? 'open' : ''}>
              <summary class="muted">Rule details</summary>
              <div class="rule-grid">
                ${rules.map(rule => `
                  <div class="rule-chip ${escapeAttr(rule.status)}">
                    <strong>${escapeHtml(shortRuleName(rule.name))}: ${escapeHtml(rule.status)}</strong>
                    <div class="rule-detail">${escapeHtml(rule.detail || '')}</div>
                  </div>
                `).join('')}
              </div>
            </details>
          </td>
          <td>
            <form class="upload" data-stock="${stock.stock_id}" data-name="${escapeAttr(stock.company_name)}">
              <div class="field-row">
                <input type="number" name="document_year" min="1900" max="2100" placeholder="Year">
                <select name="document_quarter">
                  <option value="">Quarter optional</option>
                  <option value="Q1">Q1</option>
                  <option value="Q2">Q2</option>
                  <option value="Q3">Q3</option>
                  <option value="Q4">Q4</option>
                </select>
              </div>
              <input type="file" name="file" multiple accept=".pdf,.txt,.md,.html,.htm,.csv,.json">
              <button type="submit" class="secondary">Upload</button>
            </form>
            <div class="docs">${docs.length} document(s), ${docs.reduce((a,d)=>a+(d.chunk_count||0),0)} chunks</div>
            <form class="qa-box" data-stock="${stock.stock_id}">
              <textarea name="question" placeholder="Ask this stock's uploaded documents"></textarea>
              <div class="field-row">
                <input type="number" name="document_year" min="1900" max="2100" placeholder="Filter year">
                <select name="document_quarter">
                  <option value="">All quarters</option>
                  <option value="Q1">Q1</option>
                  <option value="Q2">Q2</option>
                  <option value="Q3">Q3</option>
                  <option value="Q4">Q4</option>
                </select>
              </div>
              <button type="submit" class="secondary">Ask Documents</button>
            </form>
            <div class="qa-answer" id="qa-${escapeAttr(stock.stock_id)}" style="${qaAnswers[stock.stock_id] ? '' : 'display:none;'}">${renderQaAnswer(qaAnswers[stock.stock_id])}</div>
          </td>
          <td>
            ${ai.total_score_out_of_100 !== undefined ? `<span class="badge">${ai.total_score_out_of_100}/100</span>` : '<span class="muted">Not run</span>'}
            <div class="muted">AI: ${ai.ai_score_out_of_50 ?? '-'}/50</div>
            <button class="secondary eval-one" data-stock="${stock.stock_id}">Evaluate</button>
          </td>
          <td>
            <div>${escapeHtml(ai.verdict || '')}</div>
            <div class="muted">${escapeHtml((ai.key_reasons || []).join(' | '))}</div>
          </td>
        `;
        body.appendChild(tr);
      }
      document.querySelectorAll('form.upload').forEach(form => {
        form.addEventListener('submit', uploadForm);
      });
      document.querySelectorAll('form.qa-box').forEach(form => {
        form.addEventListener('submit', askForm);
      });
      document.querySelectorAll('.eval-one').forEach(btn => {
        btn.addEventListener('click', () => evaluate(btn.dataset.stock));
      });
    }

    async function uploadForm(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const input = form.querySelector('input[type=file]');
      if (!input.files.length) return;
      const data = new FormData();
      data.append('stock_id', form.dataset.stock);
      data.append('company_name', form.dataset.name);
      data.append('document_year', form.elements.document_year.value || '');
      data.append('document_quarter', form.elements.document_quarter.value || '');
      for (const file of input.files) data.append('file', file);
      await api('/api/upload', { method: 'POST', body: data });
      input.value = '';
      await refresh();
    }

    async function askForm(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const stockId = form.dataset.stock;
      const question = form.question.value.trim();
      if (!question) return;
      qaAnswers[stockId] = { loading: true, answer: 'Searching uploaded documents...' };
      renderStocks(latest.stocks || []);
      try {
        const response = await api('/api/ask', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            stock_id: stockId,
            question,
            document_year: form.elements.document_year.value || '',
            document_quarter: form.elements.document_quarter.value || ''
          })
        });
        qaAnswers[stockId] = response.answer;
      } catch (error) {
        qaAnswers[stockId] = { answer: `Error: ${error.message}`, citations: [], limitations: [] };
      }
      renderStocks(latest.stocks || []);
    }

    async function evaluate(stockId) {
      const payload = {
        stock_id: stockId || ''
      };
      await api('/api/evaluate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      await refresh();
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]));
    }
    function escapeAttr(value) { return escapeHtml(value).replace(/"/g, '&quot;'); }
    function shortRuleName(name) {
      const names = {
        pe_vs_industry: 'PE/industry',
        pe_vs_historical: 'PE/history',
        roce_over_10: 'ROCE',
        roe_over_10: 'ROE',
        debt_to_equity_under_0_5: 'D/E',
        dpr_yoy_positive: 'DPR YoY',
        pledged_zero: 'Pledged',
        sales_yoy_growth: 'Sales YoY',
        profit_growth_over_10: 'Profit growth',
        stock_cagr_below_profit_growth: 'CAGR < profit',
        promoter_holding_decrease_under_5: 'Promoter',
        rule_12_ssgr: 'SSGR',
        rule_13_cfo_ebitda: 'CFO/EBITDA'
      };
      return names[name] || name;
    }
    function renderQaAnswer(result) {
      if (!result) return '';
      if (result.loading) return escapeHtml(result.answer || '');
      const citations = (result.citations || []).map(citation => {
        const pageStart = citation.page_start;
        const pageEnd = citation.page_end || pageStart;
        const page = pageStart && pageEnd && pageStart !== pageEnd
          ? `pp. ${pageStart}-${pageEnd}`
          : (pageStart ? `p. ${pageStart}` : 'page unknown');
        const meta = [citation.document_year, citation.document_quarter].filter(Boolean).join(' ');
        return `${citation.source_id}: ${citation.document_name} (${page}${meta ? ', ' + meta : ''})`;
      });
      const limitations = result.limitations || [];
      return `
${escapeHtml(result.answer || '')}
${citations.length ? '\n\nCitations:\n' + escapeHtml(citations.map(item => '- ' + item).join('\n')) : ''}
${limitations.length ? '\n\nLimitations:\n' + escapeHtml(limitations.map(item => '- ' + item).join('\n')) : ''}
`.trim();
    }
    function ruleDetailsFromStock(stock) {
      const names = [
        'pe_vs_industry','pe_vs_historical','roce_over_10','roe_over_10',
        'debt_to_equity_under_0_5','dpr_yoy_positive','pledged_zero',
        'sales_yoy_growth','profit_growth_over_10','stock_cagr_below_profit_growth',
        'promoter_holding_decrease_under_5','rule_12_ssgr','rule_13_cfo_ebitda'
      ];
      return names.map(name => ({ name, status: stock[name] || '' }));
    }

    $('runBtn').addEventListener('click', async () => {
      await api('/api/run', { method: 'POST' });
      await refresh();
    });
    $('refreshBtn').addEventListener('click', refresh);
    $('evalAllBtn').addEventListener('click', () => evaluate(''));
    setInterval(refresh, 4000);
    refresh();
  </script>
</body>
</html>
"""


def main() -> int:
    document_store.ensure_dirs()
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Stock Researcher UI running at http://{HOST}:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
