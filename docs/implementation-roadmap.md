# Implementation roadmap

`[x]` records implemented work with the stated verification scope. `[ ]` is incomplete, including source awaiting live verification. Local source integration follows tested interfaces; browser, container and cloud release gates stay separate and mandatory. No unchecked gate is waived by passing offline tests.

## 1. Local application foundation

- [x] Pin direct Python dependencies; implement FastAPI configuration, explicit migration and owner bootstrap commands.
- [x] Separate local identity from Cognito access-token validation, bind verified display email using signed ID tokens, and reject development identities in cloud configuration.
- [x] Persist conversations, messages, explicit memories and approval/owner/admin permissions.
- [x] Test cross-user isolation, approval revocation and last-owner protection; obtain independent code review.
- [x] Resolve and review a cross-platform Python dependency lock and make CI consume it with locked sync.

Acceptance evidence: API tests exercise synthetic signed tokens and local users. Live Cognito has not been tested. SQLite is not evidence of PostgreSQL behavior.

## 2. Durable research execution

- [x] Implement bounded admission, durable events, singleton worker lease, heartbeat, retry and duplicate-result fencing.
- [x] Connect the actual Deep Agents SDK to explicit fake, Bedrock and vLLM adapters with persistent checkpoints.
- [x] Fence checkpoint/event mutations against stale workers and enforce cancellation/time/search/subagent limits.
- [x] Implement attached-document retrieval, bounded Tavily search/page reading, source provenance and report storage.
- [x] Test SDK execution, completed-checkpoint recovery, structured reports, SSE replay and API-to-worker round trips.
- [ ] Test real Bedrock/vLLM tool calls, tokenizer budgets, parser/template compatibility and failure recovery.

Acceptance evidence: offline mode uses the real SDK without paid calls. Search credits and source use are durable across worker reconstruction. One bounded live Bedrock + Tavily research run completed and rendered sources; this is integration evidence, not a model-quality evaluation.

## 3. Document ingestion and context

- [x] Implement private size/quota-bounded uploads, asynchronous parsing/indexing and visible failures.
- [x] Implement explicitly attached, owner-scoped hybrid retrieval with page/section provenance.
- [x] Refresh attachments at tool boundaries and persist/display document-use evidence per run.
- [x] Use SDK summarization/offloading and bounded explicitly saved memory on each turn.
- [x] Test malformed sources, deletion, stale ingestion completion, isolation and 100-turn durable context management.
- [x] Run local PostgreSQL/pgvector integration and real CPU embedding-model tests with pinned cached weights.

Acceptance evidence: the 100-turn fixture retains the durable transcript while the model receives a compacted view and older history is offloaded. No automatic permanent memory writes are exposed to the agent.

## 4. Web application

- [x] Implement Next.js/assistant-ui history, research progress/report and cancellation screens.
- [x] Implement Cognito PKCE, encrypted HttpOnly sessions and local-only development identity.
- [x] Add private documents, explicit memories, approvals, role management and trace metadata screens.
- [x] Test session tampering, CSRF, SSE parsing, conversation-selection races, document-use replay and cookie migration.
- [x] Resolve/review npm lock, install dependencies and pass the full Next.js build.
- [x] Complete local browser acceptance for identity/session persistence, history, upload/indexing/use, active-run reconnect/cancellation, memory CRUD, owner/admin role visibility, and non-admin denial.
- [ ] Complete Cognito signup/login/recovery and cloud admin revocation in staging (tracked by LCL-06).

Acceptance evidence: 16 standalone Node tests and syntax checks pass; optimized Next.js production build succeeds. Chrome exercised local session/history, upload/indexing/attachment, real research completion, app admin trace metadata, native Langfuse trace details, memory CRUD, local approval and role revocation, and active-run reconnect/cancellation. A no-model lifecycle harness used the real BGE document worker and `RunTools.retrieve_documents` during an active browser run, then verified persisted document-use events; it bypassed LLM generation and does not establish model tool-call selection. A contradictory file-access sentence and high token use remain quality/cost findings. Cognito signup/login/recovery and cloud admin revocation remain unverified. Backend authorization remains authoritative.

## 5. Observability and evaluation

- [x] Add best-effort metadata-only Langfuse callbacks with release/conversation/run correlation and error/usage reporting.
- [x] Protect administrator trace metadata and auth-request endpoints with current database roles.
- [x] Add optional separate-host native console proxy source, strict child-host cookie scope, duplicate-cookie rejection and no WebSocket upgrades.
- [x] Add 20 versioned synthetic research cases, offline runner and explicit vLLM benchmark output format.
- [ ] Verify native NGINX/Next/Langfuse cookies, sign-in, assets, redirects and revocation end to end before enabling the console.
- [ ] Publish live model/citation/grounding evaluations and measured GPU efficiency results.

