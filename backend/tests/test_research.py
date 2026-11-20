import json
from datetime import timedelta

import httpx
import pytest
from research_app.config import Settings
from research_app.db import create_session_factory, migrate
from research_app.models import (
    Attachment,
    Chunk,
    Conversation,
    Document,
    Event,
    Memory,
    Run,
    User,
    now,
)
from research_app.observability import MAX_FIELD_CHARS, LangfuseCallback
from research_app.research import (
    ModelGenerationBudget,
    RunStopped,
    RunTools,
    checkpoints,
    execute_research,
)
from research_app.retrieval import embed_texts, retrieve
from sqlalchemy import select


@pytest.fixture
def context(tmp_path):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path}/db.sqlite",
        storage_path=tmp_path / "uploads",
        model_provider="fake",
        embedding_provider="fake",
    )
    factory = create_session_factory(settings)
    migrate(factory)
    with factory.begin() as session:
        session.add_all(
            [
                User(id="alice", email="alice@example.test", approved=True),
                User(id="bob", email="bob@example.test", approved=True),
            ]
        )
        session.flush()
        session.add(Conversation(id="conversation", user_id="alice", title="Sources"))
        session.flush()
        session.add(
            Run(
                id="run",
                user_id="alice",
                conversation_id="conversation",
                prompt="Research battery recycling",
                status="running",
                started_at=now(),
                lease_owner="worker",
                lease_until=now() + timedelta(minutes=5),
            )
        )
    return settings, factory


def test_sdk_fake_execution_and_durable_restart(context):
    settings, factory = context
    with factory.begin() as session:
        session.add(Memory(user_id="alice", content="Prefer concise comparisons"))
    report = execute_research("run", factory, settings)
    assert "Offline fixture" in report
    assert "No ready documents" in report
    with checkpoints(settings) as saver:
        state = saver.get_tuple({"configurable": {"thread_id": "alice:conversation"}})
        assert state is not None
        checkpoint_id = state.checkpoint["id"]
    # Recreating the runtime after completion must reuse the result, not append the prompt twice.
    assert execute_research("run", factory, settings) == report
    with checkpoints(settings) as saver:
        assert (
            saver.get_tuple({"configurable": {"thread_id": "alice:conversation"}}).checkpoint["id"]
            == checkpoint_id
        )
    with factory() as session:
        assert session.scalar(select(Event).where(Event.kind == "documents_used")) is not None
        assert len(list(session.scalars(select(Memory)))) == 1


def test_new_run_includes_retrieved_attached_evidence_without_tool_choice(context):
    settings, factory = context
    source_text = "Example Widget supports exporting plain text summaries."
    vector = embed_texts([source_text], settings)[0]
    with factory.begin() as session:
        session.add(
            Document(
                id="source",
                user_id="alice",
                name="specification.txt",
                media_type="text/plain",
                storage_key="source",
                status="ready",
            )
        )
        session.flush()
        session.add(
            Attachment(conversation_id="conversation", document_id="source", user_id="alice")
        )
        session.add(
            Chunk(
                document_id="source",
                user_id="alice",
                ordinal=0,
                text=source_text,
                locator="section 1",
                embedding=vector,
            )
        )

    report = execute_research("run", factory, settings)

    assert source_text in report
    with factory() as session:
        used = session.scalar(
            select(Event.data).where(Event.run_id == "run", Event.kind == "documents_used")
        )
    assert used == {"document_ids": ["source"]}


