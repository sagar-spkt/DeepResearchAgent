import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import delete, func, select, text

from .auth import admin_user, approved_user, current_user, verified_profile_email
from .config import Settings
from .db import add_event, create_session_factory, require_schema
from .limits import RequestBodyLimit
from .models import (
    Attachment,
    Chunk,
    Conversation,
    Document,
    Event,
    Memory,
    Message,
    Run,
    User,
    WorkerLock,
    now,
)
from .schemas import ConversationInput, IdentityInput, MemoryInput, RunInput, UserUpdate
from .storage import Storage

TERMINAL = {"completed", "failed", "cancelled"}


def row_dict(row, fields):
    return {field: getattr(row, field) for field in fields.split()}


def user_dict(user):
    return row_dict(user, "id email role approved")


def run_dict(run, session=None):
    result = row_dict(run, "id conversation_id prompt status result error created_at")
    used = set()
    if session is not None:
        for data in session.scalars(
            select(Event.data).where(Event.run_id == run.id, Event.kind == "documents_used")
        ):
            used.update(data.get("document_ids", []))
    result["used_document_ids"] = sorted(used)
    return result


def document_dict(document):
    return row_dict(document, "id name media_type status error conversation_id created_at")


def owned(session, model, object_id, user):
    row = session.get(model, object_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(404, "Not found")
    return row


def serialize(session):
    # PostgreSQL row lock; SQLite write-lock for isolated local/test use.
    if session.bind.dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
    return session.scalar(select(WorkerLock).where(WorkerLock.id == 1).with_for_update())


def create_app(settings=None, session_factory=None):
    settings = settings or Settings()
    factory = session_factory or create_session_factory(settings)

    @asynccontextmanager
    async def lifespan(_app):
        require_schema(factory)
        yield

    app = FastAPI(title="Deep Research Agent API", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.session_factory = factory
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "X-Dev-User", "Last-Event-ID"],
    )
    app.add_middleware(RequestBodyLimit, upload_limit=settings.max_upload_bytes + 1024 * 1024)

    @app.get("/health/live")
    @app.get("/healthz")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    @app.get("/readyz")
    def ready():
        require_schema(factory)
        return {"status": "ok"}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return user_dict(user)

    @app.post("/api/me/identity")
    def bind_identity(body: IdentityInput, user: User = Depends(current_user)):
        if settings.environment == "local":
            raise HTTPException(400, "Local identity does not accept Cognito profile tokens")
        email = verified_profile_email(body.id_token, user, settings)
        with factory.begin() as session:
            stored = session.get(User, user.id)
            if stored is None:
                raise HTTPException(401, "Identity no longer exists")
            stored.email = email
            return user_dict(stored)

    @app.get("/api/conversations")
    def conversations(user: User = Depends(current_user)):
        with factory() as session:
            return [
                row_dict(row, "id title created_at")
                for row in session.scalars(
                    select(Conversation)
                    .where(Conversation.user_id == user.id)
                    .order_by(Conversation.created_at.desc())
                )
            ]

    @app.post("/api/conversations", status_code=201)
    def create_conversation(body: ConversationInput, user: User = Depends(current_user)):
        with factory.begin() as session:
            row = Conversation(user_id=user.id, title=body.title.strip())
            session.add(row)
            session.flush()
            return row_dict(row, "id title created_at")

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, user: User = Depends(current_user)):
        with factory() as session:
            row = owned(session, Conversation, conversation_id, user)
            result = row_dict(row, "id title created_at")
            result["messages"] = [
                row_dict(message, "id role content created_at")
                for message in session.scalars(
                    select(Message)
                    .where(Message.conversation_id == row.id)
                    .order_by(Message.created_at, Message.id)
                )
            ]
            result["documents"] = [
                document_dict(doc)
                for doc in session.scalars(
                    select(Document)
                    .join(Attachment, Attachment.document_id == Document.id)
                    .where(
                        Attachment.conversation_id == row.id,
                        Attachment.user_id == user.id,
                        Document.user_id == user.id,
                    )
                )
            ]
            runs = list(
                session.scalars(
                    select(Run).where(Run.conversation_id == row.id).order_by(Run.created_at.desc())
                )
            )
            result["active_run"] = next(
                (run_dict(run, session) for run in runs if run.status not in TERMINAL), None
            )
            result["latest_run"] = run_dict(runs[0], session) if runs else None
            return result

    @app.delete("/api/conversations/{conversation_id}", status_code=204)
    def delete_conversation(conversation_id: str, user: User = Depends(current_user)):
        with factory.begin() as session:
            serialize(session)
            row = owned(session, Conversation, conversation_id, user)
            runs = list(session.scalars(select(Run).where(Run.conversation_id == row.id)))
            if any(run.status not in TERMINAL for run in runs):
                raise HTTPException(409, "Cancel active research before deleting this conversation")
            # Deletion is retryable: on checkpoint-store failure relational deletion rolls back.
            from .research import checkpoints

            with checkpoints(settings) as saver:
                saver.delete_thread(f"{user.id}:{row.id}")
            ids = [run.id for run in runs]
            if ids:
                session.execute(delete(Event).where(Event.run_id.in_(ids)))
            session.execute(delete(Run).where(Run.conversation_id == row.id))
            session.execute(delete(Message).where(Message.conversation_id == row.id))
            session.execute(delete(Attachment).where(Attachment.conversation_id == row.id))
            # Documents remain in the owner's private library.
            for document in session.scalars(
                select(Document).where(
                    Document.conversation_id == row.id, Document.user_id == user.id
                )
            ):
                document.conversation_id = None
            session.delete(row)
        return Response(status_code=204)

    @app.post("/api/conversations/{conversation_id}/runs", status_code=202)
    def create_run(conversation_id: str, body: RunInput, user: User = Depends(approved_user)):
        with factory.begin() as session:
            serialize(session)
            fresh_user = session.get(User, user.id)
            if not fresh_user.approved:
                raise HTTPException(403, "Administrator approval required")
            owned(session, Conversation, conversation_id, user)
            if session.scalar(
                select(Run.id)
                .where(
                    Run.conversation_id == conversation_id, Run.status.in_(["queued", "running"])
                )
                .limit(1)
            ):
                raise HTTPException(409, "This conversation already has active research")
            count = session.scalar(
                select(func.count()).select_from(Run).where(Run.status.in_(["queued", "running"]))
            )
            if count >= settings.max_queue_size:
                raise HTTPException(429, "Research queue is full; try again later")
            run = Run(user_id=user.id, conversation_id=conversation_id, prompt=body.prompt.strip())
            session.add(run)
            session.flush()
            session.add(Message(conversation_id=conversation_id, role="user", content=run.prompt))
            add_event(session, run.id, "queued", {"status": "queued"})
            result = run_dict(run)
        # DB is source of truth. SQS is a wakeup hint; worker sweeps DB after outages.
        if settings.sqs_queue_url:
            try:
                import boto3

                boto3.client("sqs", region_name=settings.aws_region).send_message(
                    QueueUrl=settings.sqs_queue_url,
                    MessageBody=json.dumps({"run_id": result["id"]}),
                )
            except Exception:
                pass  # Durable queued run remains available to worker polling.
        return result

    @app.get("/api/runs/{run_id}")
    def run(run_id: str, user: User = Depends(current_user)):
        with factory() as session:
            return run_dict(owned(session, Run, run_id, user), session)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, user: User = Depends(current_user)):
        with factory.begin() as session:
            serialize(session)
            run = owned(session, Run, run_id, user)
            if run.status not in TERMINAL:
                run.cancel_requested = True
                if run.status == "queued":
                    run.status = "cancelled"
                    run.finished_at = now()
                    add_event(session, run.id, "cancelled", {"status": "cancelled"})
            return run_dict(run)

    @app.get("/api/runs/{run_id}/events")
    async def events(
        run_id: str,
        request: Request,
        after: int = 0,
        last_event_id: Annotated[str | None, Header()] = None,
        user: User = Depends(current_user),
    ):
        with factory() as session:
            owned(session, Run, run_id, user)
        try:
            cursor = max(0, int(last_event_id) if last_event_id else after)
        except ValueError as exc:
            raise HTTPException(400, "Invalid Last-Event-ID") from exc

        async def stream():
            nonlocal cursor
            # Bound each connection; clients reconnect with the last persisted event ID.
            for _ in range(600):
                if await request.is_disconnected():
                    return
                with factory() as session:
                    run = session.get(Run, run_id)
                    if run is None or run.user_id != user.id:
                        return
                    batch = list(
                        session.scalars(
                            select(Event)
                            .where(Event.run_id == run_id, Event.id > cursor)
                            .order_by(Event.id)
                            .limit(100)
                        )
                    )
                    status = run.status
                    for event in batch:
                        cursor = event.id
                        yield f"id: {event.id}\nevent: {event.kind}\ndata: {json.dumps(event.data)}\n\n"
                if status in TERMINAL and len(batch) < 100:
                    return
                if not batch:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/documents")
    def documents(user: User = Depends(current_user)):
        with factory() as session:
            return [
                document_dict(row)
                for row in session.scalars(
                    select(Document)
                    .where(Document.user_id == user.id)
                    .order_by(Document.created_at.desc())
                )
            ]

    @app.post("/api/documents", status_code=202)
    async def upload(
        file: UploadFile = File(...),
        conversation_id: str | None = Form(None),
        user: User = Depends(current_user),
    ):
        name = Path(file.filename or "document").name
        extension = name.rsplit(".", 1)[-1].lower()
        types = {
            "pdf": "application/pdf",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "txt": "text/plain",
            "md": "text/markdown",
        }
        if extension not in types or len(name) > 255:
            raise HTTPException(415, "Use PDF, DOCX, UTF-8 TXT or Markdown")
        content = await file.read(settings.max_upload_bytes + 1)
        if not content or len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "File is empty or exceeds the 20 MB limit")
        with factory.begin() as session:
            serialize(session)
            size, count = session.execute(
                select(func.coalesce(func.sum(Document.size_bytes), 0), func.count()).where(
                    Document.user_id == user.id
                )
            ).one()
            if (
                count >= settings.max_user_documents
                or size + len(content) > settings.max_user_storage_bytes
            ):
                raise HTTPException(429, "Private document library quota reached")
            if conversation_id:
                owned(session, Conversation, conversation_id, user)
            document_id = str(uuid4())
            key = f"{user.id}/{document_id}/original"
            storage = Storage(settings)
            storage.put(key, content, types[extension])
            row = Document(
                id=document_id,
                user_id=user.id,
                conversation_id=conversation_id,
                name=name,
                media_type=types[extension],
                storage_key=key,
                size_bytes=len(content),
            )
            session.add(row)
            session.flush()
            if conversation_id:
                session.add(
                    Attachment(
                        user_id=user.id, conversation_id=conversation_id, document_id=document_id
                    )
                )
            try:
                session.flush()
            except Exception:
                storage.delete(key)
                raise
            return document_dict(row)

    @app.post("/api/conversations/{conversation_id}/documents/{document_id}")
    def attach_document(conversation_id: str, document_id: str, user: User = Depends(current_user)):
        with factory.begin() as session:
            serialize(session)
            owned(session, Conversation, conversation_id, user)
            document = owned(session, Document, document_id, user)
            if (
                session.scalar(
                    select(Attachment.id).where(
                        Attachment.conversation_id == conversation_id,
                        Attachment.document_id == document_id,
                    )
                )
                is None
            ):
                session.add(
                    Attachment(
                        user_id=user.id, conversation_id=conversation_id, document_id=document_id
                    )
                )
            return document_dict(document)

    @app.delete("/api/documents/{document_id}", status_code=204)
    def delete_document(document_id: str, user: User = Depends(current_user)):
        with factory.begin() as session:
            serialize(session)
            document = owned(session, Document, document_id, user)
            Storage(settings).delete(document.storage_key)
            session.execute(delete(Chunk).where(Chunk.document_id == document_id))
            session.execute(delete(Attachment).where(Attachment.document_id == document_id))
            session.delete(document)
        return Response(status_code=204)

    @app.get("/api/memories")
    def memories(user: User = Depends(current_user)):
        with factory() as session:
            return [
                row_dict(row, "id content created_at")
                for row in session.scalars(
                    select(Memory).where(Memory.user_id == user.id).order_by(Memory.created_at)
                )
            ]

    @app.post("/api/memories", status_code=201)
    def save_memory(body: MemoryInput, user: User = Depends(current_user)):
        with factory.begin() as session:
            row = Memory(user_id=user.id, content=body.content.strip())
            session.add(row)
            session.flush()
            return row_dict(row, "id content created_at")

    @app.patch("/api/memories/{memory_id}")
    def edit_memory(memory_id: str, body: MemoryInput, user: User = Depends(current_user)):
        with factory.begin() as session:
            row = owned(session, Memory, memory_id, user)
            row.content = body.content.strip()
            return row_dict(row, "id content created_at")

    @app.delete("/api/memories/{memory_id}", status_code=204)
    def delete_memory(memory_id: str, user: User = Depends(current_user)):
        with factory.begin() as session:
            session.delete(owned(session, Memory, memory_id, user))
        return Response(status_code=204)

    @app.get("/api/admin/users")
    def users(user: User = Depends(admin_user)):
        with factory() as session:
            return [
                user_dict(row) for row in session.scalars(select(User).order_by(User.created_at))
            ]

    @app.patch("/api/admin/users/{user_id}")
    def update_user(user_id: str, body: UserUpdate, user: User = Depends(admin_user)):
        with factory.begin() as session:
            serialize(session)
            actor = session.get(User, user.id)
            if not actor.approved or actor.role not in {"owner", "admin"}:
                raise HTTPException(403, "Administrator access required")
            target = session.get(User, user_id)
            if target is None:
                raise HTTPException(404, "Not found")
            if actor.role != "owner" and (
                body.role is not None or target.role in {"owner", "admin"}
            ):
                raise HTTPException(403, "Only owners can manage administrators")
            proposed_role = body.role if body.role is not None else target.role
            proposed_approved = body.approved if body.approved is not None else target.approved
            if (
                target.role == "owner"
                and target.approved
                and (proposed_role != "owner" or not proposed_approved)
            ):
                count = session.scalar(
                    select(func.count())
                    .select_from(User)
                    .where(User.role == "owner", User.approved.is_(True))
                )
                if count <= 1:
                    raise HTTPException(409, "Cannot remove the last approved owner")
            target.role = proposed_role
            target.approved = proposed_approved
            return user_dict(target)

    @app.get("/api/admin/traces")
    async def traces(user: User = Depends(admin_user)):
        if not (
            settings.langfuse_base_url
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        ):
            raise HTTPException(503, "Langfuse metadata access is not configured")
        import httpx

        try:
            async with httpx.AsyncClient(follow_redirects=False, timeout=10) as client:
                response = await client.get(
                    settings.langfuse_base_url.rstrip("/") + "/api/public/traces",
                    params={"limit": 20},
                    auth=(settings.langfuse_public_key, settings.langfuse_secret_key),
                )
                response.raise_for_status()
                data = response.json()
            return {
                "items": [
                    {
                        key: row.get(key)
                        for key in ("id", "name", "timestamp", "latency", "metadata")
                    }
                    for row in data.get("data", [])
                ],
                "console_available": bool(settings.observability_console_url),
                "console_url": settings.observability_console_url or None,
            }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise HTTPException(503, "Langfuse metadata is temporarily unavailable") from exc

    @app.get("/api/admin/access", status_code=204)
    def administrator_access(user: User = Depends(admin_user)):
        # Dedicated console proxy authenticates every request through the web BFF.
        return Response(status_code=204)

    @app.api_route(
        "/api/admin/observability/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"]
    )
    def observability(path: str, user: User = Depends(admin_user)):
        # A subpath proxy cannot safely serve the native console without tested
        # cookie/base-path/asset handling. Keep it closed until isolated ingress is verified.
        raise HTTPException(
            503, "Native Langfuse console gateway is not enabled; use the administrator trace view"
        )

    return app
