"""Explicit model adapters. Fake mode exercises the real tool-calling harness."""

import os
from uuid import uuid4

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult


class OfflineResearchModel(BaseChatModel):
    """Deterministic integration fixture, never a substitute for real research."""

    @property
    def _llm_type(self):
        return "offline-research-fixture"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        summary_request = any(
            "<messages>" in str(message.content) and "summar" in str(message.content).lower()
            for message in messages
        )
        if summary_request:
            message = AIMessage(
                content="Offline conversation summary: retain explicit saved memories and cited sources. This is synthetic test output."
            )
        elif messages and isinstance(messages[-1], ToolMessage):
            content = str(messages[-1].content)
            message = AIMessage(
                content="Offline fixture report — no live web research was performed.\n\n" + content
            )
        elif messages and isinstance(messages[-1], HumanMessage):
            query = str(messages[-1].content)[-2000:]
            message = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "retrieve_documents",
                        "args": {"query": query},
                        "id": str(uuid4()),
                        "type": "tool_call",
                    }
                ],
            )
        else:
            message = AIMessage(
                content="Offline conversation summary: retain explicit saved memories and cited sources. This is synthetic test output."
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def create_model(
    settings,
    user_id,
    *,
    max_output_tokens=None,
    max_retries=None,
    rate_limiter=None,
    cache=None,
):
    # LangChain has a transitive OSS LangSmith client, but tracing stays local/self-hosted.
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    output_limit = max_output_tokens or 2048
    profile = {"max_input_tokens": 16384, "max_output_tokens": output_limit}
    if settings.model_provider == "fake":
        if settings.environment != "local":
            raise ValueError("Fake inference is restricted to local mode")
        return OfflineResearchModel(profile=profile)
    if settings.model_provider == "vllm":
        from hashlib import sha256

        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=settings.vllm_model,
            base_url=settings.vllm_base_url,
            api_key=settings.vllm_api_key,
            temperature=0.2,
            max_tokens=output_limit,
            timeout=60,
            max_retries=1 if max_retries is None else max_retries,
            rate_limiter=rate_limiter,
            cache=cache,
            use_responses_api=False,
            profile=profile,
            extra_body={"cache_salt": sha256(user_id.encode()).hexdigest()},
        )
    if settings.model_provider == "bedrock":
        if not settings.bedrock_model_id:
            raise ValueError("An explicit Bedrock model ID is required")
        try:
            from langchain_aws import ChatBedrockConverse
        except ImportError as exc:
            raise RuntimeError("Install the bedrock optional dependency group") from exc
        from botocore.config import Config

        return ChatBedrockConverse(
            model=settings.bedrock_model_id,
            region_name=settings.aws_region,
            max_tokens=output_limit,
            temperature=0.2,
            profile=profile,
            max_retries=max_retries,
            rate_limiter=rate_limiter,
            cache=cache,
            config=Config(connect_timeout=5, read_timeout=60, retries={"total_max_attempts": 1}),
        )
    raise ValueError("Unknown model provider")
