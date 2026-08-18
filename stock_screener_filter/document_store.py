"""Local document dedupe, LangChain chunking, and Chroma vector retrieval."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO

from stock_screener_filter.config import load_env


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORE_DIR = PROJECT_ROOT / "data" / "document_store"
RAW_DIR = STORE_DIR / "raw"
CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_db"
MODEL_CACHE_DIR = PROJECT_ROOT / "data" / "model_cache"
DOCUMENTS_PATH = STORE_DIR / "documents.json"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_CHUNK_SIZE = 2500
DEFAULT_CHUNK_OVERLAP = 350
VALID_DOCUMENT_TYPES = {"report", "concall"}


@dataclass(frozen=True)
class StoredDocument:
    stock_id: str
    company_name: str
    filename: str
    sha256: str
    stored_path: str
    text_chars: int
    chunk_count: int
    duplicate: bool
    document_type: str = "report"
    document_year: str = ""
    document_quarter: str = ""
    note: str = ""


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CHROMA_DIR.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    STORE_DIR.mkdir(parents=True, exist_ok=True)


def load_document_index() -> dict[str, dict[str, object]]:
    ensure_dirs()
    if not DOCUMENTS_PATH.is_file():
        return {}
    text = DOCUMENTS_PATH.read_text(encoding="utf-8-sig").strip()
    return json.loads(text) if text else {}


def save_document_index(index: dict[str, dict[str, object]]) -> None:
    ensure_dirs()
    DOCUMENTS_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")


def safe_name(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    return safe or "document"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_upload(
    stock_id: str,
    company_name: str,
    filename: str,
    source: BinaryIO,
    document_type: str = "report",
    document_year: str = "",
    document_quarter: str = "",
) -> StoredDocument:
    ensure_dirs()
    document_type = document_type.strip().lower()
    if document_type not in VALID_DOCUMENT_TYPES:
        raise RuntimeError("Document type must be either report or concall.")
    stock_raw_dir = RAW_DIR / stock_id
    stock_raw_dir.mkdir(parents=True, exist_ok=True)
    import uuid
    temp_path = stock_raw_dir / f".upload_{uuid.uuid4().hex}_{safe_name(filename)}"
    with temp_path.open("wb") as target:
        shutil.copyfileobj(source, target)

    # Deduplicate by file contents, not by the user-supplied filename.
    digest = sha256_file(temp_path)
    index = load_document_index()
    stock_docs = index.setdefault(stock_id, {})
    existing = stock_docs.get(digest)
    if existing:
        temp_path.unlink(missing_ok=True)
        return StoredDocument(
            stock_id=stock_id,
            company_name=company_name,
            filename=filename,
            sha256=digest,
            stored_path=str(existing["stored_path"]),
            text_chars=int(existing.get("text_chars", 0)),
            chunk_count=int(existing.get("chunk_count", 0)),
            duplicate=True,
            document_type=str(existing.get("document_type", "report")),
            document_year=str(existing.get("document_year", "")),
            document_quarter=str(existing.get("document_quarter", "")),
            note="Already uploaded for this stock.",
        )

    stored_path = stock_raw_dir / f"{digest[:12]}_{safe_name(filename)}"
    temp_path.replace(stored_path)
    pages, note = extract_pages(stored_path)
    chunks = chunk_pages(pages)
    write_chunks(stock_id, digest, filename, chunks, document_type, document_year, document_quarter)
    text_chars = sum(len(page["text"]) for page in pages)
    load_env()
    stock_docs[digest] = {
        "company_name": company_name,
        "filename": filename,
        "sha256": digest,
        "stored_path": str(stored_path.relative_to(PROJECT_ROOT)),
        "text_chars": text_chars,
        "chunk_count": len(chunks),
        "document_type": document_type,
        "document_year": document_year,
        "document_quarter": document_quarter,
        "embedding_model": DEFAULT_EMBEDDING_MODEL,
        "embedding_dimensions": int(embedding_model().get_sentence_embedding_dimension() or 0),
        "chunker": "RecursiveCharacterTextSplitter",
        "chunk_size": int(os.environ.get("RAG_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE))),
        "chunk_overlap": int(os.environ.get("RAG_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP))),
        "note": note,
    }
    save_document_index(index)
    return StoredDocument(
        stock_id=stock_id,
        company_name=company_name,
        filename=filename,
        sha256=digest,
        stored_path=str(stored_path),
        text_chars=text_chars,
        chunk_count=len(chunks),
        duplicate=False,
        document_type=document_type,
        document_year=document_year,
        document_quarter=document_quarter,
        note=note,
    )


def extract_pages(path: Path) -> tuple[list[dict[str, object]], str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_pages(path)
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        return [{"number": 1, "text": path.read_text(encoding="utf-8", errors="ignore")}], ""
    if suffix in {".html", ".htm"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return [{"number": 1, "text": html.unescape(" ".join(text.split()))}], ""
    return [{"number": 1, "text": path.read_text(encoding="utf-8", errors="ignore")}], "Read as plain text."


def extract_pdf_pages(path: Path) -> tuple[list[dict[str, object]], str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return [], "PDF text extraction needs pypdf. Run: pip install -r requirements.txt"

    reader = PdfReader(str(path))
    pages: list[dict[str, object]] = []
    for index, page in enumerate(reader.pages, start=1):
        pages.append({"number": index, "text": page.extract_text() or ""})
    return pages, ""


def clean_extracted_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def split_text_with_offsets(text: str) -> list[dict[str, object]]:
    if not text:
        return []
    load_env()
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise RuntimeError("LangChain text splitters are not installed. Run: python -m pip install -r requirements.txt") from exc

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=int(os.environ.get("RAG_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE))),
        chunk_overlap=int(os.environ.get("RAG_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP))),
        separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
        length_function=len,
        add_start_index=True,
    )
    chunks: list[dict[str, object]] = []
    for document in splitter.create_documents([text]):
        raw_text = document.page_content
        chunk_text = raw_text.strip()
        if not chunk_text:
            continue
        leading_trim = len(raw_text) - len(raw_text.lstrip())
        start = int(document.metadata.get("start_index", 0) or 0) + leading_trim
        chunks.append(
            {
                "text": chunk_text,
                "start": start,
                "end": start + len(chunk_text),
            }
        )
    return chunks


def chunk_pages(pages: list[dict[str, object]]) -> list[dict[str, object]]:
    # Build one coordinate space for the whole document so chunk overlap can cross
    # page boundaries; page_spans maps each resulting chunk back to citations.
    parts: list[str] = []
    page_spans: list[dict[str, int]] = []
    cursor = 0
    for page in pages:
        page_num = int(page.get("number", 0) or 0)
        page_text = clean_extracted_text(str(page.get("text", "")))
        if not page_text:
            continue
        if parts:
            parts.append(" ")
            cursor += 1
        start = cursor
        parts.append(page_text)
        cursor += len(page_text)
        page_spans.append({"page": page_num, "start": start, "end": cursor})

    full_text = "".join(parts)
    if not full_text:
        return []

    chunks: list[dict[str, object]] = []
    for chunk in split_text_with_offsets(full_text):
        start = int(chunk["start"])
        end = int(chunk["end"])
        # Half-open interval overlap: page_start < chunk_end and page_end > chunk_start.
        pages_for_chunk = [
            span["page"]
            for span in page_spans
            if span["start"] < end and span["end"] > start
        ]
        if not pages_for_chunk:
            continue
        chunks.append(
            {
                "text": str(chunk["text"]),
                "page_start": pages_for_chunk[0],
                "page_end": pages_for_chunk[-1],
                "pages": ",".join(str(page) for page in pages_for_chunk if page),
            }
        )
    return chunks


def chroma_client() -> object:
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "False")
    try:
        import chromadb
        from chromadb.config import Settings
    except ImportError as exc:
        raise RuntimeError("ChromaDB is not installed. Run: python -m pip install -r requirements.txt") from exc
    ensure_dirs()
    return chromadb.PersistentClient(
        path=str(CHROMA_DIR),
        settings=Settings(anonymized_telemetry=False),
    )


def collection_name(stock_id: str) -> str:
    # A stable digest gives Chroma-safe collection names while stock_id remains in metadata.
    stock_digest = hashlib.sha256(stock_id.encode("utf-8")).hexdigest()[:16]
    return f"stock-bge-m3-{stock_digest}"


def stock_collection(stock_id: str) -> object:
    return chroma_client().get_or_create_collection(
        name=collection_name(stock_id),
        metadata={
            "hnsw:space": "cosine",
            "stock_id": stock_id,
            "embedding_model": DEFAULT_EMBEDDING_MODEL,
        },
    )


@lru_cache(maxsize=1)
def embedding_model() -> object:
    load_env()
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(MODEL_CACHE_DIR))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    configured_local_only = os.environ.get("EMBEDDING_LOCAL_FILES_ONLY")
    if configured_local_only is None:
        # Permit the first download, then default to the existing local cache on later runs.
        model_cache_name = f"models--{DEFAULT_EMBEDDING_MODEL.replace('/', '--')}"
        local_files_only = (MODEL_CACHE_DIR / model_cache_name).exists()
    else:
        local_files_only = configured_local_only.strip().lower() in {"1", "true", "yes", "on"}
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is not installed. Run: python -m pip install -r requirements.txt") from exc

    device = os.environ.get("EMBEDDING_DEVICE") or None
    try:
        return SentenceTransformer(
            DEFAULT_EMBEDDING_MODEL,
            cache_folder=str(MODEL_CACHE_DIR),
            device=device,
            local_files_only=local_files_only,
        )
    except Exception:
        if local_files_only and configured_local_only is None:
            return SentenceTransformer(
                DEFAULT_EMBEDDING_MODEL,
                cache_folder=str(MODEL_CACHE_DIR),
                device=device,
                local_files_only=False,
            )
        raise


def embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    load_env()
    encoded = embedding_model().encode(
        texts,
        batch_size=int(os.environ.get("EMBEDDING_BATCH_SIZE", "16")),
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in encoded]


def write_chunks(
    stock_id: str,
    document_sha: str,
    filename: str,
    chunks: list[dict[str, object]],
    document_type: str = "report",
    document_year: str = "",
    document_quarter: str = "",
) -> None:
    if not chunks:
        return
    collection = stock_collection(stock_id)
    # Deterministic IDs make the upsert idempotent; all parallel lists retain chunk order.
    ids = [f"{document_sha}:{index}" for index in range(1, len(chunks) + 1)]
    metadatas = [
        {
            "stock_id": stock_id,
            "document_sha": document_sha,
            "filename": filename,
            "chunk_index": index,
            "page_start": int(chunk.get("page_start", 0) or 0),
            "page_end": int(chunk.get("page_end", 0) or 0),
            "pages": str(chunk.get("pages", "")),
            "document_type": document_type,
            "document_year": document_year,
            "document_quarter": document_quarter,
            "embedding_model": DEFAULT_EMBEDDING_MODEL,
        }
        for index, chunk in enumerate(chunks, start=1)
    ]
    texts = [str(chunk.get("text", "")) for chunk in chunks]
    collection.upsert(
        ids=ids,
        documents=texts,
        metadatas=metadatas,
        embeddings=embeddings(texts),
    )


def load_chunks(stock_id: str) -> list[dict[str, object]]:
    collection = stock_collection(stock_id)
    result = collection.get(include=["documents", "metadatas"])
    chunks: list[dict[str, object]] = []
    for chunk_id, text, metadata in zip(
        result.get("ids", []),
        result.get("documents", []),
        result.get("metadatas", []),
    ):
        chunks.append(
            {
                "id": chunk_id,
                "text": text,
                **(metadata or {}),
            }
        )
    return chunks


def search_chunks(
    stock_id: str,
    query: str,
    limit: int = 10,
    document_year: str = "",
    document_quarter: str = "",
    document_type: str = "",
    document_years: list[str] | None = None,
    document_quarters: list[str] | None = None,
    document_types: list[str] | None = None,
) -> list[dict[str, object]]:
    collection = stock_collection(stock_id)
    if collection.count() == 0:
        return []
    filters = []
    years = document_years or ([document_year] if document_year else [])
    quarters = document_quarters or ([document_quarter] if document_quarter else [])
    types = document_types or ([document_type] if document_type else [])
    years = [str(year).strip() for year in years if str(year).strip()]
    quarters = [str(quarter).strip().upper() for quarter in quarters if str(quarter).strip()]
    types = sorted({str(item).strip().lower() for item in types if str(item).strip().lower() in VALID_DOCUMENT_TYPES})
    if len(years) == 1:
        filters.append({"document_year": years[0]})
    elif len(years) > 1:
        filters.append({"document_year": {"$in": years}})
    if len(quarters) == 1:
        filters.append({"document_quarter": quarters[0]})
    elif len(quarters) > 1:
        filters.append({"document_quarter": {"$in": quarters}})
    if len(types) == 1:
        filters.append({"document_type": types[0]})
    elif len(types) > 1:
        filters.append({"document_type": {"$in": sorted(set(types))}})
    where = None
    if len(filters) == 1:
        where = filters[0]
    elif len(filters) > 1:
        where = {"$and": filters}
    query_kwargs = {
        "query_embeddings": [embeddings([query])[0]],
        "n_results": min(limit, collection.count()),
        "include": ["documents", "metadatas", "distances"],
    }
    if where:
        query_kwargs["where"] = where
    result = collection.query(
        **query_kwargs,
    )
    chunks: list[dict[str, object]] = []
    for text, metadata, distance in zip(
        result.get("documents", [[]])[0],
        result.get("metadatas", [[]])[0],
        result.get("distances", [[]])[0],
    ):
        chunks.append(
            {
                "text": text,
                **(metadata or {}),
                # Chroma returns cosine distance; callers consume a higher-is-better score.
                "score": max(0.0, 1.0 - float(distance)),
            }
        )
    return chunks


def stock_documents(stock_id: str) -> list[dict[str, object]]:
    index = load_document_index()
    return list(index.get(stock_id, {}).values())


def stocks_with_documents() -> list[dict[str, object]]:
    stocks: list[dict[str, object]] = []
    for stock_id, documents_by_hash in load_document_index().items():
        documents = list(documents_by_hash.values())
        if not documents:
            continue
        first_document = documents[0]
        company_name = str(first_document.get("company_name") or stock_id)
        stocks.append(
            {
                "stock_id": stock_id,
                "company_name": company_name,
                "company_url": "",
                "market_categories": "Document library",
                "stock_source": "document_store",
            }
        )
    return sorted(stocks, key=lambda stock: str(stock["company_name"]).lower())


def stock_document_fingerprint(stock_id: str) -> dict[str, object]:
    # Evaluation caches depend on the document set, not upload order or filenames.
    documents = stock_documents(stock_id)
    hashes = sorted(str(document.get("sha256", "")) for document in documents if document.get("sha256"))
    digest = hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()
    return {
        "document_count": len(hashes),
        "document_hashes": hashes,
        "document_fingerprint": digest,
    }
