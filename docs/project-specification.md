# Project specification

Status: agreed v1 direction; initial local implementation and platform source exist. Local Bedrock/Tavily research, PostgreSQL/checkpoint restart recovery, and core browser upload/history/report flows have been exercised with synthetic data. Broad model quality, local Langfuse, real CPU embedding weights and cloud lifecycle remain unverified. This document records the conversation decisions. Changes to requirements need explicit agreement; measured defaults can be refined with evidence and an ADR.

## Product requirements

Build a general-purpose, multiuser deep research portfolio application with the open-source Deep Agents SDK (not Managed Deep Agents). Research web pages and uploaded documents and produce reports with source citations. No LangSmith hosted-service dependency; the SDK transitively installs its open-source client with tracing disabled (ADR-0001). Use Next.js and assistant-ui with a Python FastAPI backend and a separate durable Deep Agents worker.

Cognito email/password signup, verification and recovery; administrator approval is required for research. Five signed-in users and one active research run initially, with a bounded queue. Roles: owner, administrator, user; prevent removal of the last owner. Enforce identity and ownership on the server for all APIs, files, retrieval, events and state.

Support durable conversations, streamed progress, reconnect, cancellation, retries, worker leases and duplicate queue delivery. Default run limit: 15 minutes, ten web searches, and up to two parallel research subagents. Persist run state before execution.

Uploads: PDF, DOCX, TXT and Markdown, maximum 20 MB each. Accept uploads during conversation/research; parse/index asynchronously and use at the next tool boundary after readiness. Show whether a run used a newly uploaded file. Originals are private S3 objects; page/section citations are retained. Documents attach to their conversation and enter a private library; reuse in another conversation requires explicit selection. OCR is outside v1.

PostgreSQL/pgvector plus keyword retrieval; CPU-hosted BAAI/bge-small-en-v1.5 embeddings as an initial measurable default. Deep Agents context summarization, result offloading and PostgreSQL LangGraph checkpoints support long conversations. Cross-conversation saved memory is explicit and user-viewable/editable/deletable; no automatic permanent personal memory.

Use Tavily free credits through a replaceable adapter. Do not enable paid upgrades; expose exhaustion clearly. No arbitrary code execution, browser automation, payments, or uptime SLA in v1. Initial UI/evaluation language is English.

## Model service

AWS staging/production use private, separately deployed vLLM with an OpenAI-compatible /v1 API, rotatable service keys in Secrets Manager, and network restrictions. App users receive no inference service key. Administrator testing uses an authenticated tunnel. Do not rely on vLLM API-key authentication to secure every server endpoint.

Local real-model tests use Bedrock through ChatBedrockConverse with an explicitly configured accessible model ID and local AWS profile credentials. Fake providers support offline tests. The swappable model factory supports vllm, bedrock and fake; no silent paid fallback.

Qwen3-8B BF16 is the baseline, not a proven best model. Compare Qwen3-14B, Qwen3.5-9B and gpt-oss-20b on the actual GPU before release selection; validate parser/template/quantization compatibility. Pin the winning model revision and serving image. Initial context cap is 16K; validate continuous batching, chunked prefill and supported prefix caching with per-user cache isolation. Qwen3.5 hybrid caching requires extra validation.

## AWS architecture and environments

Region us-east-1. Local Docker Compose; separate Terraform state, credentials, identity pools, buckets and backups for staging and production in one AWS account. AWS environments run sequentially; do not assume a quota increase.

Docker/ECR images by digest; EKS/Helm; initial CPU node m6i.2xlarge and dedicated tainted g6e.xlarge GPU node. Current G/VT quota is four vCPUs, allowing one four-vCPU GPU instance at a time in the region. Validate CPU resource sizing, account quotas and capacity before deployment.

Two-AZ VPC. Budget-focused nodes have public-subnet outbound internet but no unsolicited inbound access; RDS is private. Restrict Kubernetes API access. ALB with ACM/Route 53 exposes the application only; inference, data services and Langfuse remain private. An owned domain is a deployment prerequisite.

Single-AZ RDS PostgreSQL/pgvector, separate application and Langfuse databases/users; S3 private encrypted files/backups; SQS plus dead-letter queue; Cognito; Secrets Manager; least-privilege workload IAM; CloudWatch with short retention. Do not imply high availability with a single node/database instance.

## Observability and evaluation

Self-host Langfuse with its required PostgreSQL, ClickHouse, Valkey and S3 dependencies. Trace via LangChain callbacks, correlating environment/release/conversation/run. Record latency, token usage and tool failures; exclude raw documents and secrets by default. Langfuse failures must not destroy research results.

Keep Langfuse behind an administrator-authorized gateway; disable public signup and manage individual organization membership through its owner UI using free self-hosted capabilities. Do not assume Enterprise project roles or management APIs. Revoking application administrator access blocks the gateway immediately, including existing sessions.

Maintain a versioned 20-case research evaluation suite for tool use, citations, grounding and completion, with published results and human-reviewed limitations. Model quality, performance and the monthly cost target are unverified until measured.

## Delivery and lifecycle

GitHub Actions OIDC, PR checks, staging deployment, same-image production promotion through a protected environment, migrations and rollback. Separate deploy, backup/shutdown, restore and explicit full-destruction workflows will be implemented later.

Target under $200/month including Bedrock local usage. Four-hour demo sessions and 20 combined AWS environment hours/month by default. Generate current regional cost estimates including retained storage, addresses, snapshots, identity, secrets and model usage. AWS budget alerts are not hard caps.

An AWS-side expiry workflow backs up state and deletes runtime clusters, nodes, ALBs and RDS instances. Preserve identity, secrets, S3 data, database snapshots, ClickHouse backups and Terraform state for restoration. Abort dependent destructive steps if required backups fail and alert the owner. Separate explicit purge removes retained environment data; infrastructure state is removed last. EKS control planes must be deleted, not merely scaled down.

## Release acceptance

Demonstrate cross-user isolation, approval/admin revocation, mid-run uploads, reconnect, worker restart, cancellation and duplicate delivery. A 100-turn conversation survives compaction while explicitly saved facts remain accessible. Expose search exhaustion, malformed tool calls, inference failures and parsing failures as recoverable states.

Validate backup -> runtime destruction -> restore preserves accounts, conversations, retrieval and observability history. Inventory teardown for unintended billed resources. Publish GPU benchmarks and cost evidence. See the implementation roadmap for executed checks and remaining live release gates.
