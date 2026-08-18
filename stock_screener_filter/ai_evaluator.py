"""Question-by-question LangGraph RAG evaluation for rule-filtered stocks."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal, TypedDict

import requests
from pydantic import BaseModel, Field, ValidationError

from stock_screener_filter.config import load_env
from stock_screener_filter import document_store


load_env()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AI_DIR = PROJECT_ROOT / "data" / "ai_evaluations"
QA_RUNS_DIR = PROJECT_ROOT / "data" / "qa_runs"
SEARCH_CACHE_DIR = PROJECT_ROOT / "data" / "search_cache"
DEFAULT_MODEL = "gpt-5.6-luna"
DEFAULT_BASE_URL = "http://132.145.30.2:4000/v1"
DEFAULT_API_KEY = "sk-RP-0EUNkb5NE1ea3ZmO_Pw"
DEFAULT_MIN_RAG_SIMILARITY_SCORE = 0.30


class SectionAnalysis(BaseModel):
    question_id: str
    answer: str
    evidence_summary: list[str]
    confidence_score: float = Field(ge=0, le=100)
    risk_score: float = Field(ge=0, le=100)
    open_questions: list[str]


class StockEvaluation(BaseModel):
    ai_score_out_of_50: float = Field(ge=0, le=50)
    total_score_out_of_100: float = Field(ge=0, le=100)
    verdict: str
    future_outlook: str
    initiatives_progress: str
    promise_delivery: str
    news_sentiment_score: float = Field(ge=0, le=100)
    industry_outlook_score: float = Field(ge=0, le=100)
    exaggeration_risk_score: float = Field(ge=0, le=100)
    key_reasons: list[str]
    open_questions: list[str]


class EvaluationState(TypedDict, total=False):
    stock: dict[str, Any]
    stock_id: str
    section_analyses: dict[str, dict[str, Any]]
    section_citations: dict[str, list[dict[str, str]]]
    section_raw_model_text: dict[str, str]
    section_raw_payloads: dict[str, dict[str, Any]]
    section_chunk_refs: dict[str, list[dict[str, Any]]]
    section_search_results: dict[str, list[dict[str, Any]]]
    final_prompt: str
    final_raw_model_text: str
    final_raw_payload: dict[str, Any]
    parsed: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class QuestionSpec:
    question_id: str
    title: str
    question: str
    retrieval_queries: tuple[str, ...]
    guidance: str
    search_kind: Literal["none", "news", "industry"] = "none"


SECTION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "question_id": {"type": "string"},
        "answer": {"type": "string"},
        "evidence_summary": {"type": "array", "items": {"type": "string"}},
        "confidence_score": {"type": "number"},
        "risk_score": {"type": "number"},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "question_id",
        "answer",
        "evidence_summary",
        "confidence_score",
        "risk_score",
        "open_questions",
    ],
}


EVALUATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ai_score_out_of_50": {"type": "number"},
        "total_score_out_of_100": {"type": "number"},
        "verdict": {"type": "string"},
        "future_outlook": {"type": "string"},
        "initiatives_progress": {"type": "string"},
        "promise_delivery": {"type": "string"},
        "news_sentiment_score": {"type": "number"},
        "industry_outlook_score": {"type": "number"},
        "exaggeration_risk_score": {"type": "number"},
        "key_reasons": {"type": "array", "items": {"type": "string"}},
        "open_questions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "ai_score_out_of_50",
        "total_score_out_of_100",
        "verdict",
        "future_outlook",
        "initiatives_progress",
        "promise_delivery",
        "news_sentiment_score",
        "industry_outlook_score",
        "exaggeration_risk_score",
        "key_reasons",
        "open_questions",
    ],
}


DOCUMENT_QA_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citation_source_ids": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["answer", "citation_source_ids", "limitations"],
}


DOCUMENT_FILTER_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "apply_metadata_filter": {"type": "boolean"},
        "document_years": {"type": "array", "items": {"type": "string"}},
        "document_quarters": {"type": "array", "items": {"type": "string"}},
        "document_types": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["apply_metadata_filter", "document_years", "document_quarters", "document_types", "reason"],
}


QUESTION_SPECS: tuple[QuestionSpec, ...] = (
    QuestionSpec(
        question_id="future_outlook",
        title="Future Outlook",
        question="What is the future sales, profit, margin, demand, capacity, and growth outlook for the company?",
        retrieval_queries=(
            "future outlook sales growth profit growth margin guidance demand capacity expansion capex",
            "management expects revenue growth volume growth demand outlook margin expansion guidance",
        ),
        guidance="Focus on forward-looking management commentary from uploaded reports and concalls. Separate explicit guidance from vague optimism.",
    ),
    QuestionSpec(
        question_id="initiatives_progress",
        title="Initiatives And Progress",
        question="What initiatives has management undertaken, and what progress is visible?",
        retrieval_queries=(
            "initiatives undertaken progress capex expansion project commissioned new products efficiency program",
            "strategic initiatives execution progress plant capacity utilization expansion update",
        ),
        guidance="Identify concrete initiatives, milestones, delays, and evidence of execution progress.",
    ),
    QuestionSpec(
        question_id="promise_delivery",
        title="Promise Delivery",
        question="Has management delivered promises made in older uploaded documents?",
        retrieval_queries=(
            "promised target guidance expected by FY achieved delivered completed delayed missed",
            "management said will expect target guidance capex commissioning delivery progress",
            "older annual report concall guidance achieved missed delayed completed",
        ),
        guidance="Compare older commitments against later progress. Be strict when documents do not provide enough historical evidence.",
    ),
    QuestionSpec(
        question_id="exaggeration_risk",
        title="Exaggeration Risk",
        question="Is management exaggerating, overpromising, or hiding risk?",
        retrieval_queries=(
            "risk guidance missed delayed margin pressure cash flow debt receivables inventory aggressive capex",
            "vague promises repeated guidance deteriorating margins accounting cash flow mismatch competition",
        ),
        guidance="Look for repeated missed guidance, vague language, weak cash conversion, margin deterioration, aggressive capex, or narrative changes without delivery.",
    ),
    QuestionSpec(
        question_id="news_sentiment",
        title="News Sentiment",
        question="What is the current news sentiment around the company?",
        retrieval_queries=(),
        guidance="Use DuckDuckGo news/web search snippets. Classify recent company-specific sentiment as positive, mixed, negative, or unknown, and cite the key reasons.",
        search_kind="news",
    ),
    QuestionSpec(
        question_id="industry_outlook",
        title="Industry Outlook",
        question="Is the company's industry facing tailwinds, neutral conditions, or headwinds over the next few years?",
        retrieval_queries=(),
        guidance="Use DuckDuckGo web search snippets. Focus on the industry, demand cycle, regulation, commodity/input costs, exports, and competitive conditions.",
        search_kind="industry",
    ),
)


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
    return {"raw_response": text}


def extract_generate_content_text(payload: dict[str, Any]) -> str:
    pieces: list[str] = []
    for candidate in payload.get("candidates", []) or []:
        for part in candidate.get("content", {}).get("parts", []) or []:
            if part.get("text"):
                pieces.append(part["text"])
    return "\n".join(pieces)


def extract_generate_content_citations(payload: dict[str, Any]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    for candidate in payload.get("candidates", []) or []:
        metadata = candidate.get("groundingMetadata") or candidate.get("grounding_metadata") or {}
        for chunk in metadata.get("groundingChunks", []) or metadata.get("grounding_chunks", []) or []:
            web = chunk.get("web") or {}
            url = web.get("uri") or web.get("url") or ""
            title = web.get("title") or ""
            if url:
                citations.append({"title": title, "url": url})

    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for citation in citations:
        if citation["url"] not in seen:
            unique.append(citation)
            seen.add(citation["url"])
    return unique


def safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "item"


def search_cache_path(stock_id: str, question_id: str) -> Path:
    SEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # News and industry results are reusable within a day but refresh automatically tomorrow.
    today = date.today().strftime("%Y%m%d")
    return SEARCH_CACHE_DIR / f"{safe_name(stock_id)}_{safe_name(question_id)}_{today}.json"


def normalize_search_result(result: dict[str, Any], query: str, source_type: str) -> dict[str, Any]:
    url = result.get("url") or result.get("href") or ""
    return {
        "title": result.get("title", ""),
        "url": url,
        "snippet": result.get("body", ""),
        "source": result.get("source", ""),
        "date": result.get("date", ""),
        "query": query,
        "source_type": source_type,
    }


def ddg_search(query: str, *, news: bool, max_results: int) -> list[dict[str, Any]]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise RuntimeError("DuckDuckGo search needs ddgs. Run: python -m pip install -r requirements.txt") from exc

    timeout = int(os.environ.get("DDG_SEARCH_TIMEOUT_SECONDS", "15"))
    with DDGS(timeout=timeout) as ddgs:
        try:
            if news:
                results = ddgs.news(query, max_results=max_results)
                source_type = "duckduckgo_news"
            else:
                results = ddgs.text(query, max_results=max_results)
                source_type = "duckduckgo_text"
        except Exception:
            return []
    return [normalize_search_result(result, query, source_type) for result in results]


def search_queries(stock: dict[str, Any], spec: QuestionSpec) -> list[str]:
    company = str(stock.get("company_name") or "").strip()
    categories = str(stock.get("market_categories") or "").strip()
    if spec.search_kind == "news":
        return [
            f"{company} latest stock news results orders expansion",
            f"{company} share price news management outlook",
        ]
    if spec.search_kind == "industry":
        industry_context = categories or company
        return [
            f"{industry_context} industry outlook India demand growth tailwinds headwinds",
            f"{company} industry outlook India next few years",
        ]
    return []


def external_search_results(stock: dict[str, Any], spec: QuestionSpec) -> list[dict[str, Any]]:
    stock_id = str(stock["stock_id"])
    cache_path = search_cache_path(stock_id, spec.question_id)
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding="utf-8"))

    per_query = int(os.environ.get("DDG_RESULTS_PER_QUERY", "5"))
    results: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for query in search_queries(stock, spec):
        for result in ddg_search(query, news=spec.search_kind == "news", max_results=per_query):
            url = str(result.get("url", ""))
            if url and url not in seen_urls:
                results.append(result)
                seen_urls.add(url)

    cache_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def format_search_context(results: list[dict[str, Any]], max_results: int = 10) -> str:
    if not results:
        return "No external search results were available for this question."
    lines = []
    for index, result in enumerate(results[:max_results], start=1):
        lines.append(
            f"[Source {index}: {result.get('title', '')}]\n"
            f"URL: {result.get('url', '')}\n"
            f"Source: {result.get('source', '')}\n"
            f"Date: {result.get('date', '')}\n"
            f"Search query: {result.get('query', '')}\n"
            f"Snippet: {result.get('snippet', '')}"
        )
    return "\n\n".join(lines)


def search_citations(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    citations: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for result in results:
        url = str(result.get("url", ""))
        if not url or url in seen_urls:
            continue
        citations.append({"title": str(result.get("title", "")), "url": url})
        seen_urls.add(url)
    return citations


def retrieve_chunks(stock_id: str, queries: list[str] | tuple[str, ...], limit_per_query: int = 5) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    # Different semantic queries often retrieve the same physical chunk.
    seen: set[tuple[str, int]] = set()
    for query in queries:
        for chunk in document_store.search_chunks(stock_id, query, limit=limit_per_query):
            key = (str(chunk.get("document_sha")), int(chunk.get("chunk_index", 0)))
            if key not in seen:
                chunks.append(chunk)
                seen.add(key)
    return chunks


def chunk_refs(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for chunk in chunks:
        refs.append(
            {
                "filename": chunk.get("filename"),
                "document_sha": chunk.get("document_sha"),
                "chunk_index": chunk.get("chunk_index"),
                "page_start": chunk.get("page_start"),
                "page_end": chunk.get("page_end"),
                "score": round(float(chunk.get("score", 0) or 0), 3),
            }
        )
    return refs


def page_label(chunk: dict[str, Any]) -> str:
    page_start = int(chunk.get("page_start") or 0)
    page_end = int(chunk.get("page_end") or page_start or 0)
    if page_start and page_end and page_start != page_end:
        return f"pages {page_start}-{page_end}"
    if page_start:
        return f"page {page_start}"
    return "page unknown"


def format_rag_context(chunks: list[dict[str, Any]], max_chunks: int = 10) -> str:
    lines = []
    for chunk in chunks[:max_chunks]:
        lines.append(
            f"[{chunk.get('filename')} {page_label(chunk)} chunk {chunk.get('chunk_index')} score={float(chunk.get('score', 0)):.3f}]\n"
            f"{chunk.get('text')}"
        )
    return "\n\n".join(lines) or "No uploaded document chunks were retrieved for this question."


def format_document_qa_context(chunks: list[dict[str, Any]]) -> tuple[str, dict[str, dict[str, Any]]]:
    source_map: dict[str, dict[str, Any]] = {}
    lines: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        source_id = f"S{index}"
        source_map[source_id] = chunk
        lines.append(
            f"[{source_id}]\n"
            f"Document: {chunk.get('filename', '')}\n"
            f"Type: {chunk.get('document_type', '')}\n"
            f"Pages: {page_label(chunk)}\n"
            f"Year: {chunk.get('document_year', '')}\n"
            f"Quarter: {chunk.get('document_quarter', '')}\n"
            f"Chunk: {chunk.get('chunk_index', '')}\n"
            f"Similarity score: {float(chunk.get('score', 0) or 0):.3f}\n"
            f"Excerpt:\n{chunk.get('text', '')}"
        )
    return "\n\n".join(lines), source_map


def build_document_qa_prompt(
    stock: dict[str, Any],
    question: str,
    context: str,
    inferred_filters: dict[str, str] | None = None,
) -> str:
    filters = inferred_filters or {}
    return f"""
