"""第 4 層：把 AI 包成企業系統能呼叫的服務（REST + Webhook + 驗證 + 稽核）。"""
import hmac
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import agent, db, llm, rag
from .config import settings

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    yield


app = FastAPI(title="AI Integration Service", lifespan=lifespan)


def require_key(x_api_key: str = Header(default="")):
    if not hmac.compare_digest(x_api_key, settings.app_api_key):
        raise HTTPException(401, "invalid api key")


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    session_id: str | None = None
    metadata_filter: dict | None = None


class IngestReq(BaseModel):
    directory: str = "docs"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ingest", dependencies=[Depends(require_key)])
def ingest(req: IngestReq):
    return {"chunks_indexed": rag.ingest_dir(req.directory)}


@app.delete("/knowledge", dependencies=[Depends(require_key)])
def delete_knowledge(source: str = Query(min_length=1)):
    deleted = rag.delete_source(source)
    db.audit("api", "rag_delete_source", {"source": source, "chunks_deleted": deleted})
    return {"source": source, "chunks_deleted": deleted}


@app.post("/chat", dependencies=[Depends(require_key)])
def chat(req: Ask):
    session_id = req.session_id or str(uuid.uuid4())
    history = db.get_history(session_id)
    db.save_message(session_id, "user", req.question)
    result = rag.answer(req.question, history=history, metadata_filter=req.metadata_filter)
    db.save_message(session_id, "assistant", result["answer"])
    db.audit("api", "rag_chat", {"q": req.question, "sources": result["sources"]})
    return {**result, "session_id": session_id}


@app.post("/chat/stream", dependencies=[Depends(require_key)])
def chat_stream(req: Ask):
    session_id = req.session_id or str(uuid.uuid4())
    history = db.get_history(session_id)
    db.save_message(session_id, "user", req.question)

    def generate():
        output = []
        prompt = req.question
        # For streamed RAG, pass retrieved documents into the same grounded prompt.
        hits = rag.retrieve(prompt, metadata_filter=req.metadata_filter)
        if hits:
            docs = "\n".join(f'<doc id="{i+1}" source="{h["source"]}">{h["text"]}</doc>'
                             for i, h in enumerate(hits))
            prompt = f"<documents>\n{docs}\n</documents>\n\n問題：{prompt}"
        else:
            prompt = "知識庫中找不到相關資訊。問題：" + prompt
        for part in llm.stream(prompt, system=rag.RAG_SYSTEM, history=history):
            output.append(part)
            yield part
        db.save_message(session_id, "assistant", "".join(output))
    return StreamingResponse(generate(), media_type="text/plain; charset=utf-8",
                             headers={"X-Session-ID": session_id})


@app.post("/agent", dependencies=[Depends(require_key)])
def run(req: Ask):
    session_id = req.session_id or str(uuid.uuid4())
    history = db.get_history(session_id)
    db.save_message(session_id, "user", req.question)
    result = agent.run_agent(req.question, history=history)
    db.save_message(session_id, "assistant", result["answer"])
    db.audit("api", "agent_run", {"q": req.question, "steps": len(result["trace"])})
    return {**result, "session_id": session_id}


@app.get("/sessions/{session_id}/messages", dependencies=[Depends(require_key)])
def session_messages(session_id: str):
    return {"session_id": session_id, "messages": db.get_history(session_id, limit=100)}


@app.get("/approvals", dependencies=[Depends(require_key)])
def approvals(state: str | None = None):
    return {"items": db.list_approvals(state)}


@app.post("/approvals/{approval_id}/approve", dependencies=[Depends(require_key)])
def approve(approval_id: int):
    if not db.transition_approval(approval_id, "pending", "approved"):
        item = db.get_approval(approval_id)
        if not item:
            raise HTTPException(404, "approval not found")
        raise HTTPException(409, f"cannot approve action in state {item['state']}")
    db.audit("human", "approval_approved", {"approval_id": approval_id})
    return db.get_approval(approval_id)


@app.post("/approvals/{approval_id}/execute", dependencies=[Depends(require_key)])
def execute_approval(approval_id: int):
    try:
        result = db.execute_approval(approval_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "approval not found")
    db.audit("human", "approval_executed", {"approval_id": approval_id, **result})
    return {"approval_id": approval_id, "state": "executed", **result}


# ---- 企業流程範例：helpdesk / Email / 表單 → webhook → AI 分流 → 回寫 ----
TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["帳務", "物流", "退換貨", "技術問題", "其他"]},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
        "summary": {"type": "string", "description": "一句話摘要"},
        "draft_reply": {"type": "string", "description": "給客戶的回覆草稿"},
        "needs_human": {"type": "boolean", "description": "涉及退款、法律、客訴升級時為 true"},
    },
    "required": ["category", "priority", "summary", "draft_reply", "needs_human"],
}


class TicketWebhook(BaseModel):
    ticket_id: str
    subject: str
    body: str


def triage_payload(p: TicketWebhook):
    result = llm.extract_structured(
        f"主旨：{p.subject}\n內文：{p.body}", TRIAGE_SCHEMA,
        instruction="你是客服分流系統，請分類並產生繁體中文回覆草稿。草稿僅供人工審核，不得承諾退款或賠償。",
        name="triage",
    )
    db.audit("webhook", "triage", {"ticket": p.ticket_id, **result})
    # 真實情境：在這裡呼叫 Zendesk / Jira / Slack API 回寫標籤與草稿
    return {"ticket_id": p.ticket_id, **result,
            "route": "human_review" if result["needs_human"] else "auto_draft"}


@app.post("/webhook/triage", dependencies=[Depends(require_key)], status_code=202)
def triage(p: TicketWebhook):
    try:
        from .tasks import process_triage
        task = process_triage.delay(p.model_dump())
    except Exception as exc:
        logging.exception("could not enqueue triage webhook")
        raise HTTPException(503, "triage queue unavailable; start Redis and a Celery worker") from exc
    return {"job_id": task.id, "state": task.state}


@app.get("/webhook/jobs/{job_id}", dependencies=[Depends(require_key)])
def triage_job(job_id: str):
    from .tasks import celery_app
    result = celery_app.AsyncResult(job_id)
    payload = {"job_id": job_id, "state": result.state}
    if result.successful():
        payload["result"] = result.result
    elif result.failed():
        payload["error"] = str(result.result)
    return payload


@app.get("/ui", include_in_schema=False)
def ui():
    from fastapi.responses import HTMLResponse
    html = Path(__file__).parent.parent / "web" / "index.html"
    return HTMLResponse(html.read_text(encoding="utf-8"))
