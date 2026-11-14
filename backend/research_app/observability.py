"""Best-effort metadata-only Langfuse ingestion via LangChain callbacks."""

import logging
from datetime import datetime, timezone
from uuid import uuid4

import httpx
from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class LangfuseMetadataCallback(BaseCallbackHandler):
    raise_error = False

    def __init__(self, settings, run_id, conversation_id=None, user_id=None):
        self.settings = settings
        self.run_id = run_id
        self.batch = []
        self.enabled = bool(
            settings.langfuse_base_url
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        )
        from hashlib import sha256

        self.append(
            "trace-create",
            {
                "id": run_id,
                "name": "research",
                "sessionId": conversation_id or run_id,
                "userId": sha256((user_id or "anonymous").encode()).hexdigest(),
                "metadata": {
                    "environment": settings.environment,
                    "release": getattr(settings, "release_id", "development"),
                },
                "timestamp": timestamp(),
            },
        )

    def append(self, kind, body):
        if self.enabled and len(self.batch) < 500:
            self.batch.append(
                {"id": str(uuid4()), "timestamp": timestamp(), "type": kind, "body": body}
            )

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        self.append(
            "generation-create",
            {"id": str(run_id), "traceId": self.run_id, "name": "model", "startTime": timestamp()},
        )

    def on_llm_end(self, response, *, run_id, **kwargs):
        usage = None
        if response.generations and response.generations[0]:
            metadata = getattr(response.generations[0][0].message, "usage_metadata", None)
            if metadata:
                usage = {
                    "input": metadata.get("input_tokens", 0),
                    "output": metadata.get("output_tokens", 0),
                    "total": metadata.get("total_tokens", 0),
                }
        body = {"id": str(run_id), "endTime": timestamp()}
        if usage:
            body["usage"] = usage
        self.append("generation-update", body)

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self.append(
            "span-create",
            {
                "id": str(run_id),
                "traceId": self.run_id,
                "name": (serialized or {}).get("name", "tool"),
                "startTime": timestamp(),
            },
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        self.append("span-update", {"id": str(run_id), "endTime": timestamp()})

    def on_llm_error(self, error, *, run_id, **kwargs):
        self.append(
            "generation-update",
            {
                "id": str(run_id),
                "endTime": timestamp(),
                "level": "ERROR",
                "statusMessage": "Model call failed",
            },
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.append(
            "span-update",
            {
                "id": str(run_id),
                "endTime": timestamp(),
                "level": "ERROR",
                "statusMessage": "Tool call failed",
            },
        )

    def flush(self):
        if not self.enabled or not self.batch:
            return
        try:
            response = httpx.post(
                self.settings.langfuse_base_url.rstrip("/") + "/api/public/ingestion",
                json={"batch": self.batch},
                auth=(self.settings.langfuse_public_key, self.settings.langfuse_secret_key),
                timeout=3,
            )
            response.raise_for_status()
            if response.status_code == 207 and response.json().get("errors"):
                logger.warning("Langfuse rejected some telemetry events")
        except Exception:
            # Do not log responses or credentials; tracing is never a dependency of result persistence.
            logger.warning("Langfuse telemetry unavailable")
        finally:
            self.batch.clear()
