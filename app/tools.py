"""Agent 可用的工具。原則：讀取類自由使用；寫入類最小權限 + 稽核 + 必要時人工核准。"""
from . import db

TOOLS = [
    {
        "name": "search_knowledge_base",
        "description": "搜尋公司內部知識庫（退換貨政策、運送、FAQ 等）。回答政策類問題前先使用。",
        "input_schema": {"type": "object",
                         "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    {
        "name": "get_order_status",
        "description": "以訂單編號查詢訂單狀態、預計到貨日與金額。",
        "input_schema": {"type": "object",
                         "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
    },
    {
        "name": "create_support_ticket",
        "description": "建立客服工單，交由人類客服處理。無法由知識庫或訂單資料解決時才使用。",
        "input_schema": {"type": "object", "properties": {
            "title": {"type": "string"}, "description": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "medium", "high"]}},
            "required": ["title", "description", "priority"]},
    },
]


def search_knowledge_base(query: str) -> dict:
    from . import rag
    return {"results": rag.retrieve(query, k=4)}


def get_order_status(order_id: str) -> dict:
    row = db.get_order_status(order_id)
    return row if row else {"error": f"找不到訂單 {order_id}"}


def create_support_ticket(title: str, description: str, priority: str) -> dict:
    approval_id = db.create_approval("create_support_ticket", {
        "title": title, "description": description, "priority": priority,
    })
    return {"approval_id": approval_id, "state": "pending",
            "note": "寫入動作已送交人工核准；核准並執行後才會建立工單"}


REGISTRY = {f.__name__: f for f in (search_knowledge_base, get_order_status, create_support_ticket)}


def dispatch(name: str, args: dict) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"unknown tool {name}"}
    try:
        result = fn(**args)
    except Exception as e:  # 工具錯誤回報給模型，讓它自己修正或改走別條路
        result = {"error": f"{type(e).__name__}: {e}"}
    db.audit("agent", name, {"args": args, "result": result})
    return result