You are answering a user's question about {stock.get('company_name', 'the company')} using only uploaded document excerpts.

Question:
{question}

Metadata inferred from the question:
{json.dumps(filters or {"document_year": "not specified", "document_quarter": "not specified"}, indent=2)}

Evidence excerpts:
{context}

Instructions:
- Answer only from the excerpts above.
- If the excerpts do not answer the question, say that the uploaded documents do not provide enough evidence.
- citation_source_ids must contain only source IDs like S1, S2 from the excerpts that directly support the answer.
- Do not cite a source ID unless it directly supports the answer.
- Keep the answer practical and concise.

Return JSON matching the schema.
""".strip()


def build_section_prompt(stock: dict[str, Any], spec: QuestionSpec, context: str) -> str:
    company = stock.get("company_name", "the company")
    company_context = json.dumps(
        {
            "company_name": stock.get("company_name"),
            "company_url": stock.get("company_url"),
            "market_categories": stock.get("market_categories"),
        },
        indent=2,
    )
    if spec.search_kind != "none":
        evidence_instruction = "Use only the external search snippets below for current web evidence. Cite URLs from the provided sources in the evidence summary when useful. Do not invent facts beyond these snippets."
    else:
        evidence_instruction = "Use only the uploaded document excerpts below for evidence. If evidence is weak or missing, say so clearly."
    return f"""
