"""Background processing.

With ``REDIS_URL`` set, uploads are queued on Redis and processed by RQ
workers (``rq worker packetpilot`` or ``python -m packetpilot.worker``).
Without Redis the API falls back to in-process background tasks, which is
fine for a single-instance demo.

Uploaded bytes are stored in the database by default (``PACKETPILOT_STORAGE=db``),
so API and workers can run on separate machines with nothing shared but
PostgreSQL and Redis. ``PACKETPILOT_STORAGE=fs`` keeps files on disk instead;
then ``PACKETPILOT_STORAGE_DIR`` must be shared storage.
"""

from __future__ import annotations

import logging

from .config import get_settings

log = logging.getLogger(__name__)


def process_job(submission_id: str) -> dict:
    from .service import PacketPilotService

    state = PacketPilotService().process(submission_id, actor="worker")
    return {"id": submission_id, "status": state.status, "open_issues": sum(1 for i in state.issues if i.status == "open")}


def enqueue(submission_id: str) -> str | None:
    """Queue processing on Redis. Returns the job id, or None if no Redis is configured."""
    settings = get_settings()
    if not settings.redis_url:
        return None
    from redis import Redis
    from rq import Queue

    q = Queue(settings.queue_name, connection=Redis.from_url(settings.redis_url))
    job = q.enqueue(process_job, submission_id, job_timeout=600, result_ttl=86400, failure_ttl=7 * 86400)
    return job.id


def main() -> None:  # pragma: no cover - thin CLI wrapper
    from redis import Redis
    from rq import Queue, Worker

    settings = get_settings()
    if not settings.redis_url:
        raise SystemExit("REDIS_URL is not set")
    conn = Redis.from_url(settings.redis_url)
    Worker([Queue(settings.queue_name, connection=conn)], connection=conn).work()


if __name__ == "__main__":
    main()
