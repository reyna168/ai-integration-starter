"""第 4 層：把 AI 包成企業系統能呼叫的服務（REST + Webhook + 驗證 + 稽核）。"""
import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException
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


class IngestReq(BaseModel):
    directory: str = "docs"


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/ingest", dependencies=[Depends(require_key)])
def ingest(req: IngestReq):
    return {"chunks_indexed": rag.ingest_dir(req.directory)}


@app.post("/chat", dependencies=[Depends(require_key)])
def chat(req: Ask):
    result = rag.answer(req.question)
    db.audit("api", "rag_chat", {"q": req.question, "sources": result["sources"]})
    return result


@app.post("/chat/stream", dependencies=[Depends(require_key)])
def chat_stream(req: Ask):
    return StreamingResponse(llm.stream(req.question), media_type="text/plain; charset=utf-8")


@app.post("/agent", dependencies=[Depends(require_key)])
def run(req: Ask):
    result = agent.run_agent(req.question)
    db.audit("api", "agent_run", {"q": req.question, "steps": len(result["trace"])})
    return result


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


@app.post("/webhook/triage", dependencies=[Depends(require_key)])
def triage(p: TicketWebhook):
    result = llm.extract_structured(
        f"主旨：{p.subject}\n內文：{p.body}", TRIAGE_SCHEMA,
        instruction="你是客服分流系統，請分類並產生繁體中文回覆草稿。草稿僅供人工審核，不得承諾退款或賠償。",
        name="triage",
    )
    db.audit("webhook", "triage", {"ticket": p.ticket_id, **result})
    # 真實情境：在這裡呼叫 Zendesk / Jira / Slack API 回寫標籤與草稿
    return {"ticket_id": p.ticket_id, **result,
            "route": "human_review" if result["needs_human"] else "auto_draft"}
