"""Relational persistence for SQLite development and PostgreSQL deployments."""
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text, create_engine,
                        select, text, update)

from .config import settings

metadata = MetaData()
orders = Table("orders", metadata,
               Column("order_id", String, primary_key=True), Column("customer", String),
               Column("status", String), Column("eta", String), Column("amount", Float))
tickets = Table("tickets", metadata,
                Column("id", Integer, primary_key=True, autoincrement=True),
                Column("title", String), Column("description", Text), Column("priority", String),
                Column("state", String, default="open"), Column("created_at", Float))
audit_log = Table("audit_log", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("ts", Float), Column("actor", String), Column("action", String),
                  Column("detail", Text))
messages = Table("conversation_messages", metadata,
                 Column("id", Integer, primary_key=True, autoincrement=True),
                 Column("session_id", String, index=True), Column("role", String),
                 Column("content", Text), Column("created_at", Float))
approvals = Table("tool_approvals", metadata,
                  Column("id", Integer, primary_key=True, autoincrement=True),
                  Column("tool_name", String), Column("arguments", Text),
                  Column("state", String, default="pending"), Column("result", Text),
                  Column("created_at", Float), Column("approved_at", Float),
                  Column("executed_at", Float))

_engine = None


def engine():
    global _engine
    if _engine is None:
        url = settings.database_url
        if url.startswith("sqlite:"):
            db_file = url.split("///", 1)[-1]
            if db_file and db_file != ":memory:":
                Path(db_file).parent.mkdir(parents=True, exist_ok=True)
            _engine = create_engine(url, connect_args={"check_same_thread": False})
        else:
            _engine = create_engine(url, pool_pre_ping=True)
    return _engine


@contextmanager
def conn():
    """Compatibility connection for existing SQLite scripts and read-only tools."""
    if settings.database_url.startswith("sqlite:"):
        db_file = settings.database_url.split("///", 1)[-1]
        if db_file and db_file != ":memory:":
            Path(db_file).parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(db_file or ":memory:")
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    else:
        with engine().begin() as connection:
            yield connection


def init_db() -> None:
    metadata.create_all(engine())


def audit(actor: str, action: str, detail: dict) -> None:
    with engine().begin() as c:
        c.execute(audit_log.insert().values(ts=time.time(), actor=actor, action=action,
                                           detail=json.dumps(detail, ensure_ascii=False)))


def save_message(session_id: str, role: str, content: str) -> None:
    with engine().begin() as c:
        c.execute(messages.insert().values(session_id=session_id, role=role, content=content,
                                           created_at=time.time()))


def get_history(session_id: str, limit: int = 12) -> list[dict]:
    with engine().connect() as c:
        rows = c.execute(select(messages.c.role, messages.c.content)
                         .where(messages.c.session_id == session_id)
                         .order_by(messages.c.id.desc()).limit(limit)).all()
    return [{"role": role, "content": content} for role, content in reversed(rows)]


def get_order_status(order_id: str) -> dict | None:
    with engine().connect() as c:
        row = c.execute(select(orders).where(orders.c.order_id == order_id.strip().upper())).mappings().first()
    return dict(row) if row else None


def upsert_order(order_id: str, customer: str, status: str, eta: str, amount: float) -> None:
    with engine().begin() as c:
        if engine().dialect.name == "sqlite":
            c.execute(text("INSERT OR REPLACE INTO orders(order_id,customer,status,eta,amount) "
                           "VALUES (:order_id,:customer,:status,:eta,:amount)"),
                      {"order_id": order_id, "customer": customer, "status": status,
                       "eta": eta, "amount": amount})
        else:
            c.execute(text("INSERT INTO orders(order_id,customer,status,eta,amount) "
                           "VALUES (:order_id,:customer,:status,:eta,:amount) "
                           "ON CONFLICT(order_id) DO UPDATE SET customer=excluded.customer, "
                           "status=excluded.status, eta=excluded.eta, amount=excluded.amount"),
                      {"order_id": order_id, "customer": customer, "status": status,
                       "eta": eta, "amount": amount})


def create_approval(tool_name: str, arguments: dict) -> int:
    with engine().begin() as c:
        result = c.execute(approvals.insert().values(
            tool_name=tool_name, arguments=json.dumps(arguments, ensure_ascii=False),
            state="pending", created_at=time.time()))
        return result.inserted_primary_key[0]


def get_approval(approval_id: int) -> dict | None:
    with engine().connect() as c:
        row = c.execute(select(approvals).where(approvals.c.id == approval_id)).mappings().first()
    if not row:
        return None
    result = dict(row)
    result["arguments"] = json.loads(result["arguments"])
    if result["result"]:
        result["result"] = json.loads(result["result"])
    return result


def list_approvals(state: str | None = None) -> list[dict]:
    query = select(approvals).order_by(approvals.c.id.desc())
    if state:
        query = query.where(approvals.c.state == state)
    with engine().connect() as c:
        rows = c.execute(query).mappings().all()
    output = []
    for row in rows:
        item = dict(row)
        item["arguments"] = json.loads(item["arguments"])
        if item["result"]:
            item["result"] = json.loads(item["result"])
        output.append(item)
    return output


def transition_approval(approval_id: int, expected: str, target: str,
                        result: dict | None = None) -> bool:
    changes = {"state": target}
    if target == "approved":
        changes["approved_at"] = time.time()
    elif target == "executed":
        changes["executed_at"] = time.time()
        changes["result"] = json.dumps(result or {}, ensure_ascii=False)
    with engine().begin() as c:
        updated = c.execute(update(approvals).where(
            approvals.c.id == approval_id, approvals.c.state == expected).values(**changes))
        return updated.rowcount == 1


def execute_approval(approval_id: int) -> dict | None:
    """Execute the approved ticket write and advance state atomically."""
    with engine().begin() as c:
        query = select(approvals).where(approvals.c.id == approval_id)
        if engine().dialect.name == "postgresql":
            query = query.with_for_update()
        row = c.execute(query).mappings().first()
        if not row:
            return None
        if row["state"] != "approved":
            raise ValueError(f"approval must be approved before execution (state={row['state']})")
        if row["tool_name"] != "create_support_ticket":
            raise ValueError(f"unsupported write tool: {row['tool_name']}")
        args = json.loads(row["arguments"])
        result = c.execute(tickets.insert().values(
            title=args["title"], description=args["description"], priority=args["priority"],
            state="open", created_at=time.time()))
        ticket = {"ticket_id": result.inserted_primary_key[0], "state": "open"}
        c.execute(update(approvals).where(approvals.c.id == approval_id,
                                         approvals.c.state == "approved").values(
            state="executed", result=json.dumps(ticket, ensure_ascii=False), executed_at=time.time()))
        return ticket


def create_ticket(title: str, description: str, priority: str) -> dict:
    with engine().begin() as c:
        result = c.execute(tickets.insert().values(title=title, description=description,
                                                    priority=priority, state="open",
                                                    created_at=time.time()))
        return {"ticket_id": result.inserted_primary_key[0], "state": "open"}
