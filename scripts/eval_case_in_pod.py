"""Run one evaluation case with the deployed research agent; print one JSON result line.

Piped into the worker pod by scripts/langfuse_experiment.py:

    kubectl exec -i deploy/research-worker -- python - <case-b64> < scripts/eval_case_in_pod.py

The case runs exactly like a production run (same image, model, tools, limits and
Langfuse tracing) but against a throwaway SQLite database, so evaluation data
never touches the application database. The fixture document is inserted as an
already-indexed, attached document named ``<case id>.txt`` with locator
``section 1``. Only the result line is written to stdout.
"""

import base64
import json
import sys
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from research_app.config import Settings
from research_app.db import create_session_factory, migrate
from research_app.models import Attachment, Chunk, Conversation, Document, Run, User, now
from research_app.research import RunStopped, execute_research, initialize_checkpoints
from research_app.retrieval import embed_texts

USER_ID = "evaluation"


def main():
    request = json.loads(base64.b64decode(sys.argv[1]))
    case, run_id, session_id = request["case"], request["run_id"], request["session_id"]
    case_id = case["id"]
    result = {"case_id": case_id, "run_id": run_id}
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(
            environment="local",
            database_url=f"sqlite:///{directory}/eval.db",
            storage_backend="local",
            storage_path=Path(directory) / "files",
        )
        # Tag traces with the deployed environment; validation above only governs storage.
        settings = settings.model_copy(update={"environment": request["environment"]})
        if case.get("web_search_mode", "enabled") == "disabled":
            settings = settings.model_copy(update={"tavily_api_key": ""})
        result["model"] = settings.bedrock_model_id
        factory = create_session_factory(settings)
        migrate(factory)
        initialize_checkpoints(settings)
        with factory.begin() as session:
            session.add(User(id=USER_ID, email="evaluation@example.test", approved=True))
            session.flush()
            session.add(Conversation(id=session_id, user_id=USER_ID, title=case["topic"]))
            session.add(
                Document(
                    id=case_id,
                    user_id=USER_ID,
                    name=f"{case_id}.txt",
                    media_type="text/plain",
                    storage_key=case_id,
                    status="ready",
                )
            )
            session.flush()
            session.add(Attachment(user_id=USER_ID, conversation_id=session_id, document_id=case_id))
            session.add(
                Chunk(
                    user_id=USER_ID,
                    document_id=case_id,
                    ordinal=0,
                    text=case["document"],
                    locator="section 1",
                    embedding=embed_texts([case["document"]], settings)[0],
                )
            )
            session.add(
                Run(
                    id=run_id,
                    user_id=USER_ID,
                    conversation_id=session_id,
                    prompt=case["prompt"],
                    status="running",
                    started_at=now(),
                    lease_owner="evaluation",
                    lease_until=now() + timedelta(hours=1),
                )
            )
        started = time.monotonic()
        try:
            result["report"] = execute_research(run_id, factory, settings)
            result["status"] = "completed"
        except RunStopped as exc:
            result.update(status="stopped", error=str(exc)[:300])
        except Exception as exc:  # report the failure; the driver records it as a score
            result.update(status="failed", error=f"{type(exc).__name__}: {str(exc)[:300]}")
        result["latency_seconds"] = round(time.monotonic() - started, 1)
    print("EVAL_RESULT " + json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
