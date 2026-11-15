import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr
from research_app.config import Settings
from research_app.model_factory import create_model
from research_app.research import ModelGenerationBudget, RunStopped

from scripts.run_evals import (
    EVAL_SUITE_VERSION,
    EXPECTED_CASES_SHA256,
    evaluate_report_checks,
    observed_usage_cost_upper_bound,
    prepare_live_output,
    require_live_budget,
    require_live_budget_arguments,
    worst_case_cost,
    write_private_json,
)


class LimitedModel(BaseChatModel):
    _calls = PrivateAttr(default=0)

    @property
    def _llm_type(self):
        return "budget-test"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="synthetic"))])


def test_live_budget_bound_reserves_context_output_and_search_costs():
    args = SimpleNamespace(
        max_model_generations_per_case=2,
        max_output_tokens_per_generation=100,
        max_search_requests_per_case=3,
        input_usd_per_million_tokens=Decimal("0.06"),
        output_usd_per_million_tokens=Decimal("0.24"),
        tavily_usd_per_credit=Decimal("0.008"),
    )
    assert worst_case_cost(1, args) == Decimal("0.060048")
    assert worst_case_cost(20, args) == Decimal("1.200960")


def test_document_checks_only_require_citations_for_relevant_fixtures():
    required, required_passed = evaluate_report_checks(
        "research-13",
        {"document_required": True},
        "Schedule: Monday-Friday. [research-13.txt, section 1]",
    )
    assert required_passed
    assert required["uses_attached_document"] is True
    assert required["preserves_source_locator"] is True

    missing_citation, missing_passed = evaluate_report_checks(
        "research-13", {"document_required": True}, "Schedule: Monday-Friday."
    )
    assert missing_passed is False

    irrelevant, irrelevant_passed = evaluate_report_checks(
        "research-17", {"document_required": False}, "Web search unavailable."
    )
    assert irrelevant_passed
    assert irrelevant["document_expectation"] == "not_required"
    assert "uses_attached_document" not in irrelevant


def test_reviewed_eval_suite_digest_and_document_relevance_metadata_match():
    repository = Path(__file__).resolve().parents[2]
    cases_path = repository / "evals" / "cases.json"
    cases = json.loads(cases_path.read_text())
    assert hashlib.sha256(cases_path.read_bytes()).hexdigest() == EXPECTED_CASES_SHA256
    assert (repository / "evals" / "VERSION").read_text().strip() == EVAL_SUITE_VERSION
    assert len(cases) == 20
    assert all(isinstance(case.get("document_required"), bool) for case in cases)


def test_usage_cost_upper_bound_uses_reported_usage_and_full_reservations_when_missing():
    args = SimpleNamespace(
        max_output_tokens_per_generation=100,
        input_usd_per_million_tokens=Decimal("0.06"),
        output_usd_per_million_tokens=Decimal("0.24"),
        tavily_usd_per_credit=Decimal("0.008"),
    )
    snapshot = {
        "actual_input_tokens_reported": 100,
        "actual_output_tokens_reported": 10,
        "model_usage_records_missing": 0,
        "tavily_credits_reported": 1,
        "tavily_usage_records_missing": 0,
    }
    assert observed_usage_cost_upper_bound(snapshot, args) == Decimal("0.0080084")
    snapshot.update(
        {
            "model_usage_records_missing": 1,
            "tavily_usage_records_missing": 1,
            "actual_input_tokens_reported": 0,
            "actual_output_tokens_reported": 0,
            "tavily_credits_reported": 0,
        }
    )
    assert observed_usage_cost_upper_bound(snapshot, args) == Decimal("0.026024")


def test_live_budget_arguments_fail_closed_when_any_ceiling_is_missing():
    args = SimpleNamespace(
        max_model_generations_per_case=2,
        max_output_tokens_per_generation=None,
        max_search_requests_per_case=3,
        input_usd_per_million_tokens=Decimal("0.06"),
        output_usd_per_million_tokens=Decimal("0.24"),
        tavily_usd_per_credit=Decimal("0.008"),
        max_total_usd=Decimal("1"),
    )
    with pytest.raises(SystemExit):
        require_live_budget_arguments(argparse.ArgumentParser(), args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (field, value)
        for field in (
            "input_usd_per_million_tokens",
            "output_usd_per_million_tokens",
            "tavily_usd_per_credit",
            "max_total_usd",
        )
        for value in (Decimal("Infinity"), Decimal("NaN"))
    ],
)
def test_live_budget_rejects_non_finite_cost_inputs(field, value):
    args = SimpleNamespace(
        max_model_generations_per_case=1,
        max_output_tokens_per_generation=100,
        max_search_requests_per_case=0,
        input_usd_per_million_tokens=Decimal("0.06"),
        output_usd_per_million_tokens=Decimal("0.24"),
        tavily_usd_per_credit=Decimal("0.008"),
        max_total_usd=Decimal("5.00"),
    )
    setattr(args, field, value)
    settings = Settings(
        model_provider="bedrock",
        bedrock_model_id="amazon.nova-lite-v1:0",
        aws_region="us-east-1",
        embedding_provider="fake",
    )
    with pytest.raises(SystemExit):
        require_live_budget(argparse.ArgumentParser(), args, settings, 1)


