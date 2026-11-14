"""Durable database leases serialize research; SQS is an optional wakeup signal."""

import logging
import threading
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select

from .api import TERMINAL, serialize
from .config import Settings
from .db import add_event, create_session_factory, require_schema
from .documents import ingest
from .models import Document, Message, Run, User, now

logger = logging.getLogger(__name__)


def claim_run(factory, settings, owner):
    with factory.begin() as session:
        lock = serialize(session)
        instant = now()
        if lock.lease_until and lock.lease_until > instant:
            return None
        # Recover from process death without relying on queue redelivery.
        expired = list(
            session.scalars(select(Run).where(Run.status == "running", Run.lease_until < instant))
        )
        for run in expired:
            if run.cancel_requested:
                run.status = "cancelled"
                run.finished_at = instant
                add_event(session, run.id, "cancelled", {"status": "cancelled"})
            elif run.attempts >= settings.max_run_attempts:
                run.status = "failed"
                run.error = "Research worker retry limit reached"
                run.finished_at = instant
                add_event(session, run.id, "failed", {"error": run.error})
            else:
                run.status = "queued"
                run.lease_owner = None
                add_event(
                    session,
                    run.id,
                    "progress",
                    {"message": "Recovering research after worker interruption"},
                )
        session.flush()
        candidates = list(
            session.scalars(
                select(Run).where(Run.status == "queued").order_by(Run.created_at, Run.id)
            )
        )
        for run in candidates:
            user = session.get(User, run.user_id)
            if run.cancel_requested or user is None or not user.approved:
                run.status = "cancelled"
                run.finished_at = instant
                add_event(session, run.id, "cancelled", {"status": "cancelled"})
                continue
            run.status = "running"
            run.attempts += 1
            run.lease_owner = owner
            run.started_at = run.started_at or instant
            run.lease_until = instant + timedelta(seconds=settings.lease_seconds)
            lock.owner = owner
            lock.lease_until = run.lease_until
            add_event(session, run.id, "started", {"status": "running"})
            return run.id
        lock.owner = None
        lock.lease_until = None
        return None


def heartbeat(factory, settings, run_id, owner):
    with factory.begin() as session:
        lock = serialize(session)
        run = session.get(Run, run_id)
        if (
            run is None
            or run.lease_owner != owner
            or lock.owner != owner
            or run.status != "running"
        ):
            return False
        # Do not renew indefinitely if execution is stuck beyond its budget.
        if (
            run.started_at
            and (now() - run.started_at).total_seconds() > settings.run_timeout_seconds
        ):
            run.cancel_requested = True
            return False
        until = now() + timedelta(seconds=settings.lease_seconds)
        run.lease_until = until
        lock.lease_until = until
        return True


def finish_run(factory, run_id, owner, result=None, error=None):
    with factory.begin() as session:
        lock = serialize(session)
        run = session.get(Run, run_id)
        if run is None or run.lease_owner != owner or lock.owner != owner:
            return False  # A recovered worker owns the run; stale writes are fenced.
        if run.status not in TERMINAL:
            if run.cancel_requested:
                run.status = "cancelled"
                add_event(session, run.id, "cancelled", {"status": "cancelled"})
            elif error:
                run.status = "failed"
                run.error = "Research failed; retry after checking model/search availability"
                add_event(session, run.id, "failed", {"error": run.error})
            else:
                run.status = "completed"
                run.result = str(result or "")
                session.add(
                    Message(
                        conversation_id=run.conversation_id, role="assistant", content=run.result
                    )
                )
                add_event(session, run.id, "completed", {"result": run.result})
            run.finished_at = now()
        run.lease_until = None
        run.lease_owner = None
        lock.owner = None
        lock.lease_until = None
        return True


def process_one(factory, settings, execute=None):
    owner = str(uuid4())
    run_id = claim_run(factory, settings, owner)
    if run_id is None:
        return False
    if execute is None:
        from .research import execute_research

        execute = execute_research
    stop = threading.Event()

    def renew():
        while not stop.wait(settings.lease_seconds / 3):
            try:
                if not heartbeat(factory, settings, run_id, owner):
                    return
            except Exception:
                logger.warning("Lease renewal unavailable; execution must respect fencing")
                return

    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        result = execute(run_id, factory, settings)
        finish_run(factory, run_id, owner, result=result)
    except Exception:
        finish_run(factory, run_id, owner, error=True)
    finally:
        stop.set()
        thread.join(timeout=2)
    return True


def process_document(factory, settings):
    with factory.begin() as session:
        serialize(session)
        document = session.scalar(
            select(Document)
            .where(
                (Document.status == "pending")
                | ((Document.status == "indexing") & (Document.lease_until < now()))
            )
            .order_by(Document.created_at)
            .limit(1)
        )
        if document is None:
            return False
        document.status = "indexing"
        document.lease_until = now() + timedelta(seconds=600)
        document.lease_owner = str(uuid4())
        document_id = document.id
        token = document.lease_owner
    ingest(document_id, factory, settings, lease_owner=token)
    return True


def main():
    settings = Settings()
    factory = create_session_factory(settings)
    require_schema(factory)
    stop = threading.Event()

    def documents():
        while not stop.is_set():
            try:
                if not process_document(factory, settings):
                    stop.wait(1)
            except Exception:
                logger.warning("Document processing temporarily unavailable")
                stop.wait(3)

    thread = threading.Thread(target=documents, daemon=True)
    thread.start()
    sqs = None
    if settings.sqs_queue_url:
        import boto3

        sqs = boto3.client("sqs", region_name=settings.aws_region)
    try:
        while not stop.is_set():
            receipt = None
            if sqs:
                try:
                    messages = sqs.receive_message(
                        QueueUrl=settings.sqs_queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=1
                    ).get("Messages", [])
                    receipt = messages[0]["ReceiptHandle"] if messages else None
                except Exception:
                    logger.warning("SQS unavailable; polling durable queue")
            worked = process_one(factory, settings)
            if sqs and receipt:
                try:
                    sqs.delete_message(QueueUrl=settings.sqs_queue_url, ReceiptHandle=receipt)
                except Exception:
                    logger.warning("Queue acknowledgement failed; duplicate hints are safe")
            if not worked:
                stop.wait(1)
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