def test_retrieval_requires_ownership_ready_and_explicit_attachment(context):
    settings, factory = context
    with factory.begin() as session:
        session.add_all(
            [
                Document(
                    id="own",
                    user_id="alice",
                    name="Own",
                    media_type="text/plain",
                    storage_key="own",
                    status="ready",
                ),
                Document(
                    id="other",
                    user_id="bob",
                    name="Other",
                    media_type="text/plain",
                    storage_key="other",
                    status="ready",
                ),
            ]
        )
        session.flush()
        vectors = embed_texts(["Battery recycling", "Private battery"], settings)
        session.add_all(
            [
                Chunk(
                    document_id="own",
                    user_id="alice",
                    ordinal=0,
                    text="Battery recycling",
                    locator="section 1",
                    embedding=vectors[0],
                ),
                Chunk(
                    document_id="other",
                    user_id="bob",
                    ordinal=0,
                    text="Private battery",
                    locator="section 1",
                    embedding=vectors[1],
                ),
            ]
        )
    assert retrieve(factory, settings, "alice", "conversation", "battery") == []
    with factory.begin() as session:
        session.add(Attachment(conversation_id="conversation", document_id="own", user_id="alice"))
    assert [
        r["document_id"] for r in retrieve(factory, settings, "alice", "conversation", "battery")
    ] == ["own"]
    # A new attachment is visible without rebuilding the run tools.
    tools = RunTools("run", factory, settings)
    assert "Battery recycling" in tools.retrieve_documents("battery")
    with factory.begin() as session:
        session.get(Document, "own").status = "deleted"
    assert retrieve(factory, settings, "alice", "conversation", "battery") == []


def test_cancel_revocation_and_lease_fence(context):
    settings, factory = context
    tools = RunTools("run", factory, settings)
    with factory.begin() as session:
        session.get(Run, "run").lease_owner = "replacement"
    with pytest.raises(RunStopped, match="lease lost"):
        tools.emit("progress", {"message": "stale"})
    with factory.begin() as session:
        session.get(Run, "run").lease_owner = "worker"
        session.get(Run, "run").cancel_requested = True
    with pytest.raises(RunStopped, match="no longer active"):
        tools.retrieve_documents("battery")
    with factory.begin() as session:
        session.get(Run, "run").cancel_requested = False
        session.get(User, "alice").approved = False
    with pytest.raises(RunStopped, match="revoked"):
        tools.check()


def test_evaluation_model_budget_reserves_before_generation():
    budget = ModelGenerationBudget(
        max_generations=2,
        max_input_tokens=200,
        max_output_tokens=20,
        input_context_tokens=100,
        output_tokens_per_generation=10,
    )
    budget.acquire()
    budget.acquire()
    with pytest.raises(RunStopped, match="generation budget exhausted"):
        budget.acquire()
    assert budget.generations == 2
    assert budget.input_reserved == 200
    assert budget.output_reserved == 20


def test_evaluation_model_budget_refuses_incomplete_token_reservation():
    budget = ModelGenerationBudget(
        max_generations=2,
        max_input_tokens=150,
        max_output_tokens=20,
        input_context_tokens=100,
        output_tokens_per_generation=10,
    )
    budget.acquire()
    with pytest.raises(RunStopped, match="input-token budget exhausted"):
        budget.acquire()
    assert budget.generations == 1


def test_search_limit_survives_tool_recreation_and_no_network_in_fake(context, monkeypatch):
    settings, factory = context
    settings.search_call_limit = 1
    tools = RunTools("run", factory, settings)
    monkeypatch.setattr("httpx.post", lambda *a, **k: pytest.fail("offline test attempted HTTP"))
    assert "disabled" in tools.web_search("query")
    tools.reserve_search()
    with pytest.raises(RunStopped, match="budget exhausted"):
        RunTools("run", factory, settings).reserve_search()
    assert "Only URLs" in tools.read_webpage("http://169.254.169.254/latest/meta-data")


