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


def load_visible_stocks(pass_percentage: float = 90.0) -> list[dict[str, Any]]:
    # Load batch screener stocks for current run, custom single stocks, and document store stocks.
    stocks_by_id: dict[str, dict[str, Any]] = {}
    
    # 1. Load batch screener stocks for current run
    for stock in rules_pipeline.load_all_current_run_stocks(pass_percentage=pass_percentage):
        stock_id = str(stock["stock_id"])
        stocks_by_id[stock_id] = {**stock, "stock_source": "current_run"}

    # 2. Load custom single stocks under custom keys so they don't overwrite current_run batch filter entries
    for stock in rules_pipeline.load_custom_stocks():
        stock_id = str(stock["stock_id"])
        key = f"custom_{stock_id}" if stock_id in stocks_by_id else stock_id
        stocks_by_id[key] = {**stock, "stock_source": "custom_single_stock"}

    # 3. Document store stocks
    for stock in document_store.stocks_with_documents():
        stock_id = str(stock["stock_id"])
        if stock_id not in stocks_by_id and f"custom_{stock_id}" not in stocks_by_id:
            stocks_by_id[stock_id] = dict(stock)

    return sorted(
        stocks_by_id.values(),
        key=lambda stock: (
            0 if stock.get("stock_source") == "current_run" else 1,
            -float(stock.get("rule_pass_percentage") or 0),
            str(stock.get("company_name", "")).lower(),
        ),
    )



