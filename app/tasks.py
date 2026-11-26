"""Celery tasks for work that should not block webhook callers."""
from celery import Celery

from .config import settings

celery_app = Celery("ai_integration", broker=settings.redis_url, backend=settings.redis_url)
celery_app.conf.update(
    task_always_eager=settings.task_always_eager,
    task_eager_propagates=True,
    result_expires=3600,
    accept_content=["json"],
    task_serializer="json",
    result_serializer="json",
)


@celery_app.task(name="app.tasks.process_triage")
def process_triage(payload: dict) -> dict:
    from . import db
    from .main import TicketWebhook, triage_payload
    db.init_db()
    return triage_payload(TicketWebhook(**payload))
