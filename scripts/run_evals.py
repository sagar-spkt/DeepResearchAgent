"""Run synthetic research harness checks; output is not a model-quality score."""

import argparse
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from research_app.config import Settings
from research_app.db import create_session_factory, migrate
from research_app.models import Attachment, Chunk, Conversation, Document, Run, User, now
from research_app.research import execute_research, initialize_checkpoints
from research_app.retrieval import embed_texts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use explicitly configured MODEL_PROVIDER; invokes paid services",
    )
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    outcomes = []
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(
            environment="local",
            database_url=f"sqlite:///{directory}/eval.db",
            storage_path=Path(directory) / "files",
            **({} if args.live else {"model_provider": "fake", "embedding_provider": "fake"}),
        )
        if args.live and settings.model_provider == "fake":
            parser.error("--live requires an explicitly configured real MODEL_PROVIDER")
        factory = create_session_factory(settings)
        migrate(factory)
        initialize_checkpoints(settings)
        for case in cases:
            key = case["id"]
            with factory.begin() as session:
                if session.get(User, "evaluation") is None:
                    session.add(
                        User(id="evaluation", email="evaluation@example.test", approved=True)
                    )
                    session.flush()
                session.add(Conversation(id=key, user_id="evaluation", title=case["topic"]))
                session.add(
                    Document(
                        id=key,
                        user_id="evaluation",
                        name=key + ".txt",
                        media_type="text/plain",
                        storage_key=key,
                        status="ready",
                    )
                )
                session.flush()
                session.add(Attachment(user_id="evaluation", conversation_id=key, document_id=key))
                session.add(
                    Chunk(
                        user_id="evaluation",
                        document_id=key,
                        ordinal=0,
                        text=case["document"],
                        locator="section 1",
                        embedding=embed_texts([case["document"]], settings)[0],
                    )
                )
                session.add(
                    Run(
                        id=key,
                        user_id="evaluation",
                        conversation_id=key,
                        prompt=case["prompt"],
                        status="running",
                        started_at=now(),
                        lease_owner="evaluation",
                        lease_until=now() + timedelta(hours=1),
                    )
                )
            try:
                report = execute_research(key, factory, settings)
                checks = {
                    "completes": bool(report.strip()),
                    "uses_attached_document": key + ".txt" in report,
                    "preserves_source_locator": "section 1" in report,
                    "no_live_claim_in_fake": args.live or "Offline fixture" in report,
                }
                outcomes.append({"id": key, "checks": checks, "passed": all(checks.values())})
            except Exception as exc:
                outcomes.append({"id": key, "passed": False, "error_type": type(exc).__name__})
    result = {
        "kind": "live_research_checks" if args.live else "offline_harness_checks",
        "model_quality_verified": False,
        "human_review_required": True,
        "outcomes": outcomes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{sum(row['passed'] for row in outcomes)}/{len(outcomes)} harness cases passed; model quality unverified"
    )
    return 0 if outcomes and all(row["passed"] for row in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
