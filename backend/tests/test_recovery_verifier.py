import copy
import uuid

import pytest
from research_app.config import Settings
from research_app.models import (
    Attachment,
    Base,
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
from research_app.recovery_verifier import (
    VerificationError,
    _factory,
    capture_manifest,
    rehearse_source_and_restore,
    verify_restored_fixture,
)
from research_app.storage import Storage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

CONFIG = {
    "environment": "staging",
    "session_id": "session-1",
    "database_identifier": "deepresearch-staging",
    "region": "us-east-1",
    "account_id": "123456789012",
}
BACKUP = {
    "operation": "backup-receipt",
    "config": CONFIG,
    "database_snapshot": {
        "identifier": "deepresearch-staging-session-1-backup",
        "arn": "arn:aws:rds:us-east-1:123456789012:snapshot:deepresearch-staging-session-1-backup",
    },
}
RESTORE = {
    "operation": "database-restore-receipt",
    "config": CONFIG,
    "source_snapshot_identifier": BACKUP["database_snapshot"]["identifier"],
    "source_snapshot_arn": BACKUP["database_snapshot"]["arn"],
    "target_database_identifier": "deepresearch-staging-restore-session-1",
    "target_database_arn": "arn:aws:rds:us-east-1:123456789012:db:deepresearch-staging-restore-session-1",
    "target_database_endpoint": "deepresearch-staging-restore-session-1.abcdefghijkl.us-east-1.rds.amazonaws.com",
    "target_database_port": 5432,
    "provider_verified": True,
    "database_restore_verified": True,
}
USER_EMAIL = "recovery+session-1@example.test"
CONVERSATION_ID = "11111111-1111-4111-8111-111111111111"
DOCUMENT_ID = "22222222-2222-4222-8222-222222222222"
RUN_ID = "33333333-3333-4333-8333-333333333333"
CHUNK_ID = "44444444-4444-4444-8444-444444444444"
FIXTURE_DOCUMENT_CONTENT = b"Synthetic recovery marker: blue heron archive.\n"


@pytest.fixture
def fixture_db(tmp_path):
    settings = Settings(
        environment="local",
        database_url=f"sqlite:///{tmp_path / 'recovery.sqlite'}",
        model_provider="fake",
        embedding_provider="fake",
        storage_backend="local",
        storage_path=tmp_path / "objects",
    )
    engine = create_engine(settings.database_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        user = User(id="synthetic-subject", email=USER_EMAIL, role="user", approved=True)
        session.add(user)
        session.add(
            Conversation(
                id=CONVERSATION_ID,
                user_id=user.id,
                title="Recovery fixture conversation",
            )
        )
        session.add(
            Document(
                id=DOCUMENT_ID,
                user_id=user.id,
                conversation_id=CONVERSATION_ID,
                name="recovery-fixture.txt",
                media_type="text/plain",
                storage_key="sessions/session-1/recovery-fixture.txt",
                status="ready",
                size_bytes=len(FIXTURE_DOCUMENT_CONTENT),
            )
        )
        session.add(
            Attachment(
                id=str(uuid.uuid4()),
                conversation_id=CONVERSATION_ID,
                document_id=DOCUMENT_ID,
                user_id=user.id,
            )
        )
        session.add(
            Chunk(
                id=CHUNK_ID,
                document_id=DOCUMENT_ID,
                user_id=user.id,
                ordinal=0,
                text="synthetic recovery marker: blue heron archive",
                locator="section 1",
                embedding=[0.0] * 384,
            )
        )
        session.add(
            Run(
                id=RUN_ID,
                user_id=user.id,
                conversation_id=CONVERSATION_ID,
                prompt="Find the blue heron recovery marker",
                status="completed",
                result="The fixture marker is blue heron archive.",
            )
        )
        session.add_all(
            [
                Message(
                    id=str(uuid.uuid4()),
                    conversation_id=CONVERSATION_ID,
                    role="user",
                    content="Find the blue heron recovery marker",
                ),
                Message(
                    id=str(uuid.uuid4()),
                    conversation_id=CONVERSATION_ID,
                    role="assistant",
                    content="The fixture marker is blue heron archive.",
                ),
                Event(run_id=RUN_ID, kind="documents_used", data={"document_ids": [DOCUMENT_ID]}),
                Memory(user_id=user.id, content="Synthetic recovery preference: concise citations."),
                SchemaVersion(version=1),
            ]
        )
    Storage(settings).put(
        "sessions/session-1/recovery-fixture.txt",
        FIXTURE_DOCUMENT_CONTENT,
        "text/plain",
    )
    return settings, factory


def capture(settings, factory):
    return capture_manifest(
        factory,
        settings,
        BACKUP,
        USER_EMAIL,
        CONVERSATION_ID,
        "blue heron archive",
    )


def test_capture_and_verify_reads_exact_synthetic_rows_objects_and_retrieval(fixture_db):
    settings, factory = fixture_db
    manifest = capture(settings, factory)

    assert manifest["operation"] == "synthetic-recovery-fixture"
    assert manifest["fixture"]["row_counts"]["messages"] == 2
    assert manifest["fixture"]["document_objects"][DOCUMENT_ID]["size_bytes"] > 0
    assert len(manifest["fixture"]["table_sha256"]["chunks"]) == 64
    assert "The fixture marker" not in str(manifest)

    target_settings = copy.copy(settings)
    target_settings.database_url = (
        f"postgresql+psycopg://fixture@{RESTORE['target_database_endpoint']}:5432/research"
    )
    receipt = verify_restored_fixture(factory, target_settings, manifest, RESTORE)
    assert receipt["application_data_verified"] is True
    assert receipt["application_restore_rehearsed"] is False
    assert receipt["retrieval_verified"] is True
    assert receipt["full_recovery_accepted"] is False
    assert receipt["clickhouse_restore_verified"] is False
    assert receipt["valkey_restore_verified"] is False


def test_rehearse_captures_paused_source_then_compares_exact_restore(fixture_db):
    settings, factory = fixture_db
    receipt = rehearse_source_and_restore(
        factory,
        factory,
        settings,
        BACKUP,
        RESTORE,
        USER_EMAIL,
        CONVERSATION_ID,
        "blue heron archive",
        RESTORE["target_database_endpoint"],
    )
    assert receipt["application_data_verified"] is True
    assert receipt["fixture_manifest_sha256"]
    assert receipt["restore_rehearsed"] is False


def test_verify_refuses_changed_synthetic_records_and_does_not_mutate(fixture_db):
    settings, factory = fixture_db
    manifest = capture(settings, factory)
    target_settings = copy.copy(settings)
    target_settings.database_url = (
        f"postgresql+psycopg://fixture@{RESTORE['target_database_endpoint']}:5432/research"
    )
    with factory.begin() as session:
        row = session.get(Message, session.scalars(select(Message.id)).first())
        row.content = "tampered synthetic record"
    with pytest.raises(VerificationError, match="table_sha256"):
        verify_restored_fixture(factory, target_settings, manifest, RESTORE)


def test_verify_requires_receipt_for_exact_restored_endpoint(fixture_db):
    settings, factory = fixture_db
    manifest = capture(settings, factory)
    target_settings = copy.copy(settings)
    target_settings.database_url = (
        f"postgresql+psycopg://fixture@{RESTORE['target_database_endpoint']}:5432/research"
    )
    wrong = copy.deepcopy(RESTORE)
    wrong["target_database_endpoint"] = "different.rds.amazonaws.com"
    with pytest.raises(VerificationError, match="exact isolated staging clone"):
        verify_restored_fixture(factory, target_settings, manifest, wrong)


def test_capture_rejects_non_synthetic_identity(fixture_db):
    settings, factory = fixture_db
    with pytest.raises(VerificationError, match="reserved synthetic"):
        capture_manifest(
            factory,
            settings,
            BACKUP,
            "real@example.com",
            CONVERSATION_ID,
            "blue heron archive",
        )


def test_verifier_endpoint_override_changes_only_the_postgres_host(fixture_db):
    settings, _ = fixture_db
    settings.database_url = "postgresql+psycopg://fixture:secret@source.rds.amazonaws.com:5432/research"
    endpoint = RESTORE["target_database_endpoint"]
    factory = _factory(settings, endpoint)
    bind = factory.kw["bind"]
    assert bind.url.host == endpoint
    assert bind.url.port == 5432
    assert bind.url.password == "secret"
    assert bind.url.database == "research"
    assert bind.url.query == {}
