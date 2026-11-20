"""Best-effort Langfuse ingestion via LangChain callbacks.

Every model and tool call becomes an observation with timing, token usage and
errors. When ``settings.langfuse_capture_io`` is true (the default) the trace
also records prompts, completions, tool arguments/results, the model name and
the run's question and final report, truncated to ``MAX_FIELD_CHARS``.
Telemetry failures never affect research results.
"""

import json
import logging
import time
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

import httpx
from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)

MAX_FIELD_CHARS = 20_000
MAX_EVENTS = 2_000
MAX_BATCH_BYTES = 1_000_000
MAX_EVENT_BYTES = 900_000
FLUSH_DEADLINE_SECONDS = 15


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def truncate(value, limit=MAX_FIELD_CHARS):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value if isinstance(value, str) else json.loads(text)
    return text[:limit] + f"... [truncated {len(text) - limit} characters]"


def _message(message):
    item = {"role": getattr(message, "type", "message"), "content": truncate(message.content)}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        item["tool_calls"] = [
            {"name": call.get("name"), "args": truncate(call.get("args"))} for call in tool_calls
        ]
    if getattr(message, "name", None):
        item["name"] = message.name
    return item


def _model_name(serialized, kwargs):
    metadata = kwargs.get("metadata") or {}
    if isinstance(metadata.get("ls_model_name"), str) and metadata["ls_model_name"]:
        return metadata["ls_model_name"]
    params = kwargs.get("invocation_params") or {}
    init = (serialized or {}).get("kwargs") or {}
    for source in (params, init):
        for key in ("model_id", "model", "model_name"):
            if isinstance(source.get(key), str) and source[key]:
                return source[key]
    return None


class LangfuseCallback(BaseCallbackHandler):
    raise_error = False

    def __init__(
        self,
        settings,
        run_id,
        conversation_id=None,
        user_id=None,
        usage_collector=None,
        question=None,
    ):
        self.settings = settings
        self.run_id = run_id
        self.usage_collector = usage_collector
        self.capture = bool(getattr(settings, "langfuse_capture_io", True))
        self.environment = getattr(settings, "environment", None)
        self.batch = []
        self.enabled = bool(
            settings.langfuse_base_url
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        )
        trace = {
            "id": run_id,
            "name": "research",
            "sessionId": conversation_id or run_id,
            "userId": sha256((user_id or "anonymous").encode()).hexdigest(),
            "metadata": {
                "environment": settings.environment,
                "release": getattr(settings, "release_id", "development"),
            },
            "release": getattr(settings, "release_id", "development"),
            "timestamp": timestamp(),
        }
        self.trace_timestamp = trace["timestamp"]
        if self.capture and question is not None:
            trace["input"] = truncate(question)
        self.append("trace-create", trace)

    def append(self, kind, body, reserved=False):
        # Keep one slot for the final trace output so a capped run still shows its report.
        if not self.enabled or len(self.batch) >= MAX_EVENTS - (0 if reserved else 1):
            return
        if self.environment:
            body.setdefault("environment", self.environment)
        self.batch.append({"id": str(uuid4()), "timestamp": timestamp(), "type": kind, "body": body})

    def set_output(self, report):
        """Record the final report on the trace (an upsert of the same trace id)."""
        if self.capture:
            self.append(
                "trace-create",
                {"id": self.run_id, "timestamp": self.trace_timestamp, "output": truncate(report)},
                reserved=True,
            )

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        body = {"id": str(run_id), "traceId": self.run_id, "name": "model", "startTime": timestamp()}
        if self.capture:
            body["input"] = [_message(m) for batch in messages for m in batch]
            model = _model_name(serialized, kwargs)
            if model:
                body["model"] = model
        self.append("generation-create", body)

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = None
        message = None
        if response.generations and response.generations[0]:
            message = getattr(response.generations[0][0], "message", None)
            metadata = getattr(message, "usage_metadata", None)
            if (
                isinstance(metadata, dict)
                and type(metadata.get("input_tokens")) is int
                and type(metadata.get("output_tokens")) is int
                and metadata["input_tokens"] >= 0
                and metadata["output_tokens"] >= 0
            ):
                usage = {
                    "input": metadata.get("input_tokens", 0),
                    "output": metadata.get("output_tokens", 0),
                    "total": metadata.get("total_tokens", 0),
                }
                if self.usage_collector is not None:
                    self.usage_collector.record_model_usage(usage["input"], usage["output"])
        body = {"id": str(run_id), "endTime": timestamp()}
        if usage:
            body["usage"] = usage
        if self.capture and message is not None:
            body["output"] = _message(message)
        self.append("generation-update", body)

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        body = {
            "id": str(run_id),
            "traceId": self.run_id,
            "name": (serialized or {}).get("name", "tool"),
            "startTime": timestamp(),
        }
        if self.capture:
            body["input"] = truncate(kwargs.get("inputs") or input_str)
        self.append("span-create", body)

    def on_tool_end(self, output, *, run_id, **kwargs):
        body = {"id": str(run_id), "endTime": timestamp()}
        if self.capture:
            body["output"] = truncate(str(getattr(output, "content", output)))
        self.append("span-update", body)

    def _error(self, label, error):
        if self.capture:
            return truncate(f"{label}: {type(error).__name__}: {error}", 2_000)
        return label

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.append(
            "generation-update",
            {
                "id": str(run_id),
                "endTime": timestamp(),
                "level": "ERROR",
                "statusMessage": self._error("Model call failed", error),
            },
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.append(
            "span-update",
            {
                "id": str(run_id),
                "endTime": timestamp(),
                "level": "ERROR",
                "statusMessage": self._error("Tool call failed", error),
            },
        )

    @staticmethod
    def _bounded(event):
        """Shrink a single oversized event so it cannot be rejected on its own."""
        size = len(json.dumps(event, ensure_ascii=False, default=str).encode())
        if size <= MAX_EVENT_BYTES:
            return event, size
        body = dict(event["body"])
        for field in ("input", "output"):
            if field in body:
                body[field] = truncate(body[field], MAX_EVENT_BYTES // 4)
        event = {**event, "body": body}
        return event, len(json.dumps(event, ensure_ascii=False, default=str).encode())

    def _chunks(self):
        chunk, size = [], 0
        for event in self.batch:
            event, event_size = self._bounded(event)
            if chunk and size + event_size > MAX_BATCH_BYTES:
                yield chunk
                chunk, size = [], 0
            chunk.append(event)
            size += event_size
        if chunk:
            yield chunk

    def flush(self):
        if not self.enabled or not self.batch:
            return
        # Flushing runs before the worker records the result, so bound the total time.
        deadline = time.monotonic() + FLUSH_DEADLINE_SECONDS
        try:
            for chunk in self._chunks():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("Langfuse telemetry flush timed out; remaining events dropped")
                    break
                try:
                    response = httpx.post(
                        self.settings.langfuse_base_url.rstrip("/") + "/api/public/ingestion",
                        content=json.dumps({"batch": chunk}, ensure_ascii=False, default=str),
                        headers={"Content-Type": "application/json"},
                        auth=(self.settings.langfuse_public_key, self.settings.langfuse_secret_key),
                        timeout=min(10, remaining),
                    )
                    response.raise_for_status()
                    if response.status_code == 207 and response.json().get("errors"):
                        logger.warning("Langfuse rejected some telemetry events")
                except Exception:
                    # One rejected chunk must not drop the others. Never log responses or credentials.
                    logger.warning("Langfuse telemetry unavailable")
        finally:
            self.batch.clear()

