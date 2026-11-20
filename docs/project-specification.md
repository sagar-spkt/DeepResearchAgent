# Project specification

Status: v1 is implemented and deployed to AWS staging through GitHub Actions.
Changes to requirements need explicit agreement; record consequential changes in
an ADR (see `docs/decisions/`).

## Product requirements

Build a general-purpose, multiuser deep research portfolio application with the open-source Deep Agents SDK (not Managed Deep Agents). Research web pages and uploaded documents and produce reports with source citations. No LangSmith hosted-service dependency; the SDK transitively installs its open-source client with tracing disabled (ADR-0001). Use Next.js and assistant-ui with a Python FastAPI backend and a separate durable Deep Agents worker.

Cognito email/password signup, verification and recovery; administrator approval is required for research. Five signed-in users and one active research run initially, with a bounded queue. Roles: owner, administrator, user; prevent removal of the last owner. Enforce identity and ownership on the server for all APIs, files, retrieval, events and state.

Support durable conversations, streamed progress, reconnect, cancellation, retries, worker leases and duplicate queue delivery. Default run limit: 15 minutes, ten web searches, and up to two parallel research subagents. Persist run state before execution.

Uploads: PDF, DOCX, TXT and Markdown, maximum 20 MB each. Accept uploads during conversation/research; parse/index asynchronously and use at the next tool boundary after readiness. Show whether a run used a newly uploaded file. Originals are private S3 objects; page/section citations are retained. Documents attach to their conversation and enter a private library; reuse in another conversation requires explicit selection. OCR is outside v1.

PostgreSQL/pgvector plus keyword retrieval; CPU-hosted BAAI/bge-small-en-v1.5 embeddings as an initial measurable default. Deep Agents context summarization, result offloading and PostgreSQL LangGraph checkpoints support long conversations. Cross-conversation saved memory is explicit and user-viewable/editable/deletable; no automatic permanent personal memory.

Use Tavily free credits through a replaceable adapter. Do not enable paid upgrades; expose exhaustion clearly. No arbitrary code execution, browser automation, payments, or uptime SLA in v1. Initial UI/evaluation language is English.

## Model service

Staging, production and local real-model runs use Amazon Bedrock (Nova Lite)
through `ChatBedrockConverse` with an explicitly configured model ID; fake
providers are for offline tests only and there is no silent paid fallback. The
model factory also supports an OpenAI-compatible vLLM endpoint for future
self-hosting (ADR-0012).

## AWS architecture and environments

Region us-east-1. Staging and production are separate, disposable environments,
each one Terraform root (`infra/aws`) with its own state, Cognito pool, buckets
and database. One two-AZ VPC; a single EKS CPU node in public subnets (no NAT);
private single-AZ RDS PostgreSQL/pgvector with separate application and Langfuse
databases/users; private S3 for documents and Langfuse events; SQS with a
dead-letter queue; Pod Identity roles per workload. An ALB with ACM/Route 53
exposes only the web app; the API, Langfuse and data services stay private.
Nothing here is highly available.

## Observability and evaluation

Self-host Langfuse with its required PostgreSQL, ClickHouse, Valkey and S3 dependencies. Trace via LangChain callbacks, correlating environment/release/conversation/run. Record latency, token usage and tool failures; exclude raw documents and secrets by default. Langfuse failures must not destroy research results.

Maintain a versioned 20-case research evaluation suite for tool use, citations, grounding and completion, with published results and human-reviewed limitations. Model quality, performance and the monthly cost target are unverified until measured.

## Delivery

GitHub Actions only: PR checks are required on `main`; the manual *Deploy*
workflow provisions/updates staging and runs smoke tests, and deploys
production only after staging succeeds in the same run; the manual *Destroy*
workflow deletes an environment and all its data. Target under $200/month;
destroy idle environments. See `infra/README.md`.

## Release acceptance

Demonstrate cross-user isolation, approval/admin revocation, mid-run uploads,
reconnect, worker restart, cancellation and duplicate delivery. A 100-turn
conversation survives compaction while explicitly saved facts remain
accessible. Expose search exhaustion, malformed tool calls, inference failures
and parsing failures as recoverable states.
