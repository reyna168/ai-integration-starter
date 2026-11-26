"""OpenAI-compatible LLM client with native Ollama support for local models."""
import json
import logging
import time
import uuid
from typing import Iterator

from .config import settings

log = logging.getLogger("llm")
_client = None


def uses_ollama() -> bool:
    return settings.llm_base_url.rstrip("/").endswith(":11434/v1")


def _ollama_api_url() -> str:
    return settings.llm_base_url.rstrip("/")[:-3] + "/api/chat"


def get_client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                         max_retries=2, timeout=120)
    return _client


def _messages(prompt: str, system: str = "", history: list[dict] | None = None) -> list[dict]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.extend(history or [])
    messages.append({"role": "user", "content": prompt})
    return messages


def tool_specs(tools: list[dict]) -> list[dict]:
    """Convert the project's compact schema format to OpenAI/Ollama tool definitions."""
    return [{"type": "function", "function": {
        "name": tool["name"], "description": tool["description"],
        "parameters": tool["input_schema"],
    }} for tool in tools]


def completion(messages: list[dict], max_tokens: int = 1024, temperature: float = 0.2,
               tools: list[dict] | None = None) -> dict:
    """Return normalized completion data; disable Qwen thinking via native Ollama API."""
    if uses_ollama():
        import httpx
        payload = {"model": settings.model, "messages": messages, "stream": False,
                   "think": False,
                   "options": {"num_predict": max_tokens, "temperature": temperature}}
        if tools:
            payload["tools"] = tools
        response = httpx.post(_ollama_api_url(), json=payload, timeout=180)
        response.raise_for_status()
        message = response.json().get("message", {})
        calls = []
        for call in message.get("tool_calls", []):
            function = call.get("function", {})
            calls.append({"id": f"call_{uuid.uuid4().hex}", "name": function.get("name"),
                          "arguments": function.get("arguments", {})})
        return {"content": message.get("content", ""), "tool_calls": calls}

    response = get_client().chat.completions.create(
        model=settings.model, messages=messages, max_tokens=max_tokens,
        temperature=temperature, tools=tools,
    )
    message = response.choices[0].message
    calls = [{"id": call.id, "name": call.function.name,
              "arguments": json.loads(call.function.arguments or "{}")}
             for call in (message.tool_calls or [])]
    return {"content": message.content or "", "tool_calls": calls}


def chat(prompt: str, system: str = "", history: list[dict] | None = None,
         max_tokens: int = 1024, temperature: float = 0.2) -> str:
    started = time.time()
    result = completion(_messages(prompt, system, history), max_tokens, temperature)
    log.info("llm model=%s latency=%.2fs", settings.model, time.time() - started)
    return result["content"]


def stream(prompt: str, system: str = "", max_tokens: int = 1024) -> Iterator[str]:
    """Stream generated text for chat UIs."""
    messages = _messages(prompt, system)
    if uses_ollama():
        import httpx
        payload = {"model": settings.model, "messages": messages, "stream": True,
                   "think": False, "options": {"num_predict": max_tokens}}
        with httpx.stream("POST", _ollama_api_url(), json=payload, timeout=180) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line:
                    content = json.loads(line).get("message", {}).get("content")
                    if content:
                        yield content
        return

    response = get_client().chat.completions.create(
        model=settings.model, messages=messages, max_tokens=max_tokens, stream=True,
    )
    for part in response:
        text = part.choices[0].delta.content
        if text:
            yield text


def extract_structured(text: str, schema: dict, instruction: str, name: str = "record") -> dict:
    if uses_ollama():
        import httpx
        response = httpx.post(_ollama_api_url(), json={
            "model": settings.model,
            "messages": _messages(text, instruction + "\n請只輸出符合指定結構的 JSON。"),
            "stream": False, "think": False, "format": schema,
            "options": {"num_predict": 1024},
        }, timeout=180)
        response.raise_for_status()
        content = response.json().get("message", {}).get("content", "{}")
    else:
        response = get_client().chat.completions.create(
            model=settings.model,
            messages=_messages(text, f"{instruction}\n只輸出符合此 JSON Schema 的 JSON，不要加 Markdown：\n{json.dumps(schema, ensure_ascii=False)}"),
            response_format={"type": "json_object"}, max_tokens=1024,
        )
        content = response.choices[0].message.content or "{}"
    result = json.loads(content)
    if not isinstance(result, dict) or any(key not in result for key in schema.get("required", [])):
        raise ValueError(f"model did not return valid {name} output")
    return result
