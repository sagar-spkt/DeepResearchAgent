"""Capture and verify a synthetic application fixture against an isolated RDS restore."""

import argparse
import hashlib
import json
import os
import re
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .models import (
    Attachment,
    Chunk,
    Conversation,
    Document,
    Event,
    Memory,
    Message,
    Run,
    SchemaVersion,
    User,
)
from .retrieval import retrieve
from .storage import Storage


class VerificationError(ValueError):
    """The restored application data did not match the synthetic source fixture."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _scalar(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=False))
    return value


def _rows(session, model, statement):
    rows = [
        {
            column.name: _scalar(getattr(row, column.name))
            for column in model.__table__.columns
        }
        for row in session.scalars(statement).all()
    ]
    primary_key = [column.name for column in model.__table__.primary_key.columns]
    rows.sort(key=lambda row: canonical([row[name] for name in primary_key]))
    return rows


def _fixture_snapshot(factory, settings, user_email, conversation_id, query):
    if not isinstance(query, str) or not query.strip():
        raise VerificationError("Synthetic recovery query must not be empty")
    if not re.fullmatch(r"recovery\+[a-z0-9-]+@example\.test", user_email):
        raise VerificationError("Recovery fixture must use its reserved synthetic .test account")
    with factory() as session:
        users = list(session.scalars(select(User).where(User.email == user_email)))
        if len(users) != 1:
            raise VerificationError("Synthetic recovery account was not unique")
        user = users[0]
        if user.role != "user" or not user.approved:
            raise VerificationError("Synthetic recovery account must be an approved ordinary user")
        conversations = list(
            session.scalars(
                select(Conversation).where(
                    Conversation.id == conversation_id,
                    Conversation.user_id == user.id,
                )
            )
        )
        if len(conversations) != 1:
            raise VerificationError("Synthetic recovery conversation is not owned by its user")
        user_id = user.id
        document_ids = list(
            session.scalars(select(Document.id).where(Document.user_id == user_id)).all()
        )
        run_ids = list(
            session.scalars(
                select(Run.id).where(
                    Run.user_id == user_id,
                    Run.conversation_id == conversation_id,
                )
            ).all()
        )
        statements = {
            "users": (User, select(User).where(User.id == user_id)),
            "conversations": (
                Conversation,
                select(Conversation).where(Conversation.id == conversation_id),
            ),
            "messages": (
                Message,
                select(Message).where(Message.conversation_id == conversation_id),
            ),
            "runs": (
                Run,
                select(Run).where(Run.id.in_(run_ids)),
            ),
            "events": (Event, select(Event).where(Event.run_id.in_(run_ids))),
            "documents": (
                Document,
                select(Document).where(Document.id.in_(document_ids)),
            ),
            "attachments": (
                Attachment,
                select(Attachment).where(
                    Attachment.user_id == user_id,
                    Attachment.conversation_id == conversation_id,
                ),
            ),
            "chunks": (
                Chunk,
                select(Chunk).where(Chunk.user_id == user_id, Chunk.document_id.in_(document_ids)),
            ),
            "memories": (Memory, select(Memory).where(Memory.user_id == user_id)),
            "schema_versions": (SchemaVersion, select(SchemaVersion)),
        }
        records = {name: _rows(session, model, statement) for name, (model, statement) in statements.items()}
        if any(not records[name] for name in ("messages", "runs", "events", "documents", "attachments", "chunks", "memories")):
            raise VerificationError("Synthetic fixture must include messages, a completed run, a ready attached document, chunks, and a saved memory")
        if not any(row["status"] == "completed" and row["result"] for row in records["runs"]):
            raise VerificationError("Synthetic fixture must include a completed research result")
        if not any(row["status"] == "ready" for row in records["documents"]):
            raise VerificationError("Synthetic fixture must include a ready document")
        attached = {row["document_id"] for row in records["attachments"]}
        ready = {row["id"] for row in records["documents"] if row["status"] == "ready"}
        if not (attached & ready):
            raise VerificationError("Synthetic ready document is not attached to the test conversation")

    storage = Storage(settings)
    objects = {}
    for row in records["documents"]:
        body = storage.get(row["storage_key"])
        if len(body) != row["size_bytes"]:
            raise VerificationError("Synthetic document object size differs from its database record")
        objects[row["id"]] = {"sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body)}

    results = retrieve(factory, settings, user_id, conversation_id, query, limit=5)
    retrieved = [
        {
            "document_id": item["document_id"],
            "locator": item["locator"],
            "text_sha256": hashlib.sha256(item["text"].encode()).hexdigest(),
        }
        for item in results
    ]
    retrieved.sort(key=lambda item: (item["document_id"], item["locator"], item["text_sha256"]))
    if not any(item["document_id"] in attached for item in retrieved):
        raise VerificationError("Synthetic query did not retrieve an explicitly attached document")

    table_hashes = {name: digest(rows) for name, rows in records.items()}
    return {
        "user_id": user_id,
        "user_email": user_email,
        "conversation_id": conversation_id,
        "row_counts": {name: len(rows) for name, rows in records.items()},
        "table_sha256": table_hashes,
        "document_objects": objects,
        "retrieval": retrieved,
    }


def capture_manifest(factory, settings, backup_receipt, user_email, conversation_id, query):
    config = backup_receipt.get("config", {})
    snapshot = backup_receipt.get("database_snapshot", {})
    if (
        backup_receipt.get("operation") != "backup-receipt"
        or config.get("environment") != "staging"
        or not config.get("session_id")
        or not snapshot.get("arn")
        or not snapshot.get("identifier")
    ):
        raise VerificationError("Fixture capture requires a staging backup receipt with an RDS snapshot")
    fixture = _fixture_snapshot(factory, settings, user_email, conversation_id, query)
    return {
        "version": 1,
        "operation": "synthetic-recovery-fixture",
        "config": config,
        "source_snapshot_identifier": snapshot["identifier"],
        "source_snapshot_arn": snapshot["arn"],
        "backup_receipt_sha256": digest(backup_receipt),
        "query": query,
        "fixture": fixture,
    }


def verify_restored_fixture(factory, settings, manifest, restore_receipt, target_endpoint=None):
    config = manifest.get("config", {})
    expected = manifest.get("fixture", {})
    target = restore_receipt.get("target_database_identifier")
    endpoint = restore_receipt.get("target_database_endpoint")
    port = restore_receipt.get("target_database_port")
    url = make_url(settings.database_url)
    expected_target = f"{config.get('database_identifier', '')}-restore-{config.get('session_id', '')}"
    if (
        manifest.get("operation") != "synthetic-recovery-fixture"
        or manifest.get("version") != 1
        or config.get("environment") != "staging"
        or restore_receipt.get("operation") != "database-restore-receipt"
        or restore_receipt.get("config") != config
        or restore_receipt.get("database_restore_verified") is not True
        or restore_receipt.get("provider_verified") is not True
        or restore_receipt.get("source_snapshot_identifier") != manifest.get("source_snapshot_identifier")
        or restore_receipt.get("source_snapshot_arn") != manifest.get("source_snapshot_arn")
        or target != expected_target
        or restore_receipt.get("target_database_arn")
        != f"arn:aws:rds:{config.get('region')}:{config.get('account_id')}:db:{target}"
        or not isinstance(endpoint, str)
        or not re.fullmatch(r"[a-zA-Z0-9.-]+\.rds\.amazonaws\.com", endpoint)
        or port != 5432
        or (target_endpoint is None and url.host != endpoint)
        or (target_endpoint is not None and target_endpoint != endpoint)
        or url.port not in (None, 5432)
    ):
        raise VerificationError("Restore receipt or connection does not identify the exact isolated staging clone")

    actual = _fixture_snapshot(
        factory,
        settings,
        expected.get("user_email", ""),
        expected.get("conversation_id", ""),
        manifest.get("query", ""),
    )
    for key in ("user_id", "user_email", "conversation_id", "row_counts", "table_sha256", "document_objects", "retrieval"):
        if actual.get(key) != expected.get(key):
            raise VerificationError(f"Restored synthetic fixture differs in {key}")
    return {
        "version": 1,
        "operation": "application-recovery-verification-receipt",
        "config": config,
        "source_snapshot_identifier": manifest["source_snapshot_identifier"],
        "source_snapshot_arn": manifest["source_snapshot_arn"],
        "backup_receipt_sha256": manifest["backup_receipt_sha256"],
        "target_database_identifier": target,
        "target_database_endpoint": endpoint,
        "fixture_manifest_sha256": digest(manifest),
        "restore_receipt_sha256": digest(restore_receipt),
        "fixture_sha256": digest(manifest["fixture"]),
        "database_restore_verified": True,
        "application_data_verified": True,
        "document_objects_verified": True,
        "retrieval_verified": True,
        "application_restore_rehearsed": False,
        "clickhouse_restore_verified": False,
        "valkey_restore_verified": False,
        "observability_restore_verified": False,
        "restore_rehearsed": False,
        "full_recovery_accepted": False,
        "remaining_steps": [
            "restore and compare ClickHouse observability data",
            "restore Valkey and verify pending-work behavior",
            "verify trace history and application observability semantics",
        ],
    }


def rehearse_source_and_restore(
    source_factory, target_factory, settings, backup_receipt, restore_receipt,
    user_email, conversation_id, query, target_endpoint,
):
    """Capture the paused source fixture and compare it to the exact restore."""
    manifest = capture_manifest(
        source_factory, settings, backup_receipt, user_email, conversation_id, query
    )
    return verify_restored_fixture(
        target_factory, settings, manifest, restore_receipt, target_endpoint
    )


def _load_json(path):
    try:
        value = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError("Could not read a valid JSON recovery artifact") from exc
    if not isinstance(value, dict):
        raise VerificationError("Recovery artifacts must be JSON objects")
    return value


def _write_private(path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")


def _factory(settings, target_endpoint=None):
    url = make_url(settings.database_url)
    if target_endpoint is not None:
        if not url.drivername.startswith("postgresql") or url.port not in (None, 5432):
            raise VerificationError("Verifier requires the protected PostgreSQL application credential")
        url = url.set(host=target_endpoint, port=5432)
    options = {"connect_args": {}}
    if url.drivername.startswith("postgresql"):
        options["connect_args"]["options"] = "-c default_transaction_read_only=on"
    engine = create_engine(url, **options)
    return sessionmaker(bind=engine, expire_on_commit=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture = commands.add_parser("capture")
    capture.add_argument("--backup-receipt", required=True)
    capture.add_argument("--user-email", required=True)
    capture.add_argument("--conversation-id", required=True)
    capture.add_argument("--query", required=True)
    capture.add_argument("--output", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--manifest", required=True)
    verify.add_argument("--restore-receipt", required=True)
    verify.add_argument("--output", required=True)
    rehearse = commands.add_parser("rehearse")
    rehearse.add_argument("--backup-receipt", required=True)
    rehearse.add_argument("--restore-receipt", required=True)
    rehearse.add_argument("--request", required=True)
    rehearse.add_argument("--output", required=True)
    args = parser.parse_args()
    settings = Settings()
    if args.command == "capture":
        factory = _factory(settings)
        manifest = capture_manifest(
            factory,
            settings,
            _load_json(args.backup_receipt),
            args.user_email,
            args.conversation_id,
            args.query,
        )
        _write_private(args.output, manifest)
        print(json.dumps({"artifact": args.output, "sha256": digest(manifest)}))
    elif args.command == "verify":
        target_endpoint = os.environ.get("RECOVERY_TARGET_DATABASE_ENDPOINT")
        factory = _factory(settings, target_endpoint)
        receipt = verify_restored_fixture(
            factory,
            settings,
            _load_json(args.manifest),
            _load_json(args.restore_receipt),
            target_endpoint,
        )
        _write_private(args.output, receipt)
        print(json.dumps({"artifact": args.output, "sha256": digest(receipt)}))
    else:
        target_endpoint = os.environ.get("RECOVERY_TARGET_DATABASE_ENDPOINT")
        if not target_endpoint:
            raise VerificationError("Recovery rehearsal requires the exact target endpoint")
        backup_receipt = _load_json(args.backup_receipt)
        restore_receipt = _load_json(args.restore_receipt)
        request = _load_json(args.request)
        source_factory = _factory(settings)
        target_factory = _factory(settings, target_endpoint)
        receipt = rehearse_source_and_restore(
            source_factory,
            target_factory,
            settings,
            backup_receipt,
            restore_receipt,
            request.get("user_email", ""),
            request.get("conversation_id", ""),
            request.get("query", ""),
            target_endpoint,
        )
        _write_private(args.output, receipt)
        print(json.dumps({"artifact": args.output, "sha256": digest(receipt)}))


if __name__ == "__main__":
    main()
