---
name: system-architecture
description: Use when designing, documenting, or reviewing system architecture for a project. Trigger on requests for architecture diagrams, component boundaries, data flow, ADRs, system design docs, integration design, scalability/reliability tradeoffs, or "系統架構/架構設計/架構圖/技術架構" tasks.
---

# System Architecture

## Workflow

1. **Understand scope** - Identify the system goal, users, constraints, and what decision the architecture must support.
2. **Inspect current state** - Read relevant code/config/docs before proposing changes. In this repo, start with `README.md`, `app/main.py`, `app/config.py`, `app/db.py`, `app/rag.py`, `app/agent.py`, `app/tasks.py`, and `docker-compose.yml`.
3. **Define boundaries** - Map components, responsibilities, interfaces, data stores, external dependencies, and trust boundaries.
4. **Document decisions** - Capture key tradeoffs as ADR-style notes: context, decision, alternatives, consequences.
5. **Validate** - Check failure modes, security, observability, deployment, cost, and migration path. Prefer concrete verification steps.

## Deliverables

Choose the lightest artifact that fits the request:

- **Architecture summary**: purpose, components, data flow, key decisions, risks.
- **Diagram**: Mermaid `flowchart`/`sequenceDiagram`/`C4`-style view when useful.
- **ADR**: `Context`, `Decision`, `Alternatives`, `Consequences`, `Status`.
- **Design review**: findings ordered by risk, with affected files/components and recommendations.

## Checklist

- **Interfaces**: API contracts, auth, sync/async boundaries, idempotency, timeouts/retries.
- **Data**: source of truth, schema/storage choice, indexing, retention, PII handling.
- **Reliability**: failure modes, degradation path, queues/workers, backpressure, health checks.
- **Security**: least privilege, secrets, input validation, audit logging, approval gates for write actions.
- **Operations**: deploy topology, config/env vars, logging/metrics/traces, cost drivers.
- **Change**: migration/rollout, backward compatibility, test strategy.

## Project Patterns (ai-integration-starter)

- **API/service layer**: FastAPI in `app/main.py`; auth via `X-API-Key`; audit via `db.audit`.
- **LLM layer**: `app/llm.py` wraps OpenAI-compatible/Ollama chat, streaming, structured output, tool calls.
- **RAG**: `app/rag.py` handles ingestion, chunking, embeddings, Chroma or pgvector retrieval/rerank.
- **Agent/tools**: `app/agent.py` runs tool loop; `app/tools.py` defines read/write tools; write actions require approval.
- **Persistence**: `app/db.py` supports SQLite local mode and PostgreSQL; conversation/approvals/audit persisted.
- **Async integration**: `app/tasks.py` + Redis/Celery for webhook triage; Docker Compose wires Postgres/Redis/API/worker.

## Output Rules

- Be specific: name files, components, and data flows.
- State assumptions and unknowns explicitly.
- Separate current state from proposed changes.
- Prefer simple, evolvable designs; call out tradeoffs instead of over-designing.
