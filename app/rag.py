"""Incremental RAG ingestion, metadata-filtered retrieval, and a lightweight reranker."""
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import text

from . import llm
from .config import settings
from .db import engine

_collection = None
_encoder = None
_pg_ready = False


def _use_pgvector() -> bool:
    if settings.vector_backend == "pgvector":
        return True
    if settings.vector_backend == "chroma":
        return False
    return not settings.database_url.startswith("sqlite:")


def _get_encoder():
    global _encoder
    if _encoder is None:
        from sentence_transformers import SentenceTransformer
        _encoder = SentenceTransformer(settings.embed_model)
    return _encoder


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(pg.extract_text() or "" for pg in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8")


def chunk_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        while len(p) > size:
            chunks.append(p[:size])
            p = p[size - overlap:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _get_collection():
    global _collection
    if _collection is None:
        import chromadb
        from chromadb.utils import embedding_functions
        embedding = embedding_functions.SentenceTransformerEmbeddingFunction(
            model_name=settings.embed_model)
        client = chromadb.PersistentClient(path=settings.chroma_dir)
        _collection = client.get_or_create_collection(
            "knowledge_base", embedding_function=embedding, metadata={"hnsw:space": "cosine"})
    return _collection


def _ensure_pgvector():
    global _pg_ready
    if _pg_ready:
        return
    dimension = _get_encoder().get_sentence_embedding_dimension()
    with engine().begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        connection.execute(text(
            f"CREATE TABLE IF NOT EXISTS kb_chunks (id TEXT PRIMARY KEY, content TEXT NOT NULL, "
            f"metadata JSONB NOT NULL, embedding vector({int(dimension)}) NOT NULL)"))
        connection.execute(text(
            "CREATE INDEX IF NOT EXISTS kb_chunks_embedding_idx ON kb_chunks "
            "USING hnsw (embedding vector_cosine_ops)"))
    _pg_ready = True


def _existing_chunks() -> dict:
    if _use_pgvector():
        _ensure_pgvector()
        with engine().connect() as connection:
            rows = connection.execute(text("SELECT id, metadata FROM kb_chunks")).all()
        return {row._mapping["id"]: (
            row._mapping["metadata"] if isinstance(row._mapping["metadata"], dict)
            else json.loads(row._mapping["metadata"])) for row in rows}
    data = _get_collection().get(include=["metadatas"])
    return {cid: metadata or {} for cid, metadata in zip(data["ids"], data["metadatas"])}


def _upsert_pg(chunks: list[dict]) -> None:
    if not chunks:
        return
    vectors = _get_encoder().encode([item["text"] for item in chunks], normalize_embeddings=True)
    with engine().begin() as connection:
        for item, vector in zip(chunks, vectors):
            connection.execute(text(
                "INSERT INTO kb_chunks(id,content,metadata,embedding) "
                "VALUES (:id,:content,CAST(:metadata AS jsonb),CAST(:embedding AS vector)) "
                "ON CONFLICT(id) DO UPDATE SET content=EXCLUDED.content, "
                "metadata=EXCLUDED.metadata, embedding=EXCLUDED.embedding"), {
                    "id": item["id"], "content": item["text"],
                    "metadata": json.dumps(item["metadata"], ensure_ascii=False),
                    "embedding": "[" + ",".join(str(float(value)) for value in vector) + "]",
                })


def ingest_dir(directory: str) -> int:
    root = Path(directory).resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"knowledge directory not found: {directory}")
    desired, source_hashes = [], {}
    root_hash = hashlib.sha256(str(root).encode()).hexdigest()
    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in {".md", ".txt", ".pdf"}:
            continue
        content = _read(path)
        source = path.relative_to(root).as_posix()
        source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        source_hashes[source] = source_hash
        for index, chunk in enumerate(chunk_text(content)):
            cid = hashlib.sha256(f"{root_hash}:{source}:{index}".encode()).hexdigest()
            desired.append({"id": cid, "text": chunk, "metadata": {
                "source": source, "chunk": index, "source_hash": source_hash,
                "content_hash": hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                "index_root": root_hash,
            }})

    existing = _existing_chunks()
    desired_ids = {item["id"] for item in desired}
    changed = [item for item in desired
               if existing.get(item["id"], {}).get("content_hash") != item["metadata"]["content_hash"]]
    stale_ids = [cid for cid, metadata in existing.items()
                 if cid not in desired_ids and
                 (metadata.get("index_root") == root_hash or
                  (not metadata.get("index_root") and metadata.get("source") in source_hashes))]

    if _use_pgvector():
        _upsert_pg(changed)
        if stale_ids:
            with engine().begin() as connection:
                connection.execute(text("DELETE FROM kb_chunks WHERE id = ANY(:ids)"), {"ids": stale_ids})
    else:
        collection = _get_collection()
        if changed:
            collection.upsert(ids=[item["id"] for item in changed],
                              documents=[item["text"] for item in changed],
                              metadatas=[item["metadata"] for item in changed])
        if stale_ids:
            collection.delete(ids=stale_ids)
    return len(changed)


def delete_source(source: str) -> int:
    """Delete all indexed chunks for a relative source path."""
    if _use_pgvector():
        _ensure_pgvector()
        with engine().begin() as connection:
            result = connection.execute(text("DELETE FROM kb_chunks WHERE metadata->>'source'=:source"),
                                        {"source": source})
            return result.rowcount
    collection = _get_collection()
    result = collection.get(where={"source": source}, include=[])
    if result["ids"]:
        collection.delete(ids=result["ids"])
    return len(result["ids"])


def _lexical_score(query: str, document: str) -> float:
    def terms(value: str) -> set[str]:
        tokens = re.findall(r"[\w]+", value.lower())
        compact = re.sub(r"\s+", "", value.lower())
        tokens.extend(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
        return set(tokens)
    q, d = terms(query), terms(document)
    return len(q & d) / max(1, len(q))


def retrieve(query: str, k: int = 4, max_distance: float = 0.75,
             metadata_filter: dict | None = None) -> list[dict]:
    candidates = []
    if _use_pgvector():
        _ensure_pgvector()
        vector = _get_encoder().encode([query], normalize_embeddings=True)[0]
        vector_literal = "[" + ",".join(str(float(value)) for value in vector) + "]"
        filter_clause = "AND metadata @> CAST(:filters AS jsonb)" if metadata_filter else ""
        params = {"embedding": vector_literal, "limit": max(k * 4, k),
                  "filters": json.dumps(metadata_filter or {})}
        with engine().connect() as connection:
            rows = connection.execute(text(
                "SELECT content, metadata, (embedding <=> CAST(:embedding AS vector)) AS distance "
                f"FROM kb_chunks WHERE 1=1 {filter_clause} "
                "ORDER BY embedding <=> CAST(:embedding AS vector) LIMIT :limit"), params).all()
        for row in rows:
            meta_value = row._mapping["metadata"]
            meta = meta_value if isinstance(meta_value, dict) else json.loads(meta_value)
            candidates.append({"text": row._mapping["content"], "source": meta["source"],
                               "distance": float(row._mapping["distance"]), **meta})
    else:
        collection = _get_collection()
        count = collection.count()
        if not count:
            return []
        result = collection.query(query_texts=[query], n_results=min(count, max(k * 4, k)),
                                  where=metadata_filter or None)
        for doc, meta, distance in zip(result["documents"][0], result["metadatas"][0],
                                       result["distances"][0]):
            if distance <= max_distance:
                candidates.append({"text": doc, "source": meta["source"],
                                   "distance": float(distance), **meta})
    candidates = [item for item in candidates if item["distance"] <= max_distance]
    for item in candidates:
        semantic = max(0.0, 1.0 - item["distance"])
        item["rerank_score"] = round(0.7 * semantic + 0.3 * _lexical_score(query, item["text"]), 4)
        item["distance"] = round(item["distance"], 3)
    return sorted(candidates, key=lambda item: item["rerank_score"], reverse=True)[:k]


RAG_SYSTEM = (
    "你是企業內部知識庫助理，使用繁體中文回答。"
    "只能根據 <documents> 內的內容回答；若內容不足以回答，直接說「知識庫中找不到相關資訊」，不要臆測。"
    "引用時在句末標註來源編號，例如 [1]。"
)


def answer(question: str, k: int = 4, history: list[dict] | None = None,
           metadata_filter: dict | None = None) -> dict:
    hits = retrieve(question, k, metadata_filter=metadata_filter)
    if not hits:
        return {"answer": "知識庫中找不到相關資訊。", "sources": []}
    docs = "\n".join(f'<doc id="{i+1}" source="{h["source"]}">{h["text"]}</doc>'
                     for i, h in enumerate(hits))
    prompt = f"<documents>\n{docs}\n</documents>\n\n問題：{question}"
    return {"answer": llm.chat(prompt, system=RAG_SYSTEM, history=history),
            "sources": [{"id": i + 1, "source": h["source"], "distance": h["distance"],
                         "rerank_score": h["rerank_score"]}
                        for i, h in enumerate(hits)]}