You are an equity research analyst evaluating {company}.

Company context:
{company_context}

Step: {spec.title}
Question: {spec.question}
Guidance: {spec.guidance}
Evidence instruction: {evidence_instruction}

Relevant uploaded document excerpts:
{context}

Return concise JSON matching the response schema:
- question_id must be "{spec.question_id}".
- answer should directly answer only this step's question.
- evidence_summary should list the strongest 2-5 evidence points.
- confidence_score is 0-100, where 0 means no reliable evidence, 50 means partial/mixed evidence, and 100 means strong reliable evidence.
- risk_score is 0-100, where 0 means no meaningful risk signal, 50 means moderate/mixed risk, and 100 means severe risk for this step.
- open_questions should list missing evidence or follow-up checks.
""".strip()


def build_final_prompt(stock: dict[str, Any], analyses: dict[str, dict[str, Any]]) -> str:
    company = stock.get("company_name", "the company")
    quantitative_score = json.dumps(
        {
            "rule_score_out_of_50": stock.get("rule_score_out_of_50"),
        },
        indent=2,
    )
    return f"""
You are the final equity research scoring node for {company}.

Quantitative score from scraping and Excel rules:
{quantitative_score}

Question-by-question AI analyses:
{json.dumps(analyses, indent=2)}

Create the final stock evaluation. Use rule_score_out_of_50 as the deterministic first 50 points. Assign ai_score_out_of_50 from the question analyses only, considering:
- future growth/outlook quality
- concrete initiative progress
- management promise delivery
- current news sentiment
- industry tailwind/headwind
- exaggeration or overpromising risk
- confidence scores, risk scores, and missing evidence

Score fields:
- news_sentiment_score: 0 means very negative, 50 means mixed/neutral/unknown, 100 means very positive.
- industry_outlook_score: 0 means severe industry headwind, 50 means neutral/unclear, 100 means strong industry tailwind.
- exaggeration_risk_score: 0 means low exaggeration/overpromising risk, 50 means moderate risk, 100 means severe risk.

