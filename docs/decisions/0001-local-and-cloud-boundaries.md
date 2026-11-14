# ADR-0001: Explicit local and cloud execution boundaries

- Status: accepted
- Date: 2026-11-13

## Context

The portfolio must run offline tests, support local Bedrock testing and use self-hosted vLLM on AWS without leaking development identities or silently spending on fallback providers.

## Decision

Use explicit fake, Bedrock and vLLM adapters. Fake mode exercises the real Deep Agents SDK with a deterministic chat model and synthetic source text. SQLite supports isolated local tests; local Compose and AWS use PostgreSQL. Cloud settings reject fake models, development identity and local file storage. Bedrock model ID and vLLM endpoint/key are required when selected.

The LangChain SDK transitively installs the open-source LangSmith client. The project has no LangSmith hosted-service dependency: disable LangSmith tracing and route telemetry to self-hosted Langfuse. Removing a required transitive client would require unsupported dependency modification.

## Consequences and validation

Offline harness checks do not establish real-model research quality, PostgreSQL concurrency behavior or cloud deployment readiness. These require separate integration/evaluation evidence. Retain the agreed PostgreSQL checkpoints and production infrastructure interfaces rather than treating SQLite as production storage.
