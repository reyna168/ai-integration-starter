import os

os.environ["DB_PATH"] = "/tmp/test_app.db"

from app import db, tools  # noqa: E402
from app.rag import chunk_text  # noqa: E402


def test_chunk_respects_size():
    text = "\n\n".join(["段落" * 100] * 5)
    chunks = chunk_text(text, size=300, overlap=50)
    assert chunks and all(len(c) <= 300 for c in chunks)


def test_order_tool_and_injection_safe():
    db.init_db()
    with db.conn() as c:
        c.execute("INSERT OR REPLACE INTO orders VALUES ('T1','x','已出貨','2026-12-01',1)")
    assert tools.dispatch("get_order_status", {"order_id": "t1"})["status"] == "已出貨"
    assert "error" in tools.dispatch("get_order_status", {"order_id": "' OR 1=1 --"})


def test_unknown_tool():
    assert "error" in tools.dispatch("rm_rf", {})