Return concise JSON matching the response schema. The final verdict should be practical for an investor.
""".strip()


def litellm_json(
    prompt: str,
    base_url: str,
    api_key: str,
    model: str,
    response_schema: dict[str, Any],
) -> tuple[dict[str, Any], str, list[dict[str, str]], dict[str, Any]]:
    endpoint = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You are a financial analyst assistant. Output strictly valid JSON matching the requested schema.",
            },
            {"role": "user", "content": prompt},
        ],
        "response_format": {"type": "json_object"},
    }
    response = requests.post(
        endpoint,
        headers=headers,
        json=payload,
        timeout=120,
    )
    if not response.ok:
        raise RuntimeError(f"LiteLLM request failed ({response.status_code}): {response.text}")
    raw_payload = response.json()
    choices = raw_payload.get("choices", [])
    if not choices:
        raise RuntimeError(f"LiteLLM returned empty choices: {raw_payload}")
    text = choices[0].get("message", {}).get("content", "")
    citations: list[dict[str, str]] = []
    return json_from_text(text), text, citations, raw_payload


def litellm_settings() -> tuple[str, str, str]:
    load_env()
    base_url = os.environ.get("LITELLM_BASE_URL", DEFAULT_BASE_URL)
    api_key = os.environ.get("LITELLM_API_KEY", DEFAULT_API_KEY)
    model = os.environ.get("LITELLM_MODEL", DEFAULT_MODEL)
    return base_url, api_key, model


def ensure_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        parts = [f"{k}: {ensure_str(v)}" for k, v in value.items()]
        return "\n".join(parts)
    if isinstance(value, list):
        return "\n".join(ensure_str(item) for item in value if item is not None)
    return str(value)


def ensure_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        res = []
        for item in value:
            if item is None:
                continue
            s = ensure_str(item)
            if s.strip():
                res.append(s)
        return res
    if isinstance(value, dict):
        return [f"{k}: {ensure_str(v)}" for k, v in value.items()]
    s = ensure_str(value)
    return [s] if s.strip() else []


def validate_section(parsed: dict[str, Any], spec: QuestionSpec) -> dict[str, Any]:
    parsed["question_id"] = spec.question_id
    parsed["answer"] = ensure_str(parsed.get("answer"))
    parsed["evidence_summary"] = ensure_str_list(parsed.get("evidence_summary"))
    parsed["confidence_score"] = clamp_score(parsed.get("confidence_score", 0))
    parsed["risk_score"] = clamp_score(parsed.get("risk_score", 0))
    parsed["open_questions"] = ensure_str_list(parsed.get("open_questions"))
    try:
        section = SectionAnalysis.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError(f"AI response for {spec.question_id} did not match the section schema: {exc}") from exc
    return section.model_dump()


def clamp_score(value: Any) -> float:
    try:
        score = float(value or 0)
    except (TypeError, ValueError):
        score = 0.0
    return max(0.0, min(100.0, score))


def quota_limited_error(exc: Exception) -> bool:
    message = str(exc)
    return "429" in message or "RESOURCE_EXHAUSTED" in message


def fallback_section(spec: QuestionSpec, reason: str) -> dict[str, Any]:
    return SectionAnalysis(
        question_id=spec.question_id,
        answer=f"Could not complete this step. {reason}",
        evidence_summary=["Evidence was unavailable because this step failed before analysis completed."],
        confidence_score=0,
        risk_score=50,
        open_questions=[f"Retry {spec.title} after resolving the error."],
    ).model_dump()


def validate_evaluation(parsed: dict[str, Any], stock: dict[str, Any]) -> dict[str, Any]:
    rule_score = float(stock.get("rule_score_out_of_50", 0) or 0)
    parsed["ai_score_out_of_50"] = max(0.0, min(50.0, float(parsed.get("ai_score_out_of_50", 0) or 0)))
    parsed["total_score_out_of_100"] = rule_score + parsed["ai_score_out_of_50"]
    parsed["news_sentiment_score"] = clamp_score(parsed.get("news_sentiment_score", 50))
    parsed["industry_outlook_score"] = clamp_score(parsed.get("industry_outlook_score", 50))
    parsed["exaggeration_risk_score"] = clamp_score(parsed.get("exaggeration_risk_score", 50))

    parsed["verdict"] = ensure_str(parsed.get("verdict"))
    parsed["future_outlook"] = ensure_str(parsed.get("future_outlook"))
    parsed["initiatives_progress"] = ensure_str(parsed.get("initiatives_progress"))
    parsed["promise_delivery"] = ensure_str(parsed.get("promise_delivery"))
    parsed["key_reasons"] = ensure_str_list(parsed.get("key_reasons"))
    parsed["open_questions"] = ensure_str_list(parsed.get("open_questions"))

    try:
        evaluation = StockEvaluation.model_validate(parsed)
    except ValidationError as exc:
        raise RuntimeError(f"AI response did not match the stock evaluation schema: {exc}") from exc
    result = evaluation.model_dump()
    result["ai_score_out_of_50"] = round(float(result["ai_score_out_of_50"]), 2)
    result["total_score_out_of_100"] = round(float(result["total_score_out_of_100"]), 2)
    result["rule_score_out_of_50"] = rule_score
    result["company_name"] = stock.get("company_name")
    result["company_url"] = stock.get("company_url")
    result["stock_id"] = stock.get("stock_id")
    return result


def regex_document_filters(question: str) -> dict[str, list[str]]:
    normalized = question.upper()
    years: set[str] = set()
    quarters: set[str] = set()
    document_types: set[str] = set()

    for quarter_match in re.finditer(r"\bQ([1-4])\b", normalized):
        quarters.add(f"Q{quarter_match.group(1)}")

    for fy_match in re.finditer(r"\bFY\s*[-']?\s*(20)?(\d{2})\b", normalized):
        years.add(f"FY20{fy_match.group(2)}")

    for year_match in re.finditer(r"\b(20\d{2})\b", normalized):
        years.add(f"FY{year_match.group(1)}")

    for short_year_match in re.finditer(r"\b(?:MAR|JUN|SEP|SEPT|DEC)\s*[-']?\s*(\d{2})\b", normalized):
        years.add(f"FY20{short_year_match.group(1)}")

    if re.search(r"\bANNUAL\s+REPORTS?\b|\bANNUAL\s+RESULTS?\b", normalized):
        document_types.add("report")
        quarters.add("FY")
    if re.search(r"\bQUARTERLY\s+REPORTS?\b|\bQUARTERLY\s+RESULTS?\b", normalized):
        document_types.add("report")
    if re.search(r"\bCONCALLS?\b|\bCONFERENCE\s+CALLS?\b|\bEARNINGS\s+CALLS?\b", normalized):
        document_types.add("concall")
    if re.search(r"\bREPORTS?\b", normalized):
        document_types.add("report")

    return {
        "document_years": sorted(years),
        "document_quarters": sorted(quarters),
        "document_types": sorted(document_types),
    }


def normalize_filter_payload(payload: dict[str, Any]) -> dict[str, Any]:
    years = []
    for year in payload.get("document_years", []) or []:
        digits = re.sub(r"\D", "", str(year))
        if len(digits) == 2:
            digits = f"20{digits}"
        if len(digits) == 4:
            years.append(f"FY{digits}")

    quarters = []
    for quarter in payload.get("document_quarters", []) or []:
        text = str(quarter).upper()
        if re.search(r"\bFY(?:\b|\d{2,4})|\bFULL\s+YEAR\b|\bANNUAL\b", text):
            quarters.append("FY")
            continue
        match = re.search(r"\bQ([1-4])\b", text)
        if match:
            quarters.append(f"Q{match.group(1)}")

    valid_types = {"report", "concall"}
    type_aliases = {
        "annual": "report",
        "annual_report": "report",
        "annual_results": "report",
        "quarterly": "report",
        "quarterly_report": "report",
        "quarterly_results": "report",
        "results": "report",
        "report": "report",
        "concall": "concall",
        "concall_transcript": "concall",
        "conference_call": "concall",
        "earnings_call": "concall",
    }
    document_types = []
    for document_type in payload.get("document_types", []) or []:
        key = re.sub(r"[^a-z0-9]+", "_", str(document_type).lower()).strip("_")
        normalized_type = type_aliases.get(key)
        if normalized_type in valid_types:
            document_types.append(normalized_type)

    years = sorted(set(years))
    quarters = sorted(set(quarters))
    document_types = sorted(set(document_types))
    return {
        "apply_metadata_filter": bool(payload.get("apply_metadata_filter")) and bool(years or quarters or document_types),
        "document_years": years,
        "document_quarters": quarters,
        "document_types": document_types,
        "reason": str(payload.get("reason", "")),
    }


def infer_document_filters(question: str) -> dict[str, Any]:
    prompt = f"""