Acceptance evidence: telemetry failure does not lose reports; admin revocation is tested against API access. Offline harness results are not model-quality or GPU-performance scores.

## 6. Containers and AWS staging

- [x] Add local Compose, API/worker/web build recipes, explicit migrations and readiness checks.
- [x] Add separate retained/runtime Terraform and EKS/RDS/S3/SQS/Cognito/ECR/IAM source.
- [x] Add Helm app/model/observability deployments, private model networking and separate frontend identity.
- [x] Review IAM/settings/ingress integration; run Terraform format, Helm lint/render and Compose configuration checks.
- [x] Resolve cross-platform Terraform provider locks for all root modules and run provider schema validation.
- [ ] Select intended EKS/Kubernetes and controller versions; validate Helm compatibility and pin verified container/model digests.
- [x] Start local pgvector/PostgreSQL, install the pinned local psycopg binary extra, run schema/checkpoint migration and verify API readiness.
- [x] Download the pinned BGE embedding weights, verify CPU inference and re-index local documents with real vectors.
- [x] Verify low-load local Langfuse/ClickHouse/Valkey/SeaweedFS integration and metadata-only admin trace view.
- [ ] Complete controller/bootstrap IAM, secret materialization, domain/ACM setup and live quota/cost checks.
- [ ] Deploy and smoke-test staging only after all admission, lifecycle and cost prerequisites pass.

Acceptance evidence: rendered manifests reject development cloud profiles and require immutable release inputs. No AWS resources have been provisioned.

## 7. Delivery and lifecycle

- [x] Add offline PR CI and explicit build/release gates; keep unsupported deploy/purge operations closed.
- [x] Implement hashed plans, provider-bound backup verification, durable receipts and exact saved-runtime-plan shutdown building blocks.
- [x] Reject cross-account/region/resource scope, provisioners, changed artifacts and writers that resumed during shutdown waits.
- [x] Add separate Scheduler/Step Functions/CodeBuild expiry source with pinned artifacts and retained receipts.
- [x] Add retained DynamoDB control source, transactional global admission/month reservations and renewable CLI lifecycle leases.
- [x] Independently review control-store admission/lease logic and regress lost-lease and conflicting-environment failures.
- [ ] Live-test control-store integration and enforce the common lease for every deploy/manual/expiry writer.
- [x] Implement provider-scoped teardown inventory and conditional admission release; never clear a session merely because time passed.
- [x] Tag newly provisioned retained observability volumes by environment, session, component and retention intent; independently review and validate chart rendering.
- [x] Collect regional runtime/network/storage identities with pagination and explicit blockers for unknown resources or denied enumeration.
- [x] Archive a fresh completion digest and conditionally release only the matching admitted session under the current lease.
- [x] Independently review and regress successful release, incomplete inventory, changed admission and lease loss.
- [ ] Complete ClickHouse backup credentials/IAM and actual database/ClickHouse/Valkey restoration.
- [ ] Implement protected OIDC deployment, unchanged-image production promotion and explicit retained-data purge.
- [ ] Rehearse backup -> runtime destruction -> restore -> next-session admission, including failed-expiry recovery.
- [ ] Publish measured costs, real-model results and final release review.

Acceptance evidence: lifecycle tests use fake clients/runners only. Manual mutation requires a reviewed hash, explicit execute, matching admission and current lease. The six-hour reservation includes four demo hours plus cleanup allowance; its twenty-hour UTC-month ceiling is an admission budget, not a hard billing cap.

## Verification and current blockers

Available checks are documented in docs/local-development.md. Current verified scope includes backend regression tests with actual SDK execution/100 turns, standalone frontend behavior, offline 20-case harness, repository/lifecycle/control fixtures, lint/types and static deployment rendering. Historical baseline pass: 27 backend tests, 79 repository/lifecycle/control tests, 15 Node tests, and 20/20 synthetic harness cases passed at that earlier revision. Ruff and backend mypy checks passed at that revision. These historical counts do not include GPU or AWS release tests; see the remaining-work plan for the latest local repository-check count.

Unverified or missing: Cognito and staging browser acceptance; broad live Bedrock research quality/cost beyond the single bounded sample; GPU results; hosted scans and verified artifact provenance; final account-cost and staging preflight; protected OIDC and lifecycle workflows; complete purge/promotion/restore; live teardown inventory/expiry and enforcement against out-of-band AWS administrators. Local PostgreSQL migration and checkpoint recovery, pinned BGE CPU inference/re-indexing, bounded live Bedrock + Tavily flows, local browser acceptance, low-load Langfuse ingestion, and the active-run no-model lifecycle paths are verified. The Cloud runtime skill is present, but no callable Cloud environment tools were found; plugin discovery did not find a suitable connected execution environment. No alternative hosting provider was selected. See `docs/remaining-work-plan.md` for task-level acceptance and execution order.

Paid deployment admission remains closed. Continue with these unchecked items rather than treating the repository as production-ready.