class AppState:
    def __init__(self) -> None:
        # HTTP handlers, background jobs, and status polling share this in-memory state.
        self.lock = threading.Lock()
        self.running = False
        self.phase = "idle"
        self.logs: list[str] = []
        self.pass_percentage = 90.0
        self.stocks: list[dict[str, Any]] = load_visible_stocks(self.pass_percentage)
        self.error: str | None = None
        self.error_trace: str | None = None
        self.progress_current = 0
        self.progress_total = 1
        self.current_step = "Idle"

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(message)
            # Bound long crawler runs so status responses and memory use stay predictable.
            self.logs = self.logs[-500:]

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            if not self.running:
                disk_stocks = load_visible_stocks(self.pass_percentage)
                if disk_stocks:
                    self.stocks = disk_stocks

                steps = rules_pipeline.pipeline_status(pass_percentage=self.pass_percentage)
                if self.error and failed_step_is_now_complete(self.error, steps):
                    self.error = None
                    self.error_trace = None
                    self.phase = "idle"
                    self.current_step = "Idle"
            else:
                steps = rules_pipeline.pipeline_status(pass_percentage=self.pass_percentage)
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
                "pass_percentage": self.pass_percentage,
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
        pass_pct = STATE.pass_percentage
        stocks = rules_pipeline.run_full_pipeline(STATE.log, progress=STATE.progress, pass_percentage=pass_pct)
        with STATE.lock:
            STATE.stocks = load_visible_stocks(STATE.pass_percentage)
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
        pass_pct = STATE.pass_percentage
        stocks = rules_pipeline.run_step(step_id, STATE.log, progress=STATE.progress, pass_percentage=pass_pct)
        with STATE.lock:
            if stocks:
                STATE.stocks = load_visible_stocks(STATE.pass_percentage)
            else:
                STATE.stocks = load_visible_stocks(STATE.pass_percentage)
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
        selected = [
            stock
            for stock in stocks
            if (not stock_id or stock.get("stock_id") == stock_id or stock_id == f"custom_{stock.get('stock_id')}")
            and stock.get("stock_source") in {"rule_filtered", "current_run", "custom_single_stock"}
        ]
        if not selected:
            STATE.log("No rule-filtered stocks selected for AI evaluation.")
            with STATE.lock:
                STATE.phase = "AI evaluation skipped"
                STATE.current_step = "No rule-filtered stocks selected"
            return
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
        elif parsed.path == "/api/set-threshold":
            self.set_threshold()
        elif parsed.path == "/api/analyze-ticker":
            self.analyze_ticker()
        elif parsed.path == "/api/refresh-stock":
            self.refresh_stock()
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def analyze_ticker(self) -> None:
        with STATE.lock:
            if STATE.running:
                self.send_json({"ok": False, "error": "A job is already running."}, status=409)
                return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        ticker = payload.get("ticker", "").strip()
        if not ticker:
            self.send_json({"ok": False, "error": "Ticker input cannot be empty."}, status=400)
            return

        def run_single_stock_bg() -> None:
            with STATE.lock:
                STATE.running = True
                STATE.phase = f"analyzing single stock: {ticker}"
                STATE.error = None
                STATE.error_trace = None
                STATE.logs = []
                STATE.current_step = f"Analyzing {ticker}"
            try:
                stock_data = rules_pipeline.analyze_single_stock(ticker, STATE.log, force=False)
                with STATE.lock:
                    STATE.stocks = load_visible_stocks(STATE.pass_percentage)
                    STATE.phase = f"single stock complete: {stock_data.get('company_name', ticker)}"
                    STATE.current_step = "Analysis complete"
            except Exception as exc:
                trace = traceback.format_exc()
                STATE.log(trace)
                with STATE.lock:
                    STATE.error = str(exc)
                    STATE.error_trace = trace
                    STATE.phase = "single stock analysis failed"
                    STATE.current_step = "Analysis failed"
            finally:
                with STATE.lock:
                    STATE.running = False

        thread = threading.Thread(target=run_single_stock_bg, daemon=True)
        thread.start()
        self.send_json({"ok": True})

    def refresh_stock(self) -> None:
        with STATE.lock:
            if STATE.running:
                self.send_json({"ok": False, "error": "A job is already running."}, status=409)
                return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        ticker = payload.get("ticker", "").strip() or payload.get("stock_id", "").strip()
        if not ticker:
            self.send_json({"ok": False, "error": "Ticker input cannot be empty."}, status=400)
            return

        def run_refresh_bg() -> None:
            with STATE.lock:
                STATE.running = True
                STATE.phase = f"refreshing stock: {ticker}"
                STATE.error = None
                STATE.error_trace = None
                STATE.logs = []
                STATE.current_step = f"Re-downloading & analyzing {ticker}"
            try:
                stock_data = rules_pipeline.analyze_single_stock(ticker, STATE.log, force=True)
                with STATE.lock:
                    STATE.stocks = load_visible_stocks(STATE.pass_percentage)
                    STATE.phase = f"refresh complete: {stock_data.get('company_name', ticker)}"
                    STATE.current_step = "Refresh complete"

            except Exception as exc:
                trace = traceback.format_exc()
                STATE.log(trace)
                with STATE.lock:
                    STATE.error = str(exc)
                    STATE.error_trace = trace
                    STATE.phase = "refresh failed"
                    STATE.current_step = "Refresh failed"
            finally:
                with STATE.lock:
                    STATE.running = False

        thread = threading.Thread(target=run_refresh_bg, daemon=True)
        thread.start()
        self.send_json({"ok": True})


    def set_threshold(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        pct = float(payload.get("pass_percentage", 90.0))
        with STATE.lock:
            STATE.pass_percentage = pct
            paths = rules_pipeline.current_paths()
            if (paths.html_analysis_dir / "company_rule_results.csv").is_file():
                rules_pipeline.combine_rule_outputs(paths, pass_percentage=pct)
            STATE.stocks = load_visible_stocks(pct)
        self.send_json({"ok": True, "pass_percentage": pct})


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
            if not stock_id or "file" not in form:
                self.send_json({"ok": False, "error": "stock_id and file are required."}, status=400)
                return
            files = form["file"]
            if not isinstance(files, list):
                files = [files]
            files = [item for item in files if item.filename]
            document_types = [str(item).strip() for item in form.getlist("document_type")]
            document_years = [str(item).strip() for item in form.getlist("document_year")]
            document_quarters = [str(item).strip().upper() for item in form.getlist("document_quarter")]
            if not files:
                self.send_json({"ok": False, "error": "At least one file is required."}, status=400)
                return
            if not (
                len(document_types) == len(files)
                and len(document_years) == len(files)
                and len(document_quarters) == len(files)
            ):
                self.send_json(
                    {"ok": False, "error": "Each uploaded document must have document type, year, and quarter/period."},
                    status=400,
                )
                return
            missing_metadata = [
                index + 1
                for index in range(len(files))
                if not document_types[index] or not document_years[index] or not document_quarters[index]
            ]
            if missing_metadata:
                self.send_json(
                    {
                        "ok": False,
                        "error": f"Missing document metadata for file row(s): {', '.join(str(item) for item in missing_metadata)}.",
                    },
                    status=400,
                )
                return
            stored = []
            for index, item in enumerate(files):
                stored_doc = document_store.save_upload(
                    stock_id,
                    company_name,
                    item.filename,
                    item.file,
                    document_type=document_types[index],
                    document_year=document_years[index],
                    document_quarter=document_quarters[index],
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
            if not stock_id or not question.strip():
                self.send_json({"ok": False, "error": "stock_id and question are required."}, status=400)
                return
            with STATE.lock:
                stocks = list(STATE.stocks) or load_visible_stocks(STATE.pass_percentage)
            stock = next((item for item in stocks if str(item.get("stock_id")) == stock_id), None)
            if not stock:
                self.send_json({"ok": False, "error": f"Unknown stock: {stock_id}"}, status=404)
                return
            answer = ai_evaluator.answer_document_question(
                stock,
                question,
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
        try:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_html(self, html: str) -> None:
        try:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


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
      --bg: #f6f7f8;
      --panel: #ffffff;
      --panel-soft: #fafafa;
      --ink: #171717;
      --muted: #6b7280;
      --muted-strong: #4b5563;
      --line: #e5e7eb;
      --line-strong: #d1d5db;
      --accent: #111827;
      --accent-soft: #f3f4f6;
      --good: #12715b;
      --warn: #9a6700;
      --bad: #b42318;
      --shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
      --radius: 8px;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--ink);
      font-size: 13px;
      line-height: 1.45;
    }
    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 14px 24px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 255, 255, 0.96);
      backdrop-filter: blur(10px);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    h1 {
      font-size: 16px;
      line-height: 1.2;
      margin: 0;
      font-weight: 650;
      letter-spacing: 0;
    }
    main {
      display: grid;
      grid-template-columns: 300px minmax(0, 1fr);
      min-height: calc(100vh - 57px);
    }
    aside {
      padding: 18px 16px;
      border-right: 1px solid var(--line);
      background: #ffffff;
    }
    section {
      min-width: 0;
      padding: 18px 22px 28px;
    }
    button {
      border: 1px solid var(--accent);
      background: var(--accent);
      color: #fff;
      min-height: 32px;
      padding: 0 11px;
      border-radius: 6px;
      cursor: pointer;
      font-weight: 600;
      font-size: 12px;
      letter-spacing: 0;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.05s ease;
    }
    button:hover:not(:disabled) {
      background: #000000;
      border-color: #000000;
    }
    button:active:not(:disabled) {
      transform: translateY(1px);
    }
    button.secondary {
      background: #fff;
      color: var(--ink);
      border-color: var(--line-strong);
    }
    button.secondary:hover:not(:disabled) {
      background: var(--accent-soft);
      border-color: var(--line-strong);
    }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    input[type="password"], input[type="text"], input[type="number"], textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 7px 9px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      outline: none;
    }
    input[type="password"]:focus, input[type="text"]:focus, input[type="number"]:focus, textarea:focus, select:focus {
      border-color: #9ca3af;
      box-shadow: 0 0 0 3px rgba(17, 24, 39, 0.06);
    }
    input[type="password"], input[type="text"], input[type="number"], select {
      height: 32px;
      padding-top: 0;
      padding-bottom: 0;
    }
    input[type="file"] {
      width: 100%;
      color: var(--muted);
      font: inherit;
      font-size: 12px;
    }
    input[type="file"]::file-selector-button {
      height: 30px;
      margin-right: 10px;
      border: 1px solid var(--line-strong);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-weight: 600;
      cursor: pointer;
    }
    input[type="file"]::file-selector-button:hover {
      background: var(--accent-soft);
    }
    textarea {
      min-height: 70px;
      resize: vertical;
      font-family: inherit;
    }
    summary {
      cursor: pointer;
      user-select: none;
      font-weight: 600;
    }
    label {
      display: block;
      font-size: 11px;
      color: var(--muted);
      margin: 16px 0 7px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .toolbar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      align-items: center;
    }
    .step-list {
      display: grid;
      gap: 7px;
      margin-top: 10px;
    }
    .step-row {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: var(--radius);
      padding: 10px;
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
      box-shadow: var(--shadow);
    }
    .step-title {
      font-weight: 650;
      font-size: 13px;
    }
    .step-summary {
      margin-top: 2px;
      color: var(--muted);
      font-size: 12px;
    }
    .step-check {
      color: var(--good);
      font-weight: 800;
      margin-right: 6px;
    }
    .status {
      margin-top: 14px;
      padding: 12px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel);
      font-size: 13px;
      color: var(--muted);
      box-shadow: var(--shadow);
    }
    .status strong {
      color: var(--muted-strong);
      font-weight: 600;
    }
    pre {
      max-height: 260px;
      overflow: auto;
      padding: 12px;
      border: 1px solid #1f2937;
      background: #0b0f16;
      color: #e5e7eb;
      border-radius: var(--radius);
      font-size: 12px;
      line-height: 1.5;
      white-space: pre-wrap;
    }
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      overflow: hidden;
      box-shadow: var(--shadow);
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 12px;
      text-align: left;
      vertical-align: top;
      font-size: 13px;
    }
    tr:last-child td { border-bottom: 0; }
    tbody tr:hover td { background: #fcfcfd; }
    th {
      background: #fafafa;
      color: var(--muted-strong);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    th:nth-child(1) { width: 18%; }
    th:nth-child(2) { width: 14%; }
    th:nth-child(3) { width: 31%; }
    th:nth-child(4) { width: 10%; }
    th:nth-child(5) { width: 27%; }
    .stock-name {
      font-weight: 650;
      font-size: 14px;
      line-height: 1.3;
    }
    .muted { color: var(--muted); }
    .badge {
      display: inline-block;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--accent-soft);
      color: var(--ink);
      border: 1px solid var(--line);
      font-weight: 650;
      font-size: 12px;
    }
    .bad { color: var(--bad); }
    .warn { color: var(--warn); }
    .docs {
      margin-top: 8px;
      font-size: 12px;
      color: var(--muted);
    }
    .verdict-box {
      max-width: 520px;
      line-height: 1.5;
    }
    .verdict-text {
      color: var(--ink);
      font-size: 13px;
      margin-bottom: 8px;
    }
    .reason-list {
      display: grid;
      gap: 5px;
      margin: 0;
      padding: 0;
      list-style: none;
      color: var(--muted);
      font-size: 12px;
    }
    .reason-list li {
      padding-left: 10px;
      border-left: 2px solid var(--line-strong);
    }
    .section-breakdown {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .section-card {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px;
      background: #fff;
    }
    .section-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 5px;
    }
    .section-title {
      font-weight: 650;
      color: var(--ink);
      font-size: 12px;
    }
    .score-row {
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      white-space: nowrap;
    }
    .mini-score {
      display: inline-block;
      padding: 2px 6px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: var(--panel-soft);
      color: var(--muted-strong);
      font-size: 11px;
      font-weight: 600;
    }
    .section-answer {
      color: var(--muted-strong);
      font-size: 12px;
      line-height: 1.45;
    }
    .upload {
      display: grid;
      gap: 8px;
      min-width: 240px;
    }
    .field-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 7px;
    }
    .file-meta {
      display: grid;
      gap: 7px;
    }
    .file-meta-row {
      display: grid;
      grid-template-columns: minmax(120px, 1.2fr) 0.8fr 0.8fr 0.9fr;
      gap: 6px;
      align-items: center;
      padding: 8px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel-soft);
    }
    .file-name {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      font-size: 12px;
      color: var(--muted);
    }
    .qa-box {
      margin-top: 12px;
      display: grid;
      gap: 8px;
      min-width: 260px;
    }
    .qa-answer {
      margin-top: 10px;
      padding: 10px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--panel-soft);
      font-size: 12px;
      white-space: pre-wrap;
    }
    .qa-trace {
      margin-top: 8px;
      white-space: normal;
    }
    .trace-grid {
      display: grid;
      gap: 8px;
      margin-top: 8px;
    }
    .trace-card {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 8px;
      background: #fff;
    }
    .trace-pre {
      max-height: 220px;
      margin: 6px 0 0;
      background: #0b0f16;
      color: #e5e7eb;
      white-space: pre-wrap;
      overflow: auto;
    }
    .progress-shell {
      width: 100%;
      height: 8px;
      border-radius: 999px;
      overflow: hidden;
      background: #eef0f2;
      margin: 10px 0 6px;
    }
    .progress-fill {
      height: 100%;
      width: 0%;
      background: var(--accent);
      transition: width 0.2s ease;
    }
    .inline-progress {
      width: 100%;
      height: 6px;
      margin: 8px 0;
      border-radius: 999px;
      overflow: hidden;
      background: #eef0f2;
    }
    .inline-progress-fill {
      height: 100%;
      width: 100%;
      background: linear-gradient(90deg, #d1d5db, #111827, #d1d5db);
      background-size: 200% 100%;
      animation: progress-slide 1.2s linear infinite;
    }
    @keyframes progress-slide {
      from { background-position: 200% 0; }
      to { background-position: 0 0; }
    }
    .error-box {
      margin-top: 12px;
      border: 1px solid #f3b8b3;
      background: #fff7f7;
      color: var(--bad);
      border-radius: var(--radius);
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
      border-radius: var(--radius);
      background: #fff;
      color: var(--muted);
      box-shadow: var(--shadow);
    }
    .rule-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 6px;
      margin-top: 8px;
    }
    .rule-chip {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px 7px;
      font-size: 11px;
      background: #fff;
    }
    .rule-detail {
      color: var(--muted);
      margin-top: 3px;
      line-height: 1.3;
    }
    .rule-chip.pass { border-color: #c7e4d6; color: var(--good); background: #f3fbf7; }
    .rule-chip.fail { border-color: #f0c3bf; color: var(--bad); background: #fff7f6; }
    .rule-chip.missing { border-color: #ecd69b; color: var(--warn); background: #fffaf0; }
    @media (max-width: 900px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); }
      th { position: static; }
      .file-meta-row { grid-template-columns: 1fr; }
      section { padding: 14px; overflow-x: auto; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Stock Researcher</h1>
      <div class="toolbar">
      <div style="display:flex; align-items:center; gap:6px; background:#f5f7fa; padding:3px 8px; border-radius:6px; border:1px solid var(--line);">
        <input id="tickerInput" type="text" placeholder="Ticker (e.g. TCS)" style="width:130px; padding:4px 8px; font-size:13px; border:1px solid var(--line); border-radius:4px;" />
        <button id="analyzeTickerBtn" class="secondary" style="padding:4px 10px; font-size:13px;">Analyze Ticker</button>
      </div>
      <label style="font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px;">
        Show:
        <select id="viewFilterSelect" style="padding:4px 8px; font-size:13px; border-radius:6px; border:1px solid var(--line);">
          <option value="batch">Batch Screener Stocks</option>
          <option value="single">Single Stocks Only</option>
        </select>
      </label>
      <label id="thresholdLabel" style="font-size:13px; font-weight:600; display:flex; align-items:center; gap:6px;">
        Pass Threshold:
        <select id="thresholdSelect" style="padding:4px 8px; font-size:13px; border-radius:6px; border:1px solid var(--line);">
          <option value="50">>= 50%</option>
          <option value="60">>= 60%</option>
          <option value="75">>= 75%</option>
          <option value="85">>= 85%</option>
          <option value="90">>= 90%</option>
          <option value="100">100%</option>
        </select>
        <span id="thresholdNaBadge" style="display:none; padding:3px 8px; background:#eef2f7; color:var(--muted); border-radius:4px; font-weight:normal;">N/A</span>
      </label>
      <button id="refreshBtn" class="secondary">Refresh</button>
      <button id="runBtn">Run Screener Pipeline</button>
    </div>
  </header>
  <main>
    <aside>
      <div class="status">
        LiteLLM credentials are read from <strong>.env</strong>.
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
        <div><strong>Visible stocks:</strong> <span id="count">0</span></div>
        <div id="error" class="bad"></div>
      </div>
      <div id="errorBox" class="error-box"></div>
      <label>Logs</label>
      <pre id="logs"></pre>
    </aside>
    <section>
      <div id="uploadPrompt" class="prompt">
        Run the Screener pipeline. Once stocks pass the rule filter threshold, upload reports or concalls here for each stock.
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
    let evaluatingStockId = '';
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
      const ruleFilteredCount = latest.stocks.filter(stock => stock.stock_source === 'rule_filtered' || stock.stock_source === 'current_run' || stock.stock_source === 'custom_single_stock' || stock.passes_final_rule_filter).length;
      const documentOnlyCount = latest.stocks.length - ruleFilteredCount;
      $('count').textContent = `${latest.stocks.length} (${ruleFilteredCount} passing, ${documentOnlyCount} other)`;
      $('error').textContent = latest.error || '';
      $('errorBox').style.display = latest.error ? 'block' : 'none';
      $('errorBox').textContent = latest.error_trace || latest.error || '';
      $('logs').textContent = (latest.logs || []).slice(-120).join('\n');
      $('runBtn').disabled = latest.running;
      $('evalAllBtn').disabled = latest.running || !latest.stocks.length;
      if ($('analyzeTickerBtn')) $('analyzeTickerBtn').disabled = latest.running;
      if ($('tickerInput')) $('tickerInput').disabled = latest.running;
      if (!latest.running) {
        evaluatingStockId = '';
      }
      renderSteps(latest.pipeline_steps || []);
      if (!stockFormInteractionActive()) {
        renderStocks(latest.stocks || []);
      }
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

    let activeSingleTicker = '';

    function renderStocks(stocks) {
      const body = $('stocks');
      // Polling rebuilds the table, so capture disclosure state before replacing its rows.
      const openDetails = new Set(
        Array.from(document.querySelectorAll('details.rule-details[open]'))
          .map(detail => detail.dataset.stock)
      );
      const openTraces = new Set(
        Array.from(document.querySelectorAll('details.qa-trace[open]'))
          .map(detail => detail.dataset.stock)
      );
      const viewMode = ($('viewFilterSelect') && $('viewFilterSelect').value) || 'batch';
      if ($('thresholdSelect') && $('thresholdNaBadge')) {
        if (viewMode === 'single') {
          $('thresholdSelect').style.display = 'none';
          $('thresholdNaBadge').style.display = 'inline-block';
        } else {
          $('thresholdSelect').style.display = 'inline-block';
          $('thresholdNaBadge').style.display = 'none';
        }
      }
      const visibleStocks = stocks.filter(stock => {
        if (activeSingleTicker) {
          const search = activeSingleTicker.toLowerCase();
          return stock.company_name.toLowerCase().includes(search) || 
                 (stock.company_url && stock.company_url.toLowerCase().includes(search)) ||
                 stock.stock_id.toLowerCase().includes(search);
        }
        if (viewMode === 'single') {
          return stock.stock_source === 'custom_single_stock' || stock.is_custom_single_stock;
        }
        if (viewMode === 'batch') {
          return stock.stock_source === 'current_run' && Boolean(stock.passes_final_rule_filter);
        }

        return true;
      });


      body.innerHTML = '';
      $('uploadPrompt').textContent = activeSingleTicker
        ? `Showing ${visibleStocks.length} matching result(s) for "${activeSingleTicker}".`
        : (visibleStocks.length
          ? `Displaying ${visibleStocks.length} analyzed stocks. Click 🔄 Refresh to force re-downloading reports & Excel for any stock.`
          : 'Run single stock analysis or Screener pipeline to view stocks.');
      for (const stock of visibleStocks) {
        const tr = document.createElement('tr');
        const docs = stock.documents || [];
        const ai = stock.ai_evaluation || {};
        const rules = stock.rule_details || ruleDetailsFromStock(stock);
        const isRuleFiltered = stock.stock_source === 'rule_filtered' || stock.stock_source === 'current_run' || stock.stock_source === 'custom_single_stock' || stock.total_rule_count > 0;
        const sourceLabel = stock.stock_source === 'custom_single_stock' || stock.is_custom_single_stock
          ? 'Custom Stock'
          : (stock.stock_source === 'current_run' ? 'Current Run' : (stock.stock_source === 'rule_filtered' ? 'Rule filtered' : 'Document library'));
        const reasons = ai.key_reasons || [];
        const isEvaluating = evaluatingStockId && evaluatingStockId === stock.stock_id && latest.running;
        const sectionBreakdown = renderSectionBreakdown(ai.section_analyses || {});
        tr.innerHTML = `
          <td>
            <div style="display: inline-flex; align-items: center; gap: 6px;">
              <span class="stock-name" style="font-weight: 600;">${escapeHtml(stock.company_name)}</span>
              <button class="secondary btn-sm refresh-one" style="padding: 1px 5px; font-size: 0.85rem; line-height: 1; border-radius: 4px; cursor: pointer;" data-stock="${escapeAttr(stock.stock_id)}" data-name="${escapeAttr(stock.company_name)}" title="Re-download HTML, Excel & Reports for ${escapeAttr(stock.company_name)}">🔄</button>
            </div>
            <div class="muted">${escapeHtml(stock.market_categories || '')}</div>

            <div class="muted">${escapeHtml(stock.company_url || '')}</div>
            <div class="muted"><span class="badge" style="font-size: 0.75rem; font-weight: normal;">${sourceLabel}</span></div>
          </td>
          <td>
            ${isRuleFiltered ? `
              <span class="badge">${stock.total_rule_pass_count ?? 0}/${stock.total_rule_count ?? 14}</span>
              <div class="muted">${stock.rule_pass_percentage ?? 0}% passed</div>
              <div class="muted">Rule score: ${stock.rule_score_out_of_50 ?? 0}/50</div>
            ` : `
              <span class="badge">Documents</span>
              <div class="muted">No current rule-filter output for this stock.</div>
            `}
            ${isRuleFiltered ? `<details class="rule-details" data-stock="${escapeAttr(stock.stock_id)}" ${openDetails.has(stock.stock_id) ? 'open' : ''}>
              <summary class="muted">Rule details</summary>
              <div class="rule-grid">
                ${rules.map(rule => `
                  <div class="rule-chip ${escapeAttr(rule.status)}">
                    <strong>${escapeHtml(shortRuleName(rule.name))}: ${escapeHtml(rule.status)}</strong>
                    <div class="rule-detail">${escapeHtml(rule.detail || '')}</div>
                  </div>
                `).join('')}
              </div>
            </details>` : ''}
          </td>
          <td>
            <form class="upload" data-stock="${stock.stock_id}" data-name="${escapeAttr(stock.company_name)}">
              <input type="file" name="file" multiple accept=".pdf,.txt,.md,.html,.htm,.csv,.json">
              <div class="file-meta"></div>
              <button type="submit" class="secondary">Upload</button>
            </form>
            <div class="docs">${docs.length} document(s), ${docs.reduce((a,d)=>a+(d.chunk_count||0),0)} chunks</div>
            <form class="qa-box" data-stock="${stock.stock_id}">
              <textarea name="question" placeholder="Ask this stock's uploaded documents"></textarea>
              <button type="submit" class="secondary">Ask Documents</button>
            </form>
            <div class="qa-answer" id="qa-${escapeAttr(stock.stock_id)}" style="${qaAnswers[stock.stock_id] ? '' : 'display:none;'}">${renderQaAnswer(qaAnswers[stock.stock_id], stock.stock_id, openTraces.has(stock.stock_id))}</div>
          </td>
          <td>
            ${isRuleFiltered ? `
              ${isEvaluating ? `
                <span class="badge">Evaluating</span>
                <div class="inline-progress"><div class="inline-progress-fill"></div></div>
                <div class="muted">${escapeHtml(latest.current_step || 'Running AI evaluation')}</div>
              ` : `
                ${ai.total_score_out_of_100 !== undefined ? `<span class="badge" title="Total Combined Score (Rule Score + AI Score)">Total: ${ai.total_score_out_of_100}/100</span>` : '<span class="muted">Not run</span>'}
                <div class="muted">AI score: ${ai.ai_score_out_of_50 ?? '-'}/50</div>
                <button class="secondary eval-one" data-stock="${stock.stock_id}">Evaluate</button>
              `}
            ` : '<span class="muted">Document Q&A only</span>'}
          </td>
          <td>
            ${ai.verdict ? `
              <div class="verdict-box">
                <div class="verdict-text">${escapeHtml(ai.verdict)}</div>
                ${reasons.length ? `
                  <ul class="reason-list">
                    ${reasons.map(reason => `<li>${escapeHtml(reason)}</li>`).join('')}
                  </ul>
                ` : ''}
                ${sectionBreakdown}
              </div>
            ` : '<span class="muted">No verdict yet</span>'}
          </td>
        `;
        body.appendChild(tr);
      }
      document.querySelectorAll('form.upload').forEach(form => {
        form.addEventListener('submit', uploadForm);
        form.querySelector('input[type=file]').addEventListener('change', () => renderFileMetadata(form));
      });
      document.querySelectorAll('form.qa-box').forEach(form => {
        form.addEventListener('submit', askForm);
      });
      document.querySelectorAll('.eval-one').forEach(btn => {
        btn.addEventListener('click', () => evaluate(btn.dataset.stock));
      });
      document.querySelectorAll('.refresh-one').forEach(btn => {
        btn.addEventListener('click', () => refreshStock(btn.dataset.stock, btn.dataset.name));
      });
    }

    async function refreshStock(stockId, companyName) {
      if (latest && latest.running) {
        alert("A job is currently running. Please wait for it to complete.");
        return;
      }
      if (!confirm(`Re-download latest HTML profile, Excel export, and BSE reports for ${companyName || stockId}?`)) {
        return;
      }
      try {
        const res = await api('/api/refresh-stock', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker: stockId })
        });
        if (!res.ok) {
          alert("Error initiating refresh: " + (res.error || "Unknown error"));
        } else {
          await refresh();
        }
      } catch (err) {
        alert("Network error while starting stock refresh.");
      }
    }


    function stockFormInteractionActive() {
      // Do not let background polling erase typed questions, selected files, or open traces.
      const active = document.activeElement;
      if (active && active.closest && active.closest('form.upload, form.qa-box')) return true;
      if (document.querySelector('details.qa-trace[open]')) return true;
      return Array.from(document.querySelectorAll('form.upload input[type=file]'))
        .some(input => input.files && input.files.length);
    }

    async function uploadForm(event) {
      event.preventDefault();
      const form = event.currentTarget;
      const input = form.querySelector('input[type=file]');
      if (!input.files.length) return;
      renderFileMetadata(form);
      const rows = Array.from(form.querySelectorAll('.file-meta-row'));
      if (rows.length !== input.files.length) {
        alert('Metadata rows did not match selected files. Please select the files again.');
        return;
      }
      const data = new FormData();
      data.append('stock_id', form.dataset.stock);
      data.append('company_name', form.dataset.name);
      for (let index = 0; index < input.files.length; index += 1) {
        const row = rows[index];
        const documentType = row.querySelector('[data-field="document_type"]').value;
        const documentYear = row.querySelector('[data-field="document_year"]').value;
        const documentQuarter = row.querySelector('[data-field="document_quarter"]').value;
        if (!documentType || !documentYear || !documentQuarter) {
          alert('Please select document type, year, and quarter/period for every file.');
          return;
        }
        data.append('file', input.files[index]);
        data.append('document_type', documentType);
        data.append('document_year', documentYear);
        data.append('document_quarter', documentQuarter);
      }
      await api('/api/upload', { method: 'POST', body: data });
      input.value = '';
      form.querySelector('.file-meta').innerHTML = '';
      await refresh();
    }

    function renderFileMetadata(form) {
      const input = form.querySelector('input[type=file]');
      const container = form.querySelector('.file-meta');
      const existing = Array.from(container.querySelectorAll('.file-meta-row')).map(row => ({
        name: row.dataset.filename || '',
        type: row.querySelector('[data-field="document_type"]')?.value || '',
        year: row.querySelector('[data-field="document_year"]')?.value || '',
        quarter: row.querySelector('[data-field="document_quarter"]')?.value || ''
      }));
      container.innerHTML = '';
      Array.from(input.files).forEach((file, index) => {
        const previous = existing[index] && existing[index].name === file.name ? existing[index] : {};
        const row = document.createElement('div');
        row.className = 'file-meta-row';
        row.dataset.filename = file.name;
        row.innerHTML = `
          <div class="file-name" title="${escapeAttr(file.name)}">${escapeHtml(file.name)}</div>
          <select data-field="document_type" required>
            <option value="">Type</option>
            <option value="report" ${previous.type === 'report' ? 'selected' : ''}>Report</option>
            <option value="concall" ${previous.type === 'concall' ? 'selected' : ''}>Concall</option>
          </select>
          <select data-field="document_year" required>
            <option value="">Year</option>
            ${yearOptions(previous.year || '')}
          </select>
          <select data-field="document_quarter" required>
            <option value="">Period</option>
            ${periodOptions(previous.quarter || '')}
          </select>
        `;
        container.appendChild(row);
      });
    }

    function yearOptions(selected) {
      const currentYear = new Date().getFullYear();
      let options = '';
      for (let year = currentYear + 1; year >= currentYear - 15; year -= 1) {
        const value = `FY${year}`;
        options += `<option value="${value}" ${value === String(selected) ? 'selected' : ''}>${value}</option>`;
      }
      return options;
    }

    function periodOptions(selected) {
      return ['FY', 'Q1', 'Q2', 'Q3', 'Q4']
        .map(period => `<option value="${period}" ${period === selected ? 'selected' : ''}>${period}</option>`)
        .join('');
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
            question
          })
        });
        qaAnswers[stockId] = response.answer;
      } catch (error) {
        qaAnswers[stockId] = { answer: `Error: ${error.message}`, citations: [], limitations: [] };
      }
      renderStocks(latest.stocks || []);
    }

    function evaluate(stockId) {
      evaluatingStockId = stockId || '__all__';
      latest.running = true;
      latest.phase = 'starting AI/RAG evaluation';
      latest.current_step = stockId ? 'Starting AI evaluation' : 'Starting AI evaluation for all stocks';
      renderStocks(latest.stocks || []);
      const payload = {
        stock_id: stockId || ''
      };
      api('/api/evaluate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      }).then(() => {
        refresh();
      }).catch(error => {
        evaluatingStockId = '';
        latest.running = false;
        alert(`Could not start AI evaluation: ${error.message}`);
        refresh();
      });
      setTimeout(refresh, 300);
    }

    function escapeHtml(value) {
      return String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[ch]));
    }
    function escapeAttr(value) { return escapeHtml(value).replace(/"/g, '&quot;'); }
    function shortRuleName(name) {
      const names = {
        pe_vs_industry: 'PE/industry',
        pe_vs_historical: 'PE/history',
        roce_over_15: 'ROCE',
        roe_over_15: 'ROE',
        debt_to_equity_under_0_5: 'D/E',
        pledged_zero: 'Pledged',
        sales_yoy_growth: 'Sales YoY',
        profit_growth_over_10: 'Profit growth',
        stock_cagr_below_profit_growth: 'CAGR < profit',
        promoter_holding_decrease_under_5: 'Promoter',
        peg_ratio_under_1_5: 'PEG <= 1.5',
        revenue_quality_guard: 'Revenue Quality',
        rule_12_ssgr: 'SSGR',
        rule_13_cfo_ebitda: 'CFO/EBITDA'
      };
      return names[name] || name;
    }
    function sectionTitle(sectionId) {
      const names = {
        future_outlook: 'Future Outlook',
        initiatives_progress: 'Initiatives Progress',
        promise_delivery: 'Promise Delivery',
        exaggeration_risk: 'Exaggeration Risk',
        news_sentiment: 'News Sentiment',
        industry_outlook: 'Industry Outlook'
      };
      return names[sectionId] || sectionId;
    }
    function renderSectionBreakdown(sections) {
      const order = [
        'future_outlook',
        'initiatives_progress',
        'promise_delivery',
        'exaggeration_risk',
        'news_sentiment',
        'industry_outlook'
      ];
      const cards = order
        .filter(sectionId => sections[sectionId])
        .map(sectionId => {
          const section = sections[sectionId] || {};
          return `
            <div class="section-card">
              <div class="section-head">
                <span class="section-title">${escapeHtml(sectionTitle(sectionId))}</span>
                <span class="score-row">
                  <span class="mini-score">Conf ${formatScore(section.confidence_score)}/100</span>
                  <span class="mini-score">Risk ${formatScore(section.risk_score)}/100</span>
                </span>
              </div>
              <div class="section-answer">${escapeHtml(section.answer || '')}</div>
            </div>
          `;
        })
        .join('');
      return cards ? `<div class="section-breakdown">${cards}</div>` : '';
    }
    function formatScore(value) {
      if (value === undefined || value === null || value === '') return '-';
      const number = Number(value);
      if (Number.isNaN(number)) return '-';
      return Number.isInteger(number) ? String(number) : number.toFixed(1);
    }
    function renderQaAnswer(result, stockId, traceOpen) {
      if (!result) return '';
      if (result.loading) return escapeHtml(result.answer || '');
      const citations = (result.citations || []).map(citation => {
        const pageStart = citation.page_start;
        const pageEnd = citation.page_end || pageStart;
        const page = pageStart && pageEnd && pageStart !== pageEnd
          ? `pp. ${pageStart}-${pageEnd}`
          : (pageStart ? `p. ${pageStart}` : 'page unknown');
        const meta = [citation.document_year, citation.document_quarter].filter(Boolean).join(' ');
        const type = citation.document_type ? citation.document_type.replaceAll('_', ' ') : '';
        return `${citation.source_id}: ${citation.document_name} (${page}${meta ? ', ' + meta : ''}${type ? ', ' + type : ''})`;
      });
      const limitations = result.limitations || [];
      return `
<div>${escapeHtml(result.answer || '')}</div>
${citations.length ? `<div><strong>Citations:</strong>\n${escapeHtml(citations.map(item => '- ' + item).join('\n'))}</div>` : ''}
${limitations.length ? `<div><strong>Limitations:</strong>\n${escapeHtml(limitations.map(item => '- ' + item).join('\n'))}</div>` : ''}
${renderQaTrace(result.trace, stockId, traceOpen)}
`.trim();
    }
    function renderQaTrace(trace, stockId, traceOpen) {
      if (!trace) return '';
      const chunks = trace.retrieved_chunks || [];
      const rawChunks = trace.raw_retrieved_chunks || [];
      const rejectedChunks = trace.rejected_chunks || [];
      const cited = trace.cited_source_ids || [];
      const ignored = trace.ignored_source_ids || [];
      return `
<details class="qa-trace" data-stock="${escapeAttr(stockId)}" ${traceOpen ? 'open' : ''}>
  <summary class="muted">Trace</summary>
  <div class="trace-grid">
    <div class="trace-card">
      <strong>Status:</strong> ${escapeHtml(trace.status || '')}<br>
      <strong>Trace file:</strong> ${escapeHtml(trace.trace_path || '')}<br>
      <strong>Model:</strong> ${escapeHtml(trace.model || '')}<br>
      <strong>Similarity threshold:</strong> ${escapeHtml(trace.similarity_threshold ?? '')}<br>
      <strong>Raw retrieved chunks:</strong> ${escapeHtml(trace.raw_retrieved_chunk_count ?? rawChunks.length)}<br>
      <strong>Accepted chunks:</strong> ${escapeHtml(trace.retrieved_chunk_count ?? chunks.length)}<br>
      <strong>Rejected chunks:</strong> ${escapeHtml(trace.rejected_chunk_count ?? rejectedChunks.length)}<br>
      <strong>Fallback used:</strong> ${escapeHtml(trace.used_filter_fallback ? 'yes' : 'no')}<br>
      <strong>Cited source IDs:</strong> ${escapeHtml(cited.length ? cited.join(', ') : 'none')}<br>
      <strong>Ignored source IDs:</strong> ${escapeHtml(ignored.length ? ignored.join(', ') : 'none')}
    </div>
    <div class="trace-card">
      <strong>Inferred Filters</strong>
      <pre class="trace-pre">${escapeHtml(JSON.stringify(trace.inferred_filters || {}, null, 2))}</pre>
    </div>
    <div class="trace-card">
      <strong>Accepted Chunks Sent To LiteLLM</strong>
      ${chunks.length ? chunks.map(renderTraceChunk).join('') : '<div class="muted">No chunks retrieved.</div>'}
    </div>
    <div class="trace-card">
      <strong>Rejected Chunks Below Threshold</strong>
      ${rejectedChunks.length ? rejectedChunks.map(renderTraceChunk).join('') : '<div class="muted">No chunks rejected.</div>'}
    </div>
    <div class="trace-card">
      <strong>Prompt Sent To LiteLLM</strong>
      <pre class="trace-pre">${escapeHtml(trace.prompt || '')}</pre>
    </div>
    <div class="trace-card">
      <strong>Parsed Model Response</strong>
      <pre class="trace-pre">${escapeHtml(JSON.stringify(trace.parsed_response || {}, null, 2))}</pre>
    </div>
    <div class="trace-card">
      <strong>Raw Model Text</strong>
      <pre class="trace-pre">${escapeHtml(trace.raw_model_text || '')}</pre>
    </div>
    <div class="trace-card">
      <strong>Raw API Payload</strong>
      <pre class="trace-pre">${escapeHtml(JSON.stringify(trace.raw_payload || {}, null, 2))}</pre>
    </div>
  </div>
</details>
`.trim();
    }
    function renderTraceChunk(chunk) {
      const pageStart = chunk.page_start;
      const pageEnd = chunk.page_end || pageStart;
      const page = pageStart && pageEnd && pageStart !== pageEnd
        ? `pp. ${pageStart}-${pageEnd}`
        : (pageStart ? `p. ${pageStart}` : 'page unknown');
      const meta = [chunk.document_year, chunk.document_quarter, chunk.document_type].filter(Boolean).join(', ');
      return `
<div class="trace-card">
  <strong>${escapeHtml(chunk.source_id || '')}</strong>
  ${escapeHtml(chunk.document_name || '')}
  <div class="muted">${escapeHtml(page)}${meta ? ' | ' + escapeHtml(meta) : ''} | chunk ${escapeHtml(chunk.chunk_index || '')} | score ${escapeHtml(chunk.similarity_score ?? '')}</div>
  <pre class="trace-pre">${escapeHtml(chunk.text_excerpt || '')}</pre>
</div>
`.trim();
    }
    function ruleDetailsFromStock(stock) {
      const names = [
        'pe_vs_industry','pe_vs_historical','roce_over_15','roe_over_15',
        'debt_to_equity_under_0_5','pledged_zero',
        'sales_yoy_growth','profit_growth_over_10','stock_cagr_below_profit_growth',
        'promoter_holding_decrease_under_5','peg_ratio_under_1_5','revenue_quality_guard','rule_12_ssgr','rule_13_cfo_ebitda'
      ];
      return names.map(name => ({ name, status: stock[name] || '' }));
    }

    async function handleAnalyzeTicker() {
      const input = $('tickerInput');
      const val = input.value.trim();
      if (!val) {
        alert('Please enter a stock ticker (e.g. TCS)');
        return;
      }
      activeSingleTicker = val;
      if ($('viewFilterSelect')) $('viewFilterSelect').value = 'single';
      try {
        await api('/api/analyze-ticker', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ ticker: val })
        });
        await refresh();
      } catch (err) {
        alert(`Failed to trigger ticker analysis: ${err.message}`);
      }
    }
    $('analyzeTickerBtn').addEventListener('click', handleAnalyzeTicker);
    $('tickerInput').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') handleAnalyzeTicker();
    });
    $('tickerInput').addEventListener('input', (e) => {
      activeSingleTicker = e.target.value.trim();
      renderStocks(latest.stocks || []);
    });
    $('runBtn').addEventListener('click', async () => {
      await api('/api/run', { method: 'POST' });
      await refresh();
    });
    $('refreshBtn').addEventListener('click', refresh);
    $('evalAllBtn').addEventListener('click', () => evaluate(''));
    $('viewFilterSelect').addEventListener('change', () => {
      if ($('viewFilterSelect').value !== 'single') {
        activeSingleTicker = '';
        if ($('tickerInput')) $('tickerInput').value = '';
      }
      renderStocks(latest.stocks || []);
    });

    $('thresholdSelect').addEventListener('change', async (e) => {
      const val = parseFloat(e.target.value);
      await api('/api/set-threshold', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pass_percentage: val })
      });
      await refresh();
    });
    setInterval(refresh, 4000);
    refresh().then(() => {
      if (latest.pass_percentage) {
        $('thresholdSelect').value = String(latest.pass_percentage);
      }
    });
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
