"""Gemini-backed RAG evaluation for rule-filtered stocks."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import requests

from stock_screener_filter.config import load_env
from stock_screener_filter import document_store


load_env()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AI_DIR = PROJECT_ROOT / "data" / "ai_evaluations"
DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"


def extract_interaction_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    pieces: list[str] = []
    for step in payload.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            if block.get("type") == "text" and block.get("text"):
                pieces.append(block["text"])
    if pieces:
        return "\n".join(pieces)
    candidates = payload.get("candidates") or []
    for candidate in candidates:
        for part in candidate.get("content", {}).get("parts", []):
            if part.get("text"):
                pieces.append(part["text"])
    return "\n".join(pieces)


def extract_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for step in payload.get("steps", []):
        if step.get("type") != "model_output":
            continue
        for block in step.get("content", []):
            for annotation in block.get("annotations", []) or []:
                if annotation.get("type") == "url_citation":
                    citations.append(
                        {
                            "title": annotation.get("title", ""),
                            "url": annotation.get("url", ""),
                        }
                    )
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for citation in citations:
        if citation["url"] and citation["url"] not in seen:
            unique.append(citation)
            seen.add(citation["url"])
    return unique


def json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, flags=re.S)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
    return {
        "ai_score_out_of_50": 0,
        "total_score_out_of_100": 0,
        "verdict": "Could not parse AI response as JSON.",
        "raw_response": text,
    }


def stock_context(stock: dict[str, Any]) -> str:
    return json.dumps(
        {
            "company_name": stock.get("company_name"),
            "company_url": stock.get("company_url"),
            "market_categories": stock.get("market_categories"),
            "first_11_pass_count": stock.get("first_11_pass_count"),
            "excel_rule_pass_count": stock.get("excel_rule_pass_count"),
            "total_rule_pass_count": stock.get("total_rule_pass_count"),
            "rule_pass_percentage": stock.get("rule_pass_percentage"),
            "rule_score_out_of_50": stock.get("rule_score_out_of_50"),
        },
        indent=2,
    )


def rag_context(stock_id: str) -> str:
    queries = [
        "future outlook expected sales profit growth guidance capex demand",
        "initiatives undertaken progress execution management commentary",
        "promises targets guidance delivered delayed missed management",
        "risks headwinds tailwinds margin debt cash flow competition",
    ]
    chunks: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for query in queries:
        for chunk in document_store.search_chunks(stock_id, query, limit=6):
            key = (str(chunk.get("document_sha")), int(chunk.get("chunk_index", 0)))
            if key not in seen:
                chunks.append(chunk)
                seen.add(key)
    lines = []
    for chunk in chunks[:18]:
        lines.append(
            f"[{chunk.get('filename')} chunk {chunk.get('chunk_index')} score={float(chunk.get('score', 0)):.3f}]\n"
            f"{chunk.get('text')}"
        )
    return "\n\n".join(lines) or "No uploaded document chunks available for this stock."


def build_prompt(stock: dict[str, Any]) -> str:
    company = stock.get("company_name", "the company")
    return f"""
You are an equity research analyst. Evaluate {company} using:
1. The quantitative rule score below.
2. Uploaded annual report / concall / quarterly report excerpts from a local RAG store.
3. Google Search grounding for current company news sentiment and industry outlook.

Quantitative context:
{stock_context(stock)}

Relevant uploaded document excerpts:
{rag_context(str(stock["stock_id"]))}

Answer these questions:
- What is the future outlook and expected sales/profit growth?
- What initiatives has management undertaken, and what progress is visible?
- Has management delivered promises made in older uploaded documents?
- What is current news sentiment around the company?
- Is the industry outlook in tailwind, neutral, or headwind territory for the next few years?
- Look for signs of exaggeration: repeated missed guidance, vague promises, accounting/cash-flow mismatch, aggressive capex, deteriorating margins, or narrative changing without delivery.

Return only valid JSON with this exact shape:
{{
  "ai_score_out_of_50": number,
  "total_score_out_of_100": number,
  "verdict": "short final verdict",
  "future_outlook": "short answer",
  "initiatives_progress": "short answer",
  "promise_delivery": "short answer",
  "news_sentiment": "positive|mixed|negative|unknown",
  "industry_outlook": "tailwind|neutral|headwind|unknown",
  "exaggeration_risk": "low|medium|high|unknown",
  "key_reasons": ["reason 1", "reason 2", "reason 3"],
  "open_questions": ["question 1", "question 2"]
}}

Scoring guidance:
- Quantitative rule score is already out of 50 and must be included in total_score_out_of_100.
- Assign ai_score_out_of_50 based on RAG evidence, delivery, outlook, sentiment, and industry position.
- Penalize missing documents, weak evidence, missed commitments, high exaggeration risk, and negative industry/news context.
""".strip()


def gemini_interaction(prompt: str, api_key: str, model: str = DEFAULT_MODEL) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    response = requests.post(
        INTERACTIONS_URL,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "model": model,
            "input": prompt,
            "tools": [{"type": "google_search"}],
        },
        timeout=90,
    )
    response.raise_for_status()
    payload = response.json()
    return extract_interaction_text(payload), extract_citations(payload), payload


def evaluate_stock(stock: dict[str, Any], api_key: str | None = None, model: str | None = None) -> dict[str, Any]:
    load_env()
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    model = model or os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set. Add it to the local .env file and restart the app.")
    text, citations, raw_payload = gemini_interaction(build_prompt(stock), api_key, model=model)
    parsed = json_from_text(text)
    rule_score = float(stock.get("rule_score_out_of_50", 0) or 0)
    ai_score = max(0.0, min(50.0, float(parsed.get("ai_score_out_of_50", 0) or 0)))
    parsed["ai_score_out_of_50"] = round(ai_score, 2)
    parsed["total_score_out_of_100"] = round(rule_score + ai_score, 2)
    parsed["rule_score_out_of_50"] = rule_score
    parsed["company_name"] = stock.get("company_name")
    parsed["company_url"] = stock.get("company_url")
    parsed["stock_id"] = stock.get("stock_id")
    parsed["citations"] = citations
    parsed["raw_model_text"] = text
    parsed["raw_payload"] = raw_payload
    save_evaluation(str(stock["stock_id"]), parsed)
    return parsed


def save_evaluation(stock_id: str, result: dict[str, Any]) -> None:
    AI_DIR.mkdir(parents=True, exist_ok=True)
    (AI_DIR / f"{stock_id}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def load_evaluation(stock_id: str) -> dict[str, Any] | None:
    path = AI_DIR / f"{stock_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