def test_tavily_usage_metadata_is_collected_without_recording_search_content(context, monkeypatch):
    settings, factory = context
    settings = settings.model_copy(
        update={
            "model_provider": "bedrock",
            "bedrock_model_id": "amazon.nova-lite-v1:0",
            "tavily_api_key": "synthetic-key",
        }
    )
    budget = ModelGenerationBudget(
        max_generations=1,
        max_input_tokens=300_000,
        max_output_tokens=100,
        input_context_tokens=300_000,
        output_tokens_per_generation=100,
    )
    requests = []

    def mock_post(url, *, json, timeout):
        requests.append((url, json, timeout))
        if url.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "usage": {"credits": 1},
                    "results": [
                        {
                            "url": "https://example.test/source",
                            "title": "Test",
                            "raw_content": "synthetic",
                        }
                    ],
                },
                request=httpx.Request("POST", url),
            )
        return httpx.Response(
            200,
            json={
                "usage": {"credits": 0},
                "results": [
                    {
                        "url": "https://example.test/source",
                        "raw_content": "synthetic page",
                    }
                ],
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr("httpx.post", mock_post)
    tools = RunTools("run", factory, settings, usage_collector=budget)
    assert "https://example.test/source" in tools.web_search("synthetic query")
    assert "synthetic page" in tools.read_webpage("https://example.test/source")
    assert all(request[1]["include_usage"] is True for request in requests)
    assert all(request[1]["api_key"] == "synthetic-key" for request in requests)
    snapshot = budget.snapshot()
    assert snapshot["tavily_requests"] == 2
    assert snapshot["tavily_credits_reported"] == 1
    assert snapshot["tavily_usage_records_missing"] == 0
    assert "synthetic query" not in json.dumps(snapshot)


def _langfuse(settings, capture):
    settings.langfuse_base_url = "http://langfuse.invalid"
    settings.langfuse_public_key = "public"
    settings.langfuse_secret_key = "secret"
    settings.langfuse_capture_io = capture
    return LangfuseCallback(
        settings, "run", "conversation", "alice@example.test", question="What is Zephyr?"
    )


def _exercise(callback):
    from langchain_core.messages import AIMessage, HumanMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    callback.on_chat_model_start(
        {}, [[HumanMessage(content="private document content")]], run_id="generation",
        invocation_params={"model_id": "amazon.nova-lite-v1:0"},
    )
    reply = AIMessage(
        content="",
        tool_calls=[{"name": "web_search", "args": {"query": "zephyr"}, "id": "c1"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
    )
    callback.on_llm_end(LLMResult(generations=[[ChatGeneration(message=reply)]]), run_id="generation")
    callback.on_tool_start({"name": "retrieve_documents"}, "private query", run_id="tool")
    callback.on_tool_end("private result " + "x" * (MAX_FIELD_CHARS + 50), run_id="tool")
    callback.on_tool_error(RuntimeError("search quota exhausted"), run_id="tool2")
    callback.set_output("Final private report")
    return [item["body"] for item in callback.batch]


def test_telemetry_records_inputs_outputs_and_failure_is_nonfatal(context, monkeypatch):
    settings, _ = context
    callback = _langfuse(settings, capture=True)
    events = _exercise(callback)
    trace, generation, generation_end, span, span_end, span_error, trace_output = events
    assert trace["id"] == "run" and trace["sessionId"] == "conversation"
    assert trace["userId"] != "alice@example.test"
    assert trace["input"] == "What is Zephyr?" and trace["environment"] == settings.environment
    assert generation["traceId"] == "run" and generation["model"] == "amazon.nova-lite-v1:0"
    assert generation["input"] == [{"role": "human", "content": "private document content"}]
    assert generation_end["usage"] == {"input": 10, "output": 2, "total": 12}
    assert generation_end["output"]["tool_calls"] == [{"name": "web_search", "args": {"query": "zephyr"}}]
    assert span["name"] == "retrieve_documents" and span["input"] == "private query"
    assert span_end["output"].startswith("private result") and "[truncated" in span_end["output"]
    assert len(span_end["output"]) < MAX_FIELD_CHARS + 100
    assert "search quota exhausted" in span_error["statusMessage"]
    assert trace_output == {"id": "run", "output": "Final private report", "environment": settings.environment}

    sent = []
    monkeypatch.setattr("research_app.observability.MAX_BATCH_BYTES", 2_000)

    class Accepted:
        status_code = 200

        def raise_for_status(self):
            pass

    monkeypatch.setattr("httpx.post", lambda *a, **k: sent.append(k["content"]) or Accepted())
    callback.flush()
    assert len(sent) > 1  # large runs are split into several ingestion requests
    assert sum(len(json.loads(body)["batch"]) for body in sent) == len(events)

    callback = _langfuse(settings, capture=True)

    def unavailable(*a, **k):
        raise RuntimeError("offline transport")

    monkeypatch.setattr("httpx.post", unavailable)
    callback.flush()
    assert callback.batch == []


def test_telemetry_can_omit_content(context):
    settings, _ = context
    events = _exercise(_langfuse(settings, capture=False))
    assert "private" not in json.dumps(events) and "Zephyr" not in json.dumps(events)
    assert "quota" not in json.dumps(events)
    assert events[2]["usage"]["input"] == 10  # usage and timing are still recorded


def test_stale_checkpoint_writes_are_fenced(context):
    from langgraph.checkpoint.memory import InMemorySaver
    from research_app.research import FencedCheckpointer

    settings, factory = context
    tools = RunTools("run", factory, settings)
    saver = FencedCheckpointer(InMemorySaver(), tools)
    with factory.begin() as session:
        session.get(Run, "run").lease_owner = "new-worker"
    with pytest.raises(RunStopped, match="lease lost"):
        saver.put_writes(
            {
                "configurable": {
                    "thread_id": "alice:conversation",
                    "checkpoint_ns": "",
                    "checkpoint_id": "test",
                }
            },
            [],
            "stale-task",
        )


def test_non_ascii_memory_is_bounded_in_actual_model_prompt(context, monkeypatch):
    from research_app import research

    settings, factory = context
    with factory.begin() as session:
        session.add_all([Memory(user_id="alice", content="🌿測試" * 1000) for _ in range(25)])
    original = research.create_deep_agent
    observed = []

    def capture(**kwargs):
        observed.append(kwargs["system_prompt"])
        return original(**kwargs)

    monkeypatch.setattr(research, "create_deep_agent", capture)
    execute_research("run", factory, settings)
    assert len(observed[0]) < 8000
    assert "\\ud83c" not in observed[0]


def test_hundred_turns_compact_and_offload_history(context, monkeypatch):
    from research_app import research

    settings, factory = context
    settings.run_timeout_seconds = 3600
    original = research.create_deep_agent
    agents = []
    from research_app.model_factory import OfflineResearchModel

    generate = OfflineResearchModel._generate
    model_input_sizes = []

    def observe_input(self, messages, **kwargs):
        model_input_sizes.append(len(messages))
        return generate(self, messages, **kwargs)

    monkeypatch.setattr(OfflineResearchModel, "_generate", observe_input)

    def capture(**kwargs):
        agent = original(**kwargs)
        agents[:] = [agent]
        return agent

    monkeypatch.setattr(research, "create_deep_agent", capture)
    with factory.begin() as session:
        session.add(Memory(user_id="alice", content="Remember the project codename Fern"))
    for number in range(100):
        run_id = f"turn-{number}"
        with factory.begin() as session:
            session.add(
                Run(
                    id=run_id,
                    user_id="alice",
                    conversation_id="conversation",
                    prompt=f"Turn {number}: " + ("synthetic research context " * 160),
                    status="running",
                    started_at=now(),
                    lease_owner="worker",
                    lease_until=now() + timedelta(hours=1),
                )
            )
        assert "Offline fixture" in execute_research(run_id, factory, settings)
    from research_app.research import FencedCheckpointer

    with checkpoints(settings) as saver:
        # Reopen durable storage after the execution context closed its connection.
        agents[0].checkpointer = FencedCheckpointer(saver, RunTools("turn-99", factory, settings))
        state = agents[0].get_state({"configurable": {"thread_id": "alice:conversation"}}).values
    assert any("conversation_history" in name for name in state.get("files", {})), state.keys()
    # SDK retains the full durable transcript while compacting the model's view.
    assert len(state["messages"]) >= 100 * 3
    assert model_input_sizes[-1] < 100
    with factory() as session:
        assert "Fern" in session.scalar(select(Memory.content).where(Memory.user_id == "alice"))


def test_structured_report_has_identical_normal_and_recovery_text():
    from research_app.research import normalize_report

    content = [
        {"type": "text", "text": "Cited report [1]"},
        {"type": "reasoning", "reasoning": "not a report"},
        {"type": "text", "text": "Sources: document page 1"},
    ]
    assert normalize_report(content) == "Cited report [1]\nSources: document page 1"
    with pytest.raises(RunStopped, match="no text report"):
        normalize_report([])


def test_page_allowlist_is_reconstructed_after_worker_restart(context):
    settings, factory = context
    with factory.begin() as session:
        from research_app.db import add_event

        add_event(session, "run", "sources", {"urls": ["https://example.test/evidence"]})
    tools = RunTools("run", factory, settings)
    assert tools.urls == {"https://example.test/evidence"}
