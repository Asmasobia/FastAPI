"""
notifications.py — a FAKE "send email" used to demonstrate BackgroundTasks.

WHY THIS EXISTS:
  When a task is created we want to "notify" the assignee. Sending an email is
  slow and the client shouldn't wait for it. FastAPI's BackgroundTasks lets the
  endpoint RETURN IMMEDIATELY (201 Created) and run this AFTER the response is
  sent. Fire-and-forget for quick, non-critical side effects.

THE LIMIT (know this for interviews):
  BackgroundTasks runs IN THE SAME PROCESS as the web server. If the process
  crashes or restarts before the task runs, the work is LOST — there's no
  retry, no durability, no queue. That's fine for "best-effort" side effects
  (a welcome email, a cache warm). For anything that MUST happen (payments,
  critical emails, work needing retries/backoff/dead-letter queues), you use a
  real task queue like Celery or RQ backed by Redis/RabbitMQ. The README's "what
  I'd add next" section calls this out explicitly.

Here we just log, so the project needs no real mail server.
"""

import asyncio
import logging

logger = logging.getLogger("app.notifications")


async def send_task_assigned_email(
    *, to_user_id: str, task_id: str, task_title: str
) -> None:
    """
    Pretend to email the assignee that a task was assigned to them.

    We `await asyncio.sleep(...)` to SIMULATE the latency of a real network call
    to an email provider — this is exactly the kind of slow work you don't want
    the client waiting on, which is the whole point of running it in the
    background.
    """
    await asyncio.sleep(0.1)  # simulate network/API latency.
    logger.info(
        "[fake-email] Notified user=%s about task=%s (%r)",
        to_user_id,
        task_id,
        task_title,
    )
