"""Run synthetic research harness checks; output is not a model-quality score."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from research_app.config import Settings
from research_app.db import create_session_factory, migrate
from research_app.models import Attachment, Chunk, Conversation, Document, Run, User, now
from research_app.research import (
    ModelGenerationBudget,
    RunStopped,
    execute_research,
    initialize_checkpoints,
)
from research_app.retrieval import embed_texts

NOVA_LITE_MODEL_ID = "amazon.nova-lite-v1:0"
NOVA_LITE_CONTEXT_TOKENS = 300_000
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_DOCUMENT_PREFIX = "Synthetic fixture, not factual evidence for publication."
EVAL_SUITE_VERSION = "deep-research-eval-v2"
EXPECTED_CASES_SHA256 = "1da749330bccda938eb5450a5c233f6208c9642d5712bbc73f272c4a5dccc4b1"
MIN_NOVA_LITE_INPUT_USD_PER_MILLION = Decimal("0.06")
MIN_NOVA_LITE_OUTPUT_USD_PER_MILLION = Decimal("0.24")
MIN_TAVILY_USD_PER_CREDIT = Decimal("0.008")


def worst_case_cost(case_count, args):
    model_cost = args.max_model_generations_per_case * (
        Decimal(NOVA_LITE_CONTEXT_TOKENS) * args.input_usd_per_million_tokens
        + Decimal(args.max_output_tokens_per_generation) * args.output_usd_per_million_tokens
    ) / Decimal(1_000_000)
    # Reserve one full Tavily credit for every permitted search/extract request.
    search_cost = args.max_search_requests_per_case * args.tavily_usd_per_credit
    return case_count * (model_cost + search_cost)


def observed_usage_cost_upper_bound(snapshot, args):
    missing_model = snapshot["model_usage_records_missing"]
    missing_tavily = snapshot["tavily_usage_records_missing"]
    input_tokens = (
        snapshot["actual_input_tokens_reported"]
        + missing_model * NOVA_LITE_CONTEXT_TOKENS
    )
    output_tokens = (
        snapshot["actual_output_tokens_reported"]
        + missing_model * args.max_output_tokens_per_generation
    )
    tavily_credits = snapshot["tavily_credits_reported"] + missing_tavily
    model_cost = (
        Decimal(input_tokens) * args.input_usd_per_million_tokens
        + Decimal(output_tokens) * args.output_usd_per_million_tokens
    ) / Decimal(1_000_000)
    return model_cost + Decimal(tavily_credits) * args.tavily_usd_per_credit


def evaluate_report_checks(case_id, case, report):
    checks = {"completes": bool(report.strip())}
    if case["document_required"]:
        checks["uses_attached_document"] = case_id + ".txt" in report
        checks["preserves_source_locator"] = "section 1" in report
    else:
        checks["document_expectation"] = "not_required"
    return checks, all(value is True or value == "not_required" for value in checks.values())


def require_live_budget_arguments(parser, args):
    required = (
        "max_model_generations_per_case",
        "max_output_tokens_per_generation",
        "max_search_requests_per_case",
        "input_usd_per_million_tokens",
        "output_usd_per_million_tokens",
        "tavily_usd_per_credit",
        "max_total_usd",
    )
    missing = [name.replace("_", "-") for name in required if getattr(args, name) is None]
    if missing:
        parser.error("--live requires explicit budget values: " + ", ".join(missing))


def require_live_budget(parser, args, settings, case_count):
    require_live_budget_arguments(parser, args)
    if settings.model_provider != "bedrock" or settings.bedrock_model_id != NOVA_LITE_MODEL_ID:
        parser.error(f"--live requires MODEL_PROVIDER=bedrock and BEDROCK_MODEL_ID={NOVA_LITE_MODEL_ID}")
    if settings.aws_region != "us-east-1":
        parser.error("--live pricing bound is only configured for us-east-1")
    if args.max_model_generations_per_case < 1:
        parser.error("--max-model-generations-per-case must be positive")
    if args.max_model_generations_per_case > 80:
        parser.error("--max-model-generations-per-case cannot exceed the agent recursion limit (80)")
    if args.max_output_tokens_per_generation < 1 or args.max_output_tokens_per_generation > 10_000:
        parser.error("--max-output-tokens-per-generation must be between 1 and 10000")
    if args.max_search_requests_per_case < 0 or args.max_search_requests_per_case > 10:
        parser.error("--max-search-requests-per-case must be between 0 and 10")
    prices = (
        args.input_usd_per_million_tokens,
        args.output_usd_per_million_tokens,
        args.tavily_usd_per_credit,
        args.max_total_usd,
    )
    if any(not value.is_finite() for value in prices):
        parser.error("Price rates and --max-total-usd must be finite")
    if any(value <= 0 for value in prices):
        parser.error("Price rates and --max-total-usd must be greater than zero")
    if (
        args.input_usd_per_million_tokens < MIN_NOVA_LITE_INPUT_USD_PER_MILLION
        or args.output_usd_per_million_tokens < MIN_NOVA_LITE_OUTPUT_USD_PER_MILLION
        or args.tavily_usd_per_credit < MIN_TAVILY_USD_PER_CREDIT
    ):
        parser.error("Rates cannot be below the conservative published rate floor")
    ceiling = worst_case_cost(case_count, args)
    if ceiling > args.max_total_usd:
        parser.error(
            f"worst-case bound ${ceiling:.4f} exceeds --max-total-usd ${args.max_total_usd:.4f}"
        )
    return ceiling


def prepare_live_output(
    path,
    cases_path,
    cases,
    parser,
    repo_root=REPOSITORY_ROOT,
    expected_cases_sha256=EXPECTED_CASES_SHA256,
):
    """Reserve a private, ignored result file for synthetic live reports only."""
    root = repo_root.resolve()
    expected_cases = root / "evals" / "cases.json"
    if cases_path.resolve() != expected_cases:
        parser.error("--live accepts only the reviewed repository evals/cases.json suite")
    version_path = cases_path.parent / "VERSION"
    if not version_path.is_file() or version_path.read_text().strip() != EVAL_SUITE_VERSION:
        parser.error("--live evaluation suite version does not match the reviewed version")
    suite_hash = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    if suite_hash != expected_cases_sha256:
        parser.error("--live evaluation suite content does not match the reviewed suite digest")
    if any(
        not case["document"].startswith(SYNTHETIC_DOCUMENT_PREFIX)
        for case in cases
    ):
        parser.error("--live evaluation documents must be marked synthetic")

    intended_artifact_root = root / "local-data" / "evals"
    if (root / "local-data").is_symlink() or intended_artifact_root.is_symlink():
        parser.error("live evaluation artifact directory must not contain symlinks")
    artifact_root = intended_artifact_root.resolve()
    if artifact_root != intended_artifact_root or not artifact_root.is_relative_to(root):
        parser.error("live evaluation artifact directory resolves outside the repository")

    requested = Path(os.path.abspath(path))
    if not requested.is_relative_to(intended_artifact_root):
        parser.error("--live output must be a new file under ignored local-data/evals/")
    cursor = requested
    while cursor != intended_artifact_root:
        if cursor.is_symlink():
            parser.error("live evaluation output path must not contain symlinks")
        cursor = cursor.parent
    target = requested.resolve()
    if not target.is_relative_to(artifact_root):
        parser.error("live evaluation output resolves outside ignored local-data/evals/")
    if target.exists():
        parser.error("--live output already exists; choose a new path to preserve prior results")
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.parent.resolve().is_relative_to(artifact_root):
        parser.error("live evaluation output parent resolves outside local-data/evals")
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        parser.error(f"could not reserve private live output ({type(exc).__name__})")
    os.close(descriptor)
    return target


def write_private_json(path, result):
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path("evals/cases.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Use Bedrock Nova Lite and optionally Tavily; requires explicit budget flags",
    )
    parser.add_argument("--max-model-generations-per-case", type=int)
    parser.add_argument("--max-output-tokens-per-generation", type=int)
    parser.add_argument("--max-search-requests-per-case", type=int)
    parser.add_argument("--input-usd-per-million-tokens", type=Decimal)
    parser.add_argument("--output-usd-per-million-tokens", type=Decimal)
    parser.add_argument("--tavily-usd-per-credit", type=Decimal)
    parser.add_argument("--max-total-usd", type=Decimal)
    args = parser.parse_args()
    cases = json.loads(args.cases.read_text())
    if (
        not isinstance(cases, list)
        or not cases
        or len(cases) > 20
        or any(
            not isinstance(case, dict)
            or not all(isinstance(case.get(field), str) and case[field] for field in ("id", "topic", "prompt", "document"))
            or case.get("web_search_mode", "enabled") not in ("enabled", "disabled")
            or not isinstance(case.get("document_required"), bool)
            or not isinstance(case.get("human_review"), list)
            or not case["human_review"]
            or any(not isinstance(item, str) or not item for item in case["human_review"])
            for case in cases
        )
        or len({case["id"] for case in cases if isinstance(case, dict) and isinstance(case.get("id"), str)}) != len(cases)
    ):
        parser.error(
            "--cases must contain 1–20 uniquely identified cases with non-empty id/topic/prompt/document, valid search mode, and human review criteria"
        )
    if args.live:
        require_live_budget_arguments(parser, args)
    outcomes = []
    ceiling = None
    suite_hash = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(
            environment="local",
            database_url=f"sqlite:///{directory}/eval.db",
            storage_path=Path(directory) / "files",
            search_call_limit=(args.max_search_requests_per_case if args.live else 10),
            **({} if args.live else {"model_provider": "fake", "embedding_provider": "fake"}),
        )
        if args.live:
            ceiling = require_live_budget(parser, args, settings, len(cases))
            args.output = prepare_live_output(args.output, args.cases, cases, parser)
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
            started = time.monotonic()
            model_budget = None
            try:
                case_settings = settings
                if case.get("web_search_mode", "enabled") == "disabled":
                    case_settings = settings.model_copy(update={"tavily_api_key": ""})
                if args.live:
                    model_budget = ModelGenerationBudget(
                        max_generations=args.max_model_generations_per_case,
                        max_input_tokens=(
                            args.max_model_generations_per_case * NOVA_LITE_CONTEXT_TOKENS
                        ),
                        max_output_tokens=(
                            args.max_model_generations_per_case
                            * args.max_output_tokens_per_generation
                        ),
                        input_context_tokens=NOVA_LITE_CONTEXT_TOKENS,
                        output_tokens_per_generation=args.max_output_tokens_per_generation,
                    )
                report = execute_research(
                    key, factory, case_settings, model_budget=model_budget
                )
                checks, checks_passed = evaluate_report_checks(key, case, report)
                checks["no_live_claim_in_fake"] = args.live or "Offline fixture" in report
                outcome = {
                    "id": key,
                    "checks": checks,
                    "passed": checks_passed and checks["no_live_claim_in_fake"],
                }
                if model_budget is not None:
                    budget_snapshot = model_budget.snapshot()
                    outcome["usage"] = budget_snapshot
                    outcome["usage_cost_upper_bound_usd"] = str(
                        observed_usage_cost_upper_bound(budget_snapshot, args)
                    )
                    outcome["report"] = report
                    outcome["human_review"] = case["human_review"]
                outcome["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                outcomes.append(outcome)
            except Exception as exc:
                outcome = {"id": key, "passed": False, "error_type": type(exc).__name__}
                if isinstance(exc, RunStopped):
                    outcome["stop_reason"] = str(exc)[:120]
                if model_budget is not None:
                    budget_snapshot = model_budget.snapshot()
                    outcome["usage"] = budget_snapshot
                    outcome["usage_cost_upper_bound_usd"] = str(
                        observed_usage_cost_upper_bound(budget_snapshot, args)
                    )
                    outcome["human_review"] = case["human_review"]
                outcome["latency_ms"] = round((time.monotonic() - started) * 1000, 2)
                outcomes.append(outcome)
    result = {
        "kind": "live_research_checks" if args.live else "offline_harness_checks",
        "case_suite_version": (
            EVAL_SUITE_VERSION
            if args.cases.resolve() == REPOSITORY_ROOT / "evals" / "cases.json"
            else "unversioned"
        ),
        "case_suite_sha256": suite_hash,
        "model_quality_verified": False,
        "human_review_required": True,
        "outcomes": outcomes,
    }
    if args.live:
        result["budget"] = {
            "model_id": settings.bedrock_model_id,
            "region": settings.aws_region,
            "input_usd_per_million_tokens": str(args.input_usd_per_million_tokens),
            "output_usd_per_million_tokens": str(args.output_usd_per_million_tokens),
            "tavily_usd_per_credit": str(args.tavily_usd_per_credit),
            "max_total_usd": str(args.max_total_usd),
            "max_cases": len(cases),
            "max_model_generations_per_case": args.max_model_generations_per_case,
            "max_input_tokens_per_generation_reservation": NOVA_LITE_CONTEXT_TOKENS,
            "max_input_tokens_total": (
                len(cases) * args.max_model_generations_per_case * NOVA_LITE_CONTEXT_TOKENS
            ),
            "max_output_tokens_total": (
                len(cases)
                * args.max_model_generations_per_case
                * args.max_output_tokens_per_generation
            ),
            "max_search_requests_total": len(cases) * args.max_search_requests_per_case,
            "worst_case_usd": str(ceiling),
            "authorized_spend": False,
        }
        write_private_json(args.output, result)
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        f"{sum(row['passed'] for row in outcomes)}/{len(outcomes)} harness cases passed; model quality unverified"
    )
    return 0 if outcomes and all(row["passed"] for row in outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
