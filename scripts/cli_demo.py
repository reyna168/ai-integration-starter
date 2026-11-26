"""不啟動伺服器，直接在終端機試 RAG 與 Agent。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
# Windows consoles may use legacy code pages that cannot print Traditional Chinese.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from app import agent, db, rag  # noqa: E402

db.init_db()
print("indexed chunks:", rag.ingest_dir("docs"))
print(rag.answer("拆封後可以退貨嗎？"))
r = agent.run_agent("我的訂單 A1001 到哪了？另外滿多少免運？")
print(r["answer"])
print(r["trace"])
