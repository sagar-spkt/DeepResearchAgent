from datetime import timedelta
from io import BytesIO
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from research_app.api import create_app
from research_app.config import Settings
from research_app.db import create_session_factory, migrate
from research_app.documents import DocumentError, parse_document
from research_app.models import Chunk, Document, Run, WorkerLock, now
from research_app.worker import claim_run, finish_run, process_one
from sqlalchemy import select


@pytest.fixture
def service(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/app.db",
        storage_path=tmp_path / "uploads",
        max_upload_bytes=1024,
        model_provider="fake",
        embedding_provider="fake",
    )
    factory = create_session_factory(settings)
    migrate(factory)
    with TestClient(create_app(settings, factory)) as client:
        client.headers["X-Dev-User"] = "demo@example.test"
        yield client, factory, settings


def conversation(client):
    response = client.post("/api/conversations", json={"title": "Test research"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_runtime_settings_require_real_providers_unless_offline_is_explicit(monkeypatch):
    for key in ("MODEL_PROVIDER", "BEDROCK_MODEL_ID", "EMBEDDING_PROVIDER", "EMBEDDING_MODEL"):
        monkeypatch.delenv(key, raising=False)

    with pytest.raises(ValueError, match="BEDROCK_MODEL_ID must be explicitly configured"):
        Settings()

    with pytest.raises(ValueError, match="EMBEDDING_MODEL must be explicitly configured"):
        Settings(model_provider="vllm", vllm_base_url="http://inference/v1", vllm_api_key="test")

    offline = Settings(model_provider="fake", embedding_provider="fake")
    assert (offline.model_provider, offline.embedding_provider) == ("fake", "fake")


def test_identity_approval_and_last_owner(service):
    client, _, _ = service
    owner = client.get("/api/me").json()
    assert owner["role"] == "owner"
    client.headers["X-Dev-User"] = "pending@example.test"
    pending = client.get("/api/me").json()
    cid = conversation(client)
    assert (
        client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Research"}).status_code
        == 403
    )
    assert client.get("/api/admin/users").status_code == 403
    client.headers["X-Dev-User"] = "demo@example.test"
    assert (
        client.patch(f"/api/admin/users/{pending['id']}", json={"approved": True}).status_code
        == 200
    )
    assert client.patch(f"/api/admin/users/{owner['id']}", json={"role": "user"}).status_code == 409
    assert (
        client.patch(f"/api/admin/users/{owner['id']}", json={"approved": False}).status_code == 409
    )
    client.headers["X-Dev-User"] = "pending@example.test"
    assert (
        client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Research"}).status_code
        == 202
    )


def test_server_side_ownership_and_extra_fields(service):
    client, _, _ = service
    cid = conversation(client)
    run = client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Private"}).json()
    memory = client.post("/api/memories", json={"content": "Private preference"}).json()
    doc = client.post(
        "/api/documents",
        files={"file": ("notes.txt", b"Private evidence")},
        data={"conversation_id": cid},
    ).json()
    client.headers["X-Dev-User"] = "other@example.test"
    assert client.get("/api/conversations").json() == []
    assert client.get("/api/documents").json() == []
    assert client.get("/api/memories").json() == []
    assert client.get(f"/api/conversations/{cid}").status_code == 404
    assert client.get(f"/api/runs/{run['id']}").status_code == 404
    assert client.get(f"/api/runs/{run['id']}/events").status_code == 404
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 404
    assert (
        client.patch(f"/api/memories/{memory['id']}", json={"content": "Attack"}).status_code == 404
    )
    own = conversation(client)
    assert client.post(f"/api/conversations/{own}/documents/{doc['id']}").status_code == 404
    assert (
        client.post("/api/memories", json={"content": "X", "user_id": "forged"}).status_code == 422
    )


def test_replay_completed_run_and_cancel_queued(service):
    client, factory, settings = service
    cid = conversation(client)
    run = client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Evidence"}).json()
    assert (
        client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Duplicate"}).status_code
        == 409
    )
    assert process_one(factory, settings, execute=lambda *_: "Cited result [1]")
    result = client.get(f"/api/runs/{run['id']}").json()
    assert result["status"] == "completed"
    events = client.get(f"/api/runs/{run['id']}/events").text
    assert "event: completed" in events
    ids = [
        int(line.removeprefix("id: ")) for line in events.splitlines() if line.startswith("id: ")
    ]
    replay = client.get(
        f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": str(ids[-1])}
    ).text
    assert replay == ""
    messages = client.get(f"/api/conversations/{cid}").json()["messages"]
    assert [message["role"] for message in messages] == ["user", "assistant"]
    run2 = client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Next"}).json()
    assert client.post(f"/api/runs/{run2['id']}/cancel").json()["status"] == "cancelled"
    assert not process_one(factory, settings, execute=lambda *_: pytest.fail("Cancelled work ran"))


def test_single_global_worker_and_expired_lease_recovery(service):
    client, factory, settings = service
    first = conversation(client)
    second = conversation(client)
    run = client.post(f"/api/conversations/{first}/runs", json={"prompt": "One"}).json()
    client.post(f"/api/conversations/{second}/runs", json={"prompt": "Two"})
    assert claim_run(factory, settings, "worker-one") == run["id"]
    assert claim_run(factory, settings, "worker-two") is None
    with factory.begin() as session:
        session.get(WorkerLock, 1).lease_until = now() - timedelta(seconds=1)
        session.get(Run, run["id"]).lease_until = now() - timedelta(seconds=1)
    assert claim_run(factory, settings, "worker-two") == run["id"]
    assert not finish_run(factory, run["id"], "worker-one", result="Stale")
    assert finish_run(factory, run["id"], "worker-two", result="Recovered")
    with factory() as session:
        assert session.get(Run, run["id"]).result == "Recovered"
        assert session.get(Run, run["id"]).attempts == 2


def test_upload_validation_deletion_and_library_reuse(service):
    client, factory, _ = service
    cid = conversation(client)
    assert (
        client.post("/api/documents", files={"file": ("attack.exe", b"content")}).status_code == 415
    )
    assert (
        client.post("/api/documents", files={"file": ("large.txt", b"x" * 1025)}).status_code == 413
    )
    document = client.post(
        "/api/documents",
        files={"file": ("notes.md", b"# Notes\nEvidence")},
        data={"conversation_id": cid},
    ).json()
    cid2 = conversation(client)
    assert client.post(f"/api/conversations/{cid2}/documents/{document['id']}").status_code == 200
    assert client.delete(f"/api/conversations/{cid}").status_code == 204
    assert client.get("/api/documents").json()[0]["conversation_id"] is None
    assert len(client.get(f"/api/conversations/{cid2}").json()["documents"]) == 1
    assert client.delete(f"/api/documents/{document['id']}").status_code == 204
    with factory() as session:
        assert session.get(Document, document["id"]) is None
        assert not list(session.scalars(select(Chunk)))


def test_memory_requires_explicit_api_action_and_revocation_live(service):
    client, _, _ = service
    owner = client.get("/api/me").json()
    client.headers["X-Dev-User"] = "admin@example.test"
    admin = client.get("/api/me").json()
    client.headers["X-Dev-User"] = "demo@example.test"
    client.patch(f"/api/admin/users/{admin['id']}", json={"approved": True, "role": "admin"})
    client.headers["X-Dev-User"] = "admin@example.test"
    assert client.get("/api/admin/users").status_code == 200
    assert (
        client.patch(f"/api/admin/users/{owner['id']}", json={"approved": False}).status_code == 403
    )
    assert client.get("/api/admin/observability/").status_code == 503
    client.headers["X-Dev-User"] = "demo@example.test"
    client.patch(f"/api/admin/users/{admin['id']}", json={"approved": False})
    client.headers["X-Dev-User"] = "admin@example.test"
    assert client.get("/api/admin/observability/").status_code == 403
    assert client.get("/api/memories").json() == []
    memory = client.post("/api/memories", json={"content": "Remember short reports"}).json()
    assert (
        client.patch(f"/api/memories/{memory['id']}", json={"content": "Long reports"}).status_code
        == 200
    )
    assert client.delete(f"/api/memories/{memory['id']}").status_code == 204


def test_cloud_refuses_local_defaults():
    with pytest.raises(ValueError):
        Settings(environment="production")
    with pytest.raises(ValueError):
        Settings(model_provider="bedrock")


def test_cloud_accepts_bedrock_and_rejects_fake_models():
    cloud = dict(
        environment="staging",
        database_url="postgresql+psycopg://u:p@db/research",
        cognito_issuer="https://cognito-idp.us-east-1.amazonaws.com/pool",
        cognito_client_id="client",
        storage_backend="s3",
        s3_bucket="documents",
        embedding_model="/opt/models/bge",
    )
    settings = Settings(model_provider="bedrock", bedrock_model_id="amazon.nova-lite-v1:0", **cloud)
    assert settings.model_provider == "bedrock"
    with pytest.raises(ValueError):
        Settings(model_provider="fake", **cloud)


def test_parsers_reject_unsupported_or_binary_text():
    with pytest.raises(DocumentError):
        parse_document("a.txt", b"\xff")
    with pytest.raises(DocumentError):
        parse_document("a.exe", b"x")
    assert parse_document("a.md", b"# Title\nEvidence")[0][1].endswith("Evidence")
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Evidence</w:t></w:r></w:p></w:body></w:document>',
        )
    assert "Evidence" in parse_document("a.docx", output.getvalue())[0][1]


def test_cognito_access_token_validation_ignores_dev_header(service, monkeypatch):
    import time

    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from research_app import auth

    _, factory, _ = service
    settings = Settings(
        environment="staging",
        database_url="postgresql+psycopg://unused",
        model_provider="vllm",
        vllm_base_url="http://inference/v1",
        vllm_api_key="test-secret",
        embedding_model="/models/bge-small-en-v1.5",
        cognito_issuer="https://cognito-idp.us-east-1.amazonaws.com/test",
        cognito_client_id="client",
        storage_backend="s3",
        s3_bucket="test",
        embedding_provider="sentence-transformers",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class Keys:
        def get_signing_key_from_jwt(self, token):
            class SigningKey:
                pass

            signing = SigningKey()
            signing.key = key.public_key()
            return signing

    monkeypatch.setattr(auth, "key_client", lambda _: Keys())

    def token(**overrides):
        claims = {
            "sub": "verified-subject",
            "iss": settings.cognito_issuer,
            "client_id": "client",
            "token_use": "access",
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
        }
        claims.update(overrides)
        return jwt.encode(claims, key, algorithm="RS256")

    with TestClient(create_app(settings, factory)) as client:
        assert client.get("/api/me", headers={"X-Dev-User": "demo@example.test"}).status_code == 401
        good = {"Authorization": "Bearer " + token(), "X-Dev-User": "demo@example.test"}
        response = client.get("/api/me", headers=good)
        assert response.status_code == 200
        assert response.json()["id"] == "verified-subject"
        assert response.json()["role"] == "user"
        profile_claims = {
            "aud": "client",
            "token_use": "id",
            "email": "Verified@Example.test",
            "email_verified": True,
            "cognito:groups": ["owner"],
        }
        verified = client.post(
            "/api/me/identity", headers=good, json={"id_token": token(**profile_claims)}
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["email"] == "verified@example.test"
        assert verified.json()["role"] == "user"
        assert verified.json()["approved"] is False
        for wrong in (
            {"sub": "different-user"},
            {"aud": "other-client"},
            {"email_verified": False},
            {"token_use": "access"},
            {"exp": int(time.time()) - 1},
        ):
            assert (
                client.post(
                    "/api/me/identity",
                    headers=good,
                    json={"id_token": token(**{**profile_claims, **wrong})},
                ).status_code
                == 401
            )
        assert client.get("/api/me", headers=good).json()["email"] == "verified@example.test"
        for invalid in (
            {"client_id": "other"},
            {"token_use": "id"},
            {"exp": int(time.time()) - 60},
            {"iss": "https://other"},
        ):
            assert (
                client.get(
                    "/api/me", headers={"Authorization": "Bearer " + token(**invalid)}
                ).status_code
                == 401
            )


def test_failed_execution_and_queue_limit_are_durable(service):
    client, factory, settings = service
    settings.max_queue_size = 1
    cid = conversation(client)
    run = client.post(f"/api/conversations/{cid}/runs", json={"prompt": "Research"}).json()
    other = conversation(client)
    assert (
        client.post(f"/api/conversations/{other}/runs", json={"prompt": "Next"}).status_code == 429
    )

    def failure(*_):
        raise RuntimeError("private document content should never enter public error")

    assert process_one(factory, settings, execute=failure)
    response = client.get(f"/api/runs/{run['id']}").json()
    assert response["status"] == "failed"
    assert "private document" not in response["error"]
    assert "event: failed" in client.get(f"/api/runs/{run['id']}/events").text
    assert client.post(f"/api/conversations/{other}/runs", json={"prompt": "  "}).status_code == 422


def test_real_sdk_worker_roundtrip_and_checkpoint_deletion(service):
    from research_app.research import checkpoints
    from research_app.worker import process_document

    client, factory, settings = service
    cid = conversation(client)
    doc = client.post(
        "/api/documents",
        files={"file": ("evidence.txt", b"Lithium recycling can recover battery materials.")},
        data={"conversation_id": cid},
    ).json()
    assert process_document(factory, settings)
    assert client.get("/api/documents").json()[0]["status"] == "ready"
    run = client.post(
        f"/api/conversations/{cid}/runs",
        json={"prompt": "Research lithium recycling from my evidence"},
    ).json()
    assert process_one(factory, settings)
    result = client.get(f"/api/runs/{run['id']}").json()
    assert result["status"] == "completed", result
    assert "Lithium recycling" in result["result"]
    owner = client.get("/api/me").json()["id"]
    with checkpoints(settings) as saver:
        assert saver.get_tuple({"configurable": {"thread_id": f"{owner}:{cid}"}})
    assert client.delete(f"/api/conversations/{cid}").status_code == 204
    with checkpoints(settings) as saver:
        assert saver.get_tuple({"configurable": {"thread_id": f"{owner}:{cid}"}}) is None
    assert client.get("/api/documents").json()[0]["id"] == doc["id"]


def test_stale_document_completion_and_failure_do_not_overwrite_new_attempt(service, monkeypatch):
    from research_app.documents import ingest

    client, factory, settings = service
    doc = client.post("/api/documents", files={"file": ("source.txt", b"Evidence")}).json()
    with factory.begin() as session:
        record = session.get(Document, doc["id"])
        record.status = "indexing"
        record.lease_owner = "old"

    def replaced(texts, settings):
        with factory.begin() as session:
            record = session.get(Document, doc["id"])
            record.status = "ready"
            record.lease_owner = "new"
        return [[0.0] * 384 for _ in texts]

    monkeypatch.setattr("research_app.retrieval.embed_texts", replaced)
    ingest(doc["id"], factory, settings, lease_owner="old")
    with factory() as session:
        assert session.get(Document, doc["id"]).lease_owner == "new"
    with factory.begin() as session:
        session.get(Document, doc["id"]).status = "indexing"

    def replaced_failure(texts, settings):
        replaced(texts, settings)
        raise RuntimeError("old attempt failed after replacement")

    monkeypatch.setattr("research_app.retrieval.embed_texts", replaced_failure)
    ingest(doc["id"], factory, settings, lease_owner="old")
    with factory() as session:
        assert session.get(Document, doc["id"]).status == "ready"


def test_upload_aggregate_limit_and_preparser_body_limit(service):
    client, _, settings = service
    settings.max_user_documents = 1
    assert client.post("/api/documents", files={"file": ("one.txt", b"one")}).status_code == 202
    assert client.post("/api/documents", files={"file": ("two.txt", b"two")}).status_code == 429
    response = client.post(
        "/api/documents",
        content=b"x" * (settings.max_upload_bytes + 1024 * 1024 + 1),
        headers={"Content-Type": "multipart/form-data; boundary=test"},
    )
    assert response.status_code == 413


def test_admin_trace_view_revocation_and_console_fail_closed(service, monkeypatch):
    import httpx

    client, factory, settings = service
    settings.langfuse_base_url = "http://langfuse.internal"
    settings.langfuse_public_key = "public"
    settings.langfuse_secret_key = "private"

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        async def get(self, *args, **kwargs):
            return httpx.Response(
                200,
                request=httpx.Request("GET", args[0]),
                json={
                    "data": [
                        {
                            "id": "trace",
                            "name": "research",
                            "input": "private prompt never expose",
                            "metadata": {"environment": "local"},
                        }
                    ]
                },
            )

    monkeypatch.setattr(httpx, "AsyncClient", lambda **_: FakeClient())
    response = client.get("/api/admin/traces")
    assert response.status_code == 200, response.text
    assert "private prompt" not in response.text
    assert client.get("/api/admin/observability/").status_code == 503
    client.headers["X-Dev-User"] = "pending@example.test"
    pending = client.get("/api/me").json()
    client.headers["X-Dev-User"] = "demo@example.test"
    assert (
        client.patch(
            f"/api/admin/users/{pending['id']}", json={"approved": True, "role": "admin"}
        ).status_code
        == 200
    )
    client.headers["X-Dev-User"] = "pending@example.test"
    assert client.get("/api/admin/traces").status_code == 200
    client.headers["X-Dev-User"] = "demo@example.test"
    client.patch(f"/api/admin/users/{pending['id']}", json={"role": "user"})
    client.headers["X-Dev-User"] = "pending@example.test"
    assert client.get("/api/admin/traces").status_code == 403


def test_dense_prompt_is_rejected_before_queue_admission(service):
    client, _, _ = service
    cid = conversation(client)
    response = client.post(f"/api/conversations/{cid}/runs", json={"prompt": "測" * 3000})
    assert response.status_code == 422
    assert "UTF-8" in response.text


def test_console_auth_request_rechecks_admin_access(service):
    client, _, _ = service
    assert client.get("/api/admin/access").status_code == 204
    client.headers["X-Dev-User"] = "pending-console@example.test"
    pending = client.get("/api/me").json()
    assert client.get("/api/admin/access").status_code == 403
    client.headers["X-Dev-User"] = "demo@example.test"
    client.patch(f"/api/admin/users/{pending['id']}", json={"approved": True, "role": "admin"})
    client.headers["X-Dev-User"] = "pending-console@example.test"
    assert client.get("/api/admin/access").status_code == 204
    client.headers["X-Dev-User"] = "demo@example.test"
    client.patch(f"/api/admin/users/{pending['id']}", json={"role": "user"})
    client.headers["X-Dev-User"] = "pending-console@example.test"
    assert client.get("/api/admin/access").status_code == 403