def test_langchain_rate_limiter_blocks_model_call_before_generation():
    budget = ModelGenerationBudget(
        max_generations=1,
        max_input_tokens=300_000,
        max_output_tokens=100,
        input_context_tokens=300_000,
        output_tokens_per_generation=100,
    )
    model = LimitedModel(rate_limiter=budget)
    assert model.invoke("first")
    with pytest.raises(RunStopped, match="generation budget exhausted"):
        model.invoke("second")
    assert model._calls == 1


def test_bedrock_evaluation_client_is_limited_to_one_total_attempt(monkeypatch):
    import langchain_aws

    captured = {}

    def capture_model(**kwargs):
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(langchain_aws, "ChatBedrockConverse", capture_model)
    settings = Settings(
        model_provider="bedrock",
        bedrock_model_id="amazon.nova-lite-v1:0",
        embedding_provider="fake",
    )
    limiter = ModelGenerationBudget(
        max_generations=1,
        max_input_tokens=300_000,
        max_output_tokens=100,
        input_context_tokens=300_000,
        output_tokens_per_generation=100,
    )
    create_model(
        settings,
        "evaluation-user",
        max_output_tokens=100,
        max_retries=0,
        rate_limiter=limiter,
        cache=False,
    )
    assert captured["max_retries"] == 0
    assert captured["rate_limiter"] is limiter
    assert captured["config"].retries == {"total_max_attempts": 1}

    client = boto3.client(
        "bedrock-runtime",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
        config=captured["config"],
    )
    assert client.meta.config.retries["total_max_attempts"] == 1


def test_live_reports_are_private_local_artifacts_and_do_not_overwrite(tmp_path):
    root = tmp_path / "repo"
    cases_path = root / "evals" / "cases.json"
    cases_path.parent.mkdir(parents=True)
    cases_path.write_text("[]")
    (cases_path.parent / "VERSION").write_text("deep-research-eval-v2\n")
    output = root / "local-data" / "evals" / "run.json"
    cases = [
        {
            "document": "Synthetic fixture, not factual evidence for publication. Section 1: x",
            "human_review": ["Check citations"],
        }
    ]

    reserved = prepare_live_output(
        output,
        cases_path,
        cases,
        argparse.ArgumentParser(),
        root,
        expected_cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
    )
    assert reserved.stat().st_mode & 0o777 == 0o600
    write_private_json(reserved, {"report": "synthetic real-model output"})
    assert reserved.read_text() == '{\n  "report": "synthetic real-model output"\n}\n'
    assert reserved.stat().st_mode & 0o777 == 0o600
    with pytest.raises(SystemExit):
        prepare_live_output(
            output,
            cases_path,
            cases,
            argparse.ArgumentParser(),
            root,
            expected_cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        )


def test_live_report_path_outside_ignored_local_data_is_refused(tmp_path):
    root = tmp_path / "repo"
    cases_path = root / "evals" / "cases.json"
    cases_path.parent.mkdir(parents=True)
    cases_path.write_text("[]")
    (cases_path.parent / "VERSION").write_text("deep-research-eval-v2\n")
    cases = [
        {
            "document": "Synthetic fixture, not factual evidence for publication. Section 1: x",
            "human_review": ["Check citations"],
        }
    ]
    with pytest.raises(SystemExit):
        prepare_live_output(
            root / "reports" / "run.json",
            cases_path,
            cases,
            argparse.ArgumentParser(),
            root,
            expected_cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        )


def test_live_runner_rejects_changed_suite_and_symlinked_artifact_root(tmp_path):
    root = tmp_path / "repo"
    cases_path = root / "evals" / "cases.json"
    cases_path.parent.mkdir(parents=True)
    cases_path.write_text("changed suite")
    (cases_path.parent / "VERSION").write_text("deep-research-eval-v2\n")
    cases = [
        {
            "document": "Synthetic fixture, not factual evidence for publication. Section 1: x",
            "human_review": ["Check citations"],
        }
    ]
    with pytest.raises(SystemExit):
        prepare_live_output(
            root / "local-data" / "evals" / "run.json",
            cases_path,
            cases,
            argparse.ArgumentParser(),
            root,
        )

    local_data = root / "local-data"
    local_data.mkdir(parents=True)
    (root / "reports").mkdir()
    (local_data / "evals").symlink_to(root / "reports", target_is_directory=True)
    with pytest.raises(SystemExit):
        prepare_live_output(
            root / "local-data" / "evals" / "run.json",
            cases_path,
            cases,
            argparse.ArgumentParser(),
            root,
            expected_cases_sha256=hashlib.sha256(cases_path.read_bytes()).hexdigest(),
        )