Extract only explicit document-period filters from this user question.

Question:
{question}

Rules:
- Extract explicit years such as 2023, 2024, FY24, FY2025, Mar-25, Jun-26.
- Extract explicit quarter/period values such as FY, Q1, Q2, Q3, Q4.
- Extract explicit document types: report or concall.
- If the user asks to compare years, return all mentioned years.
- If the user asks to compare quarters or periods, return all mentioned quarter/period values.
- If the user asks for an annual report, return document_quarters as FY.
- If the user asks about annual reports, quarterly reports, results, or reports in general, return report.
- If the user asks about concalls, conference calls, or earnings calls, return concall.
- Do not infer relative periods such as latest, previous, recent, older, last year, or current quarter.
- If there is no explicit year, quarter, or document type, set apply_metadata_filter to false and return empty arrays.
- Return document_years as FY plus four digits, for example FY25 becomes FY2025.
- Return document_quarters using only FY, Q1, Q2, Q3, or Q4.
- Return document_types using only report or concall.
""".strip()
    try:
        base_url, api_key, model = litellm_settings()
        parsed, _raw_text, _citations, _raw_payload = litellm_json(
            prompt,
            base_url,
            api_key,
            model,
            DOCUMENT_FILTER_RESPONSE_SCHEMA,
        )
        normalized = normalize_filter_payload(parsed)
        if normalized["apply_metadata_filter"]:
            return normalized
    except Exception:
        pass

    # Metadata inference must not make document Q&A unavailable when LLM fails.
    fallback = regex_document_filters(question)
    return {
        "apply_metadata_filter": bool(
            fallback["document_years"] or fallback["document_quarters"] or fallback["document_types"]
        ),
        **fallback,
        "reason": "Inferred with deterministic fallback.",
    }


def analyze_question(state: EvaluationState, spec: QuestionSpec) -> EvaluationState:
    chunks = retrieve_chunks(state["stock_id"], spec.retrieval_queries) if spec.retrieval_queries else []
    search_results = external_search_results(state["stock"], spec) if spec.search_kind != "none" else []
    context = format_search_context(search_results) if spec.search_kind != "none" else format_rag_context(chunks)
    prompt = build_section_prompt(state["stock"], spec, context)
    base_url, api_key, model = litellm_settings()
    try:
        parsed, raw_text, citations, raw_payload = litellm_json(
            prompt,
            base_url,
            api_key,
            model,
            SECTION_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        if spec.search_kind != "none" and quota_limited_error(exc):
            section = fallback_section(spec, str(exc).splitlines()[0])
            analyses = dict(state.get("section_analyses", {}))
            citations_by_section = dict(state.get("section_citations", {}))
            raw_text_by_section = dict(state.get("section_raw_model_text", {}))
            raw_payloads_by_section = dict(state.get("section_raw_payloads", {}))
            chunk_refs_by_section = dict(state.get("section_chunk_refs", {}))
            search_results_by_section = dict(state.get("section_search_results", {}))
            analyses[spec.question_id] = section
            citations_by_section[spec.question_id] = search_citations(search_results)
            raw_text_by_section[spec.question_id] = section["answer"]
            raw_payloads_by_section[spec.question_id] = {"fallback": "quota_exhausted"}
            chunk_refs_by_section[spec.question_id] = chunk_refs(chunks)
            search_results_by_section[spec.question_id] = search_results
            return {
                **state,
                "section_analyses": analyses,
                "section_citations": citations_by_section,
                "section_raw_model_text": raw_text_by_section,
                "section_raw_payloads": raw_payloads_by_section,
                "section_chunk_refs": chunk_refs_by_section,
                "section_search_results": search_results_by_section,
            }
        raise RuntimeError(f"AI section failed at {spec.question_id}: {exc}") from exc
    section = validate_section(parsed, spec)

    # LangGraph state is shared between nodes, so copy nested mappings before
    # adding this node's output instead of mutating prior state in place.
    analyses = dict(state.get("section_analyses", {}))
    citations_by_section = dict(state.get("section_citations", {}))
    raw_text_by_section = dict(state.get("section_raw_model_text", {}))
    raw_payloads_by_section = dict(state.get("section_raw_payloads", {}))
    chunk_refs_by_section = dict(state.get("section_chunk_refs", {}))
    search_results_by_section = dict(state.get("section_search_results", {}))

    analyses[spec.question_id] = section
    citations_by_section[spec.question_id] = search_citations(search_results) if spec.search_kind != "none" else citations
    raw_text_by_section[spec.question_id] = raw_text
    raw_payloads_by_section[spec.question_id] = raw_payload
    chunk_refs_by_section[spec.question_id] = chunk_refs(chunks)
    search_results_by_section[spec.question_id] = search_results

    return {
        **state,
        "section_analyses": analyses,
        "section_citations": citations_by_section,
        "section_raw_model_text": raw_text_by_section,
        "section_raw_payloads": raw_payloads_by_section,
        "section_chunk_refs": chunk_refs_by_section,
        "section_search_results": search_results_by_section,
    }


def make_question_node(spec: QuestionSpec):
    def node(state: EvaluationState) -> EvaluationState:
        return analyze_question(state, spec)

    return node


def final_scoring_node(state: EvaluationState) -> EvaluationState:
    prompt = build_final_prompt(state["stock"], state.get("section_analyses", {}))
    base_url, api_key, model = litellm_settings()
    try:
        parsed, raw_text, citations, raw_payload = litellm_json(
            prompt,
            base_url,
            api_key,
            model,
            EVALUATION_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        raise RuntimeError(f"AI section failed at final_scoring: {exc}") from exc
    result = validate_evaluation(parsed, state["stock"])

    section_citations = state.get("section_citations", {})
    flat_citations: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for citation_list in list(section_citations.values()) + [citations]:
        for citation in citation_list:
            url = citation.get("url", "")
            if url and url not in seen_urls:
                flat_citations.append(citation)
                seen_urls.add(url)

    result["citations"] = flat_citations
    result["section_analyses"] = state.get("section_analyses", {})
    result["section_citations"] = section_citations
    result["section_chunk_refs"] = state.get("section_chunk_refs", {})
    result["section_search_results"] = state.get("section_search_results", {})
    result["section_raw_model_text"] = state.get("section_raw_model_text", {})
    result["section_raw_payloads"] = state.get("section_raw_payloads", {})
    result["raw_model_text"] = raw_text
    result["raw_payload"] = raw_payload
    result["rag_chunk_count"] = sum(len(chunks) for chunks in state.get("section_chunk_refs", {}).values())
    result["rag_queries"] = {spec.question_id: list(spec.retrieval_queries) for spec in QUESTION_SPECS}
    result.update(document_store.stock_document_fingerprint(str(state["stock_id"])))
    save_evaluation(str(state["stock_id"]), result)
    return {**state, "final_prompt": prompt, "final_raw_model_text": raw_text, "final_raw_payload": raw_payload, "result": result}


def run_evaluation_graph(stock: dict[str, Any]) -> dict[str, Any]:
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RuntimeError("LangGraph is not installed. Run: python -m pip install -r requirements.txt") from exc

    graph = StateGraph(EvaluationState)
    previous_node = ""
    for spec in QUESTION_SPECS:
        node_name = f"analyze_{spec.question_id}"
        graph.add_node(node_name, make_question_node(spec))
        if previous_node:
            graph.add_edge(previous_node, node_name)
        else:
            graph.set_entry_point(node_name)
        previous_node = node_name

    graph.add_node("final_scoring", final_scoring_node)
    graph.add_edge(previous_node, "final_scoring")
    graph.add_edge("final_scoring", END)
    final_state = graph.compile().invoke(
        {
            "stock": stock,
            "stock_id": str(stock["stock_id"]),
            "section_analyses": {},
            "section_citations": {},
            "section_raw_model_text": {},
            "section_raw_payloads": {},
            "section_chunk_refs": {},
            "section_search_results": {},
        }
    )
    return final_state["result"]


def evaluate_stock(stock: dict[str, Any], api_key: str | None = None, model: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    load_env()
    if api_key:
        os.environ["LITELLM_API_KEY"] = api_key
    if model:
        os.environ["LITELLM_MODEL"] = model
    if base_url:
        os.environ["LITELLM_BASE_URL"] = base_url
    cached = current_evaluation(str(stock["stock_id"]))
    if cached:
        return cached
    return run_evaluation_graph(stock)


def save_evaluation(stock_id: str, result: dict[str, Any]) -> None:
    AI_DIR.mkdir(parents=True, exist_ok=True)
    (AI_DIR / f"{stock_id}.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


def load_evaluation(stock_id: str) -> dict[str, Any] | None:
    path = AI_DIR / f"{stock_id}.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def current_evaluation(stock_id: str) -> dict[str, Any] | None:
    evaluation = load_evaluation(stock_id)
    if not evaluation:
        return None
    # Reuse the expensive evaluation until the stock's uploaded document set changes.
    document_state = document_store.stock_document_fingerprint(stock_id)
    if evaluation.get("document_fingerprint") != document_state["document_fingerprint"]:
        return None
    if int(evaluation.get("document_count", -1)) != int(document_state["document_count"]):
        return None
    return evaluation


def qa_trace_chunks(source_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    traced_chunks: list[dict[str, Any]] = []
    for source_id, chunk in source_map.items():
        text = str(chunk.get("text", ""))
        traced_chunks.append(
            {
                "source_id": source_id,
                "document_name": chunk.get("filename", ""),
                "document_sha": chunk.get("document_sha", ""),
                "chunk_index": chunk.get("chunk_index", ""),
                "page_start": chunk.get("page_start", ""),
                "page_end": chunk.get("page_end", ""),
                "document_year": chunk.get("document_year", ""),
                "document_quarter": chunk.get("document_quarter", ""),
                "document_type": chunk.get("document_type", ""),
                "similarity_score": round(float(chunk.get("score", 0) or 0), 3),
                "text_chars": len(text),
                "text_excerpt": text[:1800] + ("..." if len(text) > 1800 else ""),
            }
        )
    return traced_chunks


def save_qa_trace(stock_id: str, trace: dict[str, Any]) -> dict[str, str]:
    QA_RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stock_dir = QA_RUNS_DIR / safe_name(stock_id)
    stock_dir.mkdir(parents=True, exist_ok=True)
    created_at = datetime.now().isoformat(timespec="seconds")
    trace_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{safe_name(stock_id)}"
    trace = {
        "trace_id": trace_id,
        "created_at": created_at,
        **trace,
    }
    path = stock_dir / f"{trace_id}.json"
    path.write_text(json.dumps(trace, indent=2), encoding="utf-8")
    return {
        "trace_id": trace_id,
        "trace_path": str(path.relative_to(PROJECT_ROOT)),
    }


def min_rag_similarity_score() -> float:
    load_env()
    try:
        return float(os.environ.get("RAG_MIN_SIMILARITY_SCORE", str(DEFAULT_MIN_RAG_SIMILARITY_SCORE)))
    except ValueError:
        return DEFAULT_MIN_RAG_SIMILARITY_SCORE


def split_chunks_by_similarity(chunks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float]:
    threshold = min_rag_similarity_score()
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for chunk in chunks:
        score = float(chunk.get("score", 0) or 0)
        if score >= threshold:
            accepted.append(chunk)
        else:
            rejected.append(chunk)
    return accepted, rejected, threshold


def answer_document_question(
    stock: dict[str, Any],
    question: str,
) -> dict[str, Any]:
    question = question.strip()
    if not question:
        raise RuntimeError("Question is required.")

    stock_id = str(stock["stock_id"])
    inferred_filters = infer_document_filters(question)
    chunks = document_store.search_chunks(
        stock_id,
        question,
        limit=8,
        document_years=inferred_filters.get("document_years", []),
        document_quarters=inferred_filters.get("document_quarters", []),
        document_types=inferred_filters.get("document_types", []),
    )
    used_filter_fallback = False
    # Explicit metadata is preferred, but widening the search is more useful than
    # a false empty result when uploaded metadata and query wording disagree.
    if not chunks and inferred_filters.get("apply_metadata_filter"):
        used_filter_fallback = True
        chunks = document_store.search_chunks(stock_id, question, limit=8)

    raw_retrieved_chunks = list(chunks)
    chunks, rejected_chunks, similarity_threshold = split_chunks_by_similarity(raw_retrieved_chunks)
    raw_source_map = {f"S{index}": chunk for index, chunk in enumerate(raw_retrieved_chunks, start=1)}
    rejected_source_map = {f"R{index}": chunk for index, chunk in enumerate(rejected_chunks, start=1)}
    raw_traced_chunks = qa_trace_chunks(raw_source_map)
    rejected_traced_chunks = qa_trace_chunks(rejected_source_map)

    # Stop before LLM when retrieval found nothing above the relevance gate.
    # This saves a model call and prevents answers built from unrelated context.
    if not chunks:
        answer = (
            "No matching uploaded document chunks were found for this question."
            if not raw_retrieved_chunks
            else "No sufficiently relevant uploaded document chunks were found for this question."
        )
        limitation = (
            "Upload relevant documents for this stock and try again."
            if not raw_retrieved_chunks
            else f"The nearest retrieved chunks were below the similarity threshold of {similarity_threshold:.2f}."
        )
        trace_info = save_qa_trace(
            stock_id,
            {
                "stock": {
                    "stock_id": stock_id,
                    "company_name": stock.get("company_name", ""),
                    "stock_source": stock.get("stock_source", ""),
                },
                "question": question,
                "inferred_filters": inferred_filters,
                "used_filter_fallback": used_filter_fallback,
                "similarity_threshold": similarity_threshold,
                "raw_retrieved_chunk_count": len(raw_retrieved_chunks),
                "raw_retrieved_chunks": raw_traced_chunks,
                "rejected_chunk_count": len(rejected_chunks),
                "rejected_chunks": rejected_traced_chunks,
                "retrieved_chunk_count": 0,
                "retrieved_chunks": [],
                "prompt": "",
                "model": "",
                "parsed_response": {
                    "answer": answer,
                    "citation_source_ids": [],
                    "limitations": [limitation],
                },
                "raw_model_text": "",
                "raw_payload": {},
                "status": "no_chunks" if not raw_retrieved_chunks else "below_similarity_threshold",
            },
        )
        return {
            "answer": answer,
            "citations": [],
            "limitations": [limitation],
            "retrieved_chunk_count": 0,
            "inferred_filters": inferred_filters,
            "trace": {
                **trace_info,
                "inferred_filters": inferred_filters,
                "used_filter_fallback": used_filter_fallback,
                "similarity_threshold": similarity_threshold,
                "raw_retrieved_chunk_count": len(raw_retrieved_chunks),
                "raw_retrieved_chunks": raw_traced_chunks,
                "rejected_chunk_count": len(rejected_chunks),
                "rejected_chunks": rejected_traced_chunks,
                "retrieved_chunks": [],
                "prompt": "",
                "parsed_response": {
                    "answer": answer,
                    "citation_source_ids": [],
                    "limitations": [limitation],
                },
                "raw_model_text": "",
                "raw_payload": {},
                "status": "no_chunks" if not raw_retrieved_chunks else "below_similarity_threshold",
            },
        }

    context, source_map = format_document_qa_context(chunks)
    prompt = build_document_qa_prompt(stock, question, context, inferred_filters)
    base_url, api_key, model = litellm_settings()
    traced_chunks = qa_trace_chunks(source_map)
    try:
        parsed, raw_text, _citations, raw_payload = litellm_json(
            prompt,
            base_url,
            api_key,
            model,
            DOCUMENT_QA_RESPONSE_SCHEMA,
        )
    except Exception as exc:
        trace_info = save_qa_trace(
            stock_id,
            {
                "stock": {
                    "stock_id": stock_id,
                    "company_name": stock.get("company_name", ""),
                    "stock_source": stock.get("stock_source", ""),
                },
                "question": question,
                "inferred_filters": inferred_filters,
                "used_filter_fallback": used_filter_fallback,
                "similarity_threshold": similarity_threshold,
                "raw_retrieved_chunk_count": len(raw_retrieved_chunks),
                "raw_retrieved_chunks": raw_traced_chunks,
                "rejected_chunk_count": len(rejected_chunks),
                "rejected_chunks": rejected_traced_chunks,
                "retrieved_chunk_count": len(chunks),
                "retrieved_chunks": traced_chunks,
                "prompt": prompt,
                "model": model,
                "parsed_response": {},
                "raw_model_text": "",
                "raw_payload": {},
                "status": "model_error",
                "error": str(exc),
            },
        )
        raise RuntimeError(f"Document QA failed. Trace saved at {trace_info['trace_path']}. {exc}") from exc

    citations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_id in parsed.get("citation_source_ids", []) or []:
        source_id = str(source_id)
        chunk = source_map.get(source_id)
        if not chunk or source_id in seen:
            continue
        seen.add(source_id)
        page_start = int(chunk.get("page_start") or 0)
        page_end = int(chunk.get("page_end") or page_start or 0)
        citations.append(
            {
                "source_id": source_id,
                "document_name": chunk.get("filename", ""),
                "page_start": page_start if page_start else None,
                "page_end": page_end if page_end else None,
                "document_year": chunk.get("document_year", ""),
                "document_quarter": chunk.get("document_quarter", ""),
                "document_type": chunk.get("document_type", ""),
                "chunk_index": chunk.get("chunk_index", ""),
                "similarity_score": round(float(chunk.get("score", 0) or 0), 3),
            }
        )

    cited_source_ids = {str(citation.get("source_id", "")) for citation in citations}
    ignored_source_ids = [
        str(chunk.get("source_id", ""))
        for chunk in traced_chunks
        if str(chunk.get("source_id", "")) not in cited_source_ids
    ]
    trace_info = save_qa_trace(
        stock_id,
        {
            "stock": {
                "stock_id": stock_id,
                "company_name": stock.get("company_name", ""),
                "stock_source": stock.get("stock_source", ""),
            },
            "question": question,
            "inferred_filters": inferred_filters,
            "used_filter_fallback": used_filter_fallback,
            "similarity_threshold": similarity_threshold,
            "raw_retrieved_chunk_count": len(raw_retrieved_chunks),
            "raw_retrieved_chunks": raw_traced_chunks,
            "rejected_chunk_count": len(rejected_chunks),
            "rejected_chunks": rejected_traced_chunks,
            "retrieved_chunk_count": len(chunks),
            "retrieved_chunks": traced_chunks,
            "cited_source_ids": sorted(cited_source_ids),
            "ignored_source_ids": ignored_source_ids,
            "prompt": prompt,
            "model": model,
            "parsed_response": parsed,
            "raw_model_text": raw_text,
            "raw_payload": raw_payload,
            "status": "complete",
        },
    )

    return {
        "answer": str(parsed.get("answer", "")),
        "citations": citations,
        "limitations": parsed.get("limitations", []) or [],
        "retrieved_chunk_count": len(chunks),
        "inferred_filters": inferred_filters,
        "used_filter_fallback": used_filter_fallback,
        "raw_model_text": raw_text,
        "raw_payload": raw_payload,
        "trace": {
            **trace_info,
            "model": model,
            "inferred_filters": inferred_filters,
            "used_filter_fallback": used_filter_fallback,
            "similarity_threshold": similarity_threshold,
            "raw_retrieved_chunk_count": len(raw_retrieved_chunks),
            "raw_retrieved_chunks": raw_traced_chunks,
            "rejected_chunk_count": len(rejected_chunks),
            "rejected_chunks": rejected_traced_chunks,
            "retrieved_chunk_count": len(chunks),
            "retrieved_chunks": traced_chunks,
            "cited_source_ids": sorted(cited_source_ids),
            "ignored_source_ids": ignored_source_ids,
            "prompt": prompt,
            "parsed_response": parsed,
            "raw_model_text": raw_text,
            "raw_payload": raw_payload,
            "status": "complete",
        },
    }
