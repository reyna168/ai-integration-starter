import json
import sqlite3
import time
from pathlib import Path

from .config import settings


def conn() -> sqlite3.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS orders(
            order_id TEXT PRIMARY KEY, customer TEXT, status TEXT, eta TEXT, amount REAL);
        CREATE TABLE IF NOT EXISTS tickets(
            id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, description TEXT,
            priority TEXT, state TEXT DEFAULT 'open', created_at REAL);
        CREATE TABLE IF NOT EXISTS audit_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, action TEXT, detail TEXT);
        """)


def audit(actor: str, action: str, detail: dict) -> None:
    """所有 AI 的動作都留稽核紀錄，企業導入時這是必備。"""
    with conn() as c:
        c.execute("INSERT INTO audit_log(ts, actor, action, detail) VALUES (?,?,?,?)",
                  (time.time(), actor, action, json.dumps(detail, ensure_ascii=False)))
