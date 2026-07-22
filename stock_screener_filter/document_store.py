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


def save_upload(stock_id: str, company_name: str, filename: str, source: BinaryIO) -> StoredDocument:
    ensure_dirs()
    stock_raw_dir = RAW_DIR / stock_id
    stock_raw_dir.mkdir(parents=True, exist_ok=True)
    temp_path = stock_raw_dir / f".upload_{safe_name(filename)}"
    with temp_path.open("wb") as target:
        shutil.copyfileobj(source, target)

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
            note="Already uploaded for this stock.",
        )

    stored_path = stock_raw_dir / f"{digest[:12]}_{safe_name(filename)}"
    temp_path.replace(stored_path)
    text, note = extract_text(stored_path)
    chunks = chunk_text(text)
    write_chunks(stock_id, digest, filename, chunks)
    stock_docs[digest] = {
        "company_name": company_name,
        "filename": filename,
        "sha256": digest,
        "stored_path": str(stored_path.relative_to(PROJECT_ROOT)),
        "text_chars": len(text),
        "chunk_count": len(chunks),
        "embedding_model": embedding_model_name(),
        "embedding_dimensions": embedding_dimensions(),
        "chunker": "RecursiveCharacterTextSplitter",
        "chunk_size": chunk_size(),
        "chunk_overlap": chunk_overlap(),
        "note": note,
    }
    save_document_index(index)
    return StoredDocument(
        stock_id=stock_id,
        company_name=company_name,
        filename=filename,
        sha256=digest,
        stored_path=str(stored_path),
        text_chars=len(text),
        chunk_count=len(chunks),
        duplicate=False,
        note=note,
    )


def extract_text(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_pdf_text(path)
    if suffix in {".txt", ".md", ".csv", ".json", ".log"}:
        return path.read_text(encoding="utf-8", errors="ignore"), ""
    if suffix in {".html", ".htm"}:
        text = path.read_text(encoding="utf-8", errors="ignore")
        text = re.sub(r"<script\b.*?</script>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<style\b.*?</style>", " ", text, flags=re.I | re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        return html.unescape(" ".join(text.split())), ""
    return path.read_text(encoding="utf-8", errors="ignore"), "Read as plain text."


def extract_pdf_text(path: Path) -> tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "PDF text extraction needs pypdf. Run: pip install -r requirements.txt"

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return "\n\n".join(pages), ""


def chunk_size() -> int:
    load_env()
    return int(os.environ.get("RAG_CHUNK_SIZE", str(DEFAULT_CHUNK_SIZE)))


def chunk_overlap() -> int:
    load_env()
    return int(os.environ.get("RAG_CHUNK_OVERLAP", str(DEFAULT_CHUNK_OVERLAP)))


def chunk_text(text: str) -> list[str]:
    cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not cleaned:
        return []
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
    except ImportError as exc:
        raise RuntimeError("LangChain text splitters are not installed. Run: python -m pip install -r requirements.txt") from exc

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size(),
        chunk_overlap=chunk_overlap(),
        separators=["\n\n", "\n", ". ", "; ", ", ", " ", ""],
        length_function=len,
    )
    return [chunk.strip() for chunk in splitter.split_text(cleaned) if chunk.strip()]


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
    stock_digest = hashlib.sha256(stock_id.encode("utf-8")).hexdigest()[:16]
    return f"stock-bge-m3-{stock_digest}"


def stock_collection(stock_id: str) -> object:
    return chroma_client().get_or_create_collection(
        name=collection_name(stock_id),
        metadata={
            "hnsw:space": "cosine",
            "stock_id": stock_id,
            "embedding_model": embedding_model_name(),
        },
    )


def embedding_model_name() -> str:
    return DEFAULT_EMBEDDING_MODEL


def embedding_cache_exists() -> bool:
    model_cache_name = f"models--{embedding_model_name().replace('/', '--')}"
    return (MODEL_CACHE_DIR / model_cache_name).exists()


def embedding_local_files_only() -> bool:
    load_env()
    configured = os.environ.get("EMBEDDING_LOCAL_FILES_ONLY")
    if configured is not None:
        return configured.strip().lower() in {"1", "true", "yes", "on"}
    return embedding_cache_exists()


@lru_cache(maxsize=1)
def embedding_model() -> object:
    load_env()
    os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR))
    os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", str(MODEL_CACHE_DIR))
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    local_files_only = embedding_local_files_only()
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise RuntimeError("sentence-transformers is not installed. Run: python -m pip install -r requirements.txt") from exc

    device = os.environ.get("EMBEDDING_DEVICE") or None
    return SentenceTransformer(
        embedding_model_name(),
        cache_folder=str(MODEL_CACHE_DIR),
        device=device,
        local_files_only=local_files_only,
    )


def embedding_dimensions() -> int:
    dimensions = embedding_model().get_sentence_embedding_dimension()
    return int(dimensions or 0)


def embedding_batch_size() -> int:
    load_env()
    return int(os.environ.get("EMBEDDING_BATCH_SIZE", "16"))


def embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    encoded = embedding_model().encode(
        texts,
        batch_size=embedding_batch_size(),
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    return [vector.tolist() for vector in encoded]


def embedding(text: str) -> list[float]:
    return embeddings([text])[0]


def write_chunks(stock_id: str, document_sha: str, filename: str, chunks: list[str]) -> None:
    if not chunks:
        return
    collection = stock_collection(stock_id)
    ids = [f"{document_sha}:{index}" for index in range(1, len(chunks) + 1)]
    metadatas = [
        {
            "stock_id": stock_id,
            "document_sha": document_sha,
            "filename": filename,
            "chunk_index": index,
            "embedding_model": embedding_model_name(),
        }
        for index in range(1, len(chunks) + 1)
    ]
    collection.upsert(
        ids=ids,
        documents=chunks,
        metadatas=metadatas,
        embeddings=embeddings(chunks),
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


def search_chunks(stock_id: str, query: str, limit: int = 10) -> list[dict[str, object]]:
    collection = stock_collection(stock_id)
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[embedding(query)],
        n_results=min(limit, collection.count()),
        include=["documents", "metadatas", "distances"],
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
                "score": max(0.0, 1.0 - float(distance)),
            }
        )
    return chunks


def stock_documents(stock_id: str) -> list[dict[str, object]]:
    index = load_document_index()
    return list(index.get(stock_id, {}).values())
