"""第 3 層：Agent。核心是一個迴圈：模型決定用工具 → 我們執行 → 把結果還給模型 → 直到給出最終答案。"""
import json

from . import llm, tools

AGENT_SYSTEM = (
    "你是電商客服 Agent，使用繁體中文，語氣專業親切。\n"
    "規則：政策問題先查知識庫；訂單問題先查訂單；查不到或客戶要求超出權限（退款、改價）時，"
    "建立工單轉人工，並告知客戶後續流程。絕不編造訂單或政策內容。"
)


def run_agent(user_message: str, history: list[dict] | None = None, max_steps: int = 6) -> dict:
    messages = (history or []) + [{"role": "user", "content": user_message}]
    trace = []
    for _ in range(max_steps):  # 步數上限，避免無限迴圈燒 token
        resp = llm.completion(
            [{"role": "system", "content": AGENT_SYSTEM}, *messages],
            max_tokens=1024, tools=llm.tool_specs(tools.TOOLS),
        )
        calls = resp["tool_calls"]
        if not calls:
            return {"answer": resp["content"], "trace": trace}

        if llm.uses_ollama():
            messages.append({"role": "assistant", "content": resp["content"],
                             "tool_calls": [{"function": {"name": call["name"],
                                                            "arguments": call["arguments"]}}
                                            for call in calls]})
        else:
            messages.append({"role": "assistant", "content": resp["content"],
                             "tool_calls": [
                                 {"id": call["id"], "type": "function",
                                  "function": {"name": call["name"],
                                                "arguments": json.dumps(call["arguments"])}}
                                 for call in calls]})
        results = []
        for call in calls:
            name = call["name"]
            args = call["arguments"]
            out = tools.dispatch(name, args)
            trace.append({"tool": name, "args": args, "result": out})
            if llm.uses_ollama():
                results.append({"role": "tool", "tool_name": name,
                                "content": json.dumps(out, ensure_ascii=False)})
            else:
                results.append({"role": "tool", "tool_call_id": call["id"],
                                "content": json.dumps(out, ensure_ascii=False)})
        messages.extend(results)
    return {"answer": "處理步驟過多，已轉交人工客服。", "trace": trace}
