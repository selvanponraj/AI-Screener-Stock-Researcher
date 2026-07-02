"""Local document dedupe, text chunking, and Chroma vector retrieval."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STORE_DIR = PROJECT_ROOT / "data" / "document_store"
RAW_DIR = STORE_DIR / "raw"
CHROMA_DIR = PROJECT_ROOT / "data" / "chroma_db"
DOCUMENTS_PATH = STORE_DIR / "documents.json"
TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_&.-]{1,}")
EMBEDDING_DIMENSIONS = 384


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


def chunk_text(text: str, chunk_words: int = 450, overlap_words: int = 80) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    step = max(1, chunk_words - overlap_words)
    for start in range(0, len(words), step):
        chunk = " ".join(words[start : start + chunk_words]).strip()
        if chunk:
            chunks.append(chunk)
    return chunks


def chroma_client() -> object:
    import os

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
    digest = hashlib.sha256(stock_id.encode("utf-8")).hexdigest()[:24]
    return f"stock-{digest}"


def stock_collection(stock_id: str) -> object:
    return chroma_client().get_or_create_collection(
        name=collection_name(stock_id),
        metadata={"hnsw:space": "cosine", "stock_id": stock_id},
    )


def tokenize(text: str) -> list[str]:
    return [token.lower() for token in TOKEN_RE.findall(text)]


def embedding(text: str) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    for token in tokenize(text):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[index] += sign
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return vector
    return [value / norm for value in vector]


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
        }
        for index in range(1, len(chunks) + 1)
    ]
    collection.upsert(
        ids=ids,
        documents=chunks,
        metadatas=metadatas,
        embeddings=[embedding(chunk) for chunk in chunks],
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
