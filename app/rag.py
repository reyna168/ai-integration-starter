"""第 2 層：RAG 知識庫。流程 = 載入 → 切塊 → 向量化 → 檢索 → 帶引用回答。"""
import hashlib
from pathlib import Path

from . import llm
from .config import settings


def chunk_text(text: str, size: int = 500, overlap: int = 80) -> list[str]:
    """先依段落切，再合併到 size 字元；字元切法對中文友善。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) + 2 <= size:
            buf = f"{buf}\n\n{p}" if buf else p
            continue
        if buf:
            chunks.append(buf)
        while len(p) > size:  # 單一段落過長時，用滑動視窗硬切
            chunks.append(p[:size])
            p = p[size - overlap:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks


def _read(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        return "\n\n".join(pg.extract_text() or "" for pg in PdfReader(str(path)).pages)
    return path.read_text(encoding="utf-8")


_collection = None


def _get_collection():
    global _collection
    if _collection is None:
        import chromadb
        from chromadb.utils import embedding_functions

        ef = embedding_functions.SentenceTransformerEmbeddingFunction(model_name=settings.embed_model)
        client = chromadb.PersistentClient(path=settings.chroma_dir)
        _collection = client.get_or_create_collection(
            "knowledge_base", embedding_function=ef, metadata={"hnsw:space": "cosine"})
    return _collection


def ingest_dir(directory: str) -> int:
    col, n = _get_collection(), 0
    for path in sorted(Path(directory).rglob("*")):
        if path.suffix.lower() not in {".md", ".txt", ".pdf"}:
            continue
        for i, ch in enumerate(chunk_text(_read(path))):
            cid = hashlib.md5(f"{path}:{i}".encode()).hexdigest()
            col.upsert(ids=[cid], documents=[ch], metadatas=[{"source": path.name, "chunk": i}])
            n += 1
    return n


def retrieve(query: str, k: int = 4, max_distance: float = 0.75) -> list[dict]:
    res = _get_collection().query(query_texts=[query], n_results=k)
    hits = []
    for doc, meta, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0]):
        if dist <= max_distance:  # 過濾不相關結果，避免硬湊答案
            hits.append({"text": doc, "source": meta["source"], "distance": round(dist, 3)})
    return hits


RAG_SYSTEM = (
    "你是企業內部知識庫助理，使用繁體中文回答。"
    "只能根據 <documents> 內的內容回答；若內容不足以回答，直接說「知識庫中找不到相關資訊」，不要臆測。"
    "引用時在句末標註來源編號，例如 [1]。"
)


def answer(question: str, k: int = 4) -> dict:
    hits = retrieve(question, k)
    if not hits:
        return {"answer": "知識庫中找不到相關資訊。", "sources": []}
    docs = "\n".join(f'<doc id="{i+1}" source="{h["source"]}">{h["text"]}</doc>'
                     for i, h in enumerate(hits))
    prompt = f"<documents>\n{docs}\n</documents>\n\n問題：{question}"
    return {"answer": llm.chat(prompt, system=RAG_SYSTEM),
            "sources": [{"id": i + 1, "source": h["source"], "distance": h["distance"]}
                        for i, h in enumerate(hits)]}
