# Local development

## Current execution boundary

Offline tests explicitly select fake providers, but the default local application configuration uses real Amazon Bedrock responses and real embeddings; it never silently falls back to fake output. Runtime startup now requires an explicit Bedrock model ID and embedding model. AWS staging and production use Bedrock Nova Pro (ADR-0012). In this workspace, package installation, AWS profile/model discovery, real BGE CPU embeddings, a Bedrock tool-call request, one bounded Tavily-backed research run with rendered citations, a semantically phrased document retrieval answer, browser history/report/upload/attachment flow, Git initialization, Docker, PostgreSQL migration, local API/web startup and API/worker restart recovery against PostgreSQL checkpoints have been verified. Cognito sign-in and AWS deployment have been verified on staging and production; broad research quality remains unverified.

## Backend

Use Python 3.12 and uv 0.12.5. The lock was resolved with this uv version. In a network-enabled local terminal, create the project environment and install the project plus its local development extras with the lock:

```sh
uv sync --locked --extra bedrock --extra local-postgres --extra embeddings --group dev --group build
scripts/download_embedding_model.sh
test -e .env || cp .env.example .env
test -e .env.platform || cp .env.platform.example .env.platform
python3 scripts/setup_local_langfuse.py
```

Start PostgreSQL/pgvector, and wait for its health check before migrating:

```sh
docker compose --env-file .env.platform up -d postgres
docker compose --env-file .env.platform ps postgres
```

Once PostgreSQL reports healthy, migrate:

```sh
set -a; source .env; source .env.platform; set +a
export DATABASE_URL="postgresql+psycopg://research:${LOCAL_DB_PASSWORD}@127.0.0.1:5432/research"
export PYTHONPATH=backend
.venv/bin/python -m research_app.manage migrate
```

`.env.platform` holds local secrets and is ignored by Git. The hex database password avoids reserved URI characters. The workspace has owner-only local env files. You can instead leave `DATABASE_URL` on SQLite and omit Docker and the `local-postgres` extra.

Start the API with:

```sh
set -a; source .env; source .env.platform; set +a
export DATABASE_URL="postgresql+psycopg://research:${LOCAL_DB_PASSWORD}@127.0.0.1:5432/research"
PYTHONPATH=backend .venv/bin/python -m uvicorn research_app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```sh
set -a; source .env; source .env.platform; set +a
export DATABASE_URL="postgresql+psycopg://research:${LOCAL_DB_PASSWORD}@127.0.0.1:5432/research"
PYTHONPATH=backend .venv/bin/python -m research_app.worker
```

The checked-in `.env.example` selects Amazon Nova Lite through Bedrock, the `research-operator` profile in `us-east-1`, SQLite and CPU BAAI/bge-small-en-v1.5 embeddings. Install the pinned `embeddings` extra and run `scripts/download_embedding_model.sh` before indexing documents. The script downloads model files from a fixed public repository revision into ignored `local-data`; it does not run during application requests. Nova Lite supports Converse tool calling and is available in `us-east-1`; inference is billed per token, and model access or AWS account credits vary by account. Check the current [Bedrock pricing page](https://aws.amazon.com/bedrock/pricing/) and [Nova Lite model card](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-amazon-nova-lite.html) before use. A synthetic Converse tool-call smoke test and one bounded live research request through Bedrock plus Tavily passed. The latter rendered source links in the browser; this is integration evidence, not a broad quality evaluation. Configure `TAVILY_API_KEY` for live web research. Local API calls require `X-Dev-User: demo@example.test`; this is the bootstrap owner. Other email identities start pending. Never expose this local identity simulator to an untrusted network. Use `MODEL_PROVIDER=fake` only when intentionally running offline tests; it produces synthetic reports.

At the start of each new run, the worker retrieves relevant chunks from ready documents explicitly attached to that user's conversation and includes the bounded results as untrusted evidence in the first model turn. The retrieval tool remains available during a run so uploads that become ready later can be picked up. One synthetic TXT upload was verified end to end through browser upload, BGE indexing, explicit attachment and a cited real-model answer. This does not establish general retrieval quality for large or scanned files.

Configuration is read from process environment, not automatically from a backend `.env` file. Load `.env` in each backend terminal before migration, API and worker startup. All processes must use the same database and model configuration. Keep paid testing bounded. Tavily is optional and free-plan exhaustion is visible.

The `local-postgres` extra installs the pinned psycopg binary wheel for developer machines without system libpq. The backend container uses the OS libpq package. Migration also prepares LangGraph checkpoint storage. Do not use SQLite as evidence for PostgreSQL concurrency or vector behavior.

## Frontend

```sh
test -e web/.env.local || cp web/.env.example web/.env.local
npm --prefix web ci
npm --prefix web run dev
```

Open localhost:3000. The Next.js server adds the local identity when configured; browser-supplied identity/authorization headers are discarded. The page offers research history, uploads/library, explicit memories and owner/admin controls. Cloud auth uses Cognito hosted signup/login/recovery with PKCE and an encrypted HttpOnly session. Account approvals are checked by the API. On Cognito callback, a signed ID token is bound to the authenticated access-token subject to save its verified display email; token groups never grant application roles. The ID token is not stored in the browser session.

Research prompts have a 6,000-byte UTF-8 budget; upload long source material instead. Files are limited to 20 MiB, 50 documents and 200 MiB per user initially. Parsing failures remain visible. Document use is recorded per run and replayable. Reports render Markdown without raw HTML.

## Containers

Copy `.env.platform.example` to a private `.env.platform`, download the pinned BGE model as described above, and generate local secrets. The Compose API, worker and migration service select Bedrock and real sentence-transformer embeddings; they do not default to fake providers. Their images include the locked Bedrock and embedding dependencies, and mount the local model read-only. Compose does not mount the host AWS profile, so configure an appropriate container credential source before making model requests; otherwise requests fail visibly instead of returning synthetic reports. For normal local Bedrock development with `research-operator`, use the host API/worker commands above, where the host AWS profile is available.

```sh
docker compose --env-file .env.platform config --quiet
docker compose --env-file .env.platform up --build
```

Compose includes pgvector, migration, API, worker and web, plus an optional `observability` profile for self-hosted Langfuse dependencies. Docker Desktop currently allocates 8 GiB; Langfuse's [resource guidance](https://langfuse.com/self-hosting/configuration/scaling) lists 8 GiB for ClickHouse alone plus its separate web, worker, PostgreSQL, Valkey and blob-storage services. The profile uses authenticated SeaweedFS S3-compatible event storage and authenticated Valkey. Startup and a synthetic callback-to-trace readback have been verified locally. The observed low-load memory use was about 1.3 GiB across Langfuse web/worker, ClickHouse, SeaweedFS, and Valkey; actual resource use rises with trace volume. Start only Langfuse and its dependencies if the API/web are already running on the host:

The backend image syncs the pinned Python runtime dependencies from `uv.lock` in a builder stage; the web image uses `npm ci`. Both Dockerfiles pin multi-platform Python/Node base image digests. Local image builds and disposable-container smoke checks have passed. The corrected Compose API, worker and migration images built with the locked Bedrock/embedding extras; a network-disabled non-root container loaded the local BGE model and returned a normalized 384-dimensional embedding. Compose uses the x86_64 CPU embedding target to match the supported Linux lock and AWS nodes. ECR image digests, SBOM/provenance, vulnerability scanning, and staging compatibility are still release tasks.

```sh
python3 scripts/setup_local_langfuse.py --enable-app-tracing
docker compose --env-file .env.platform --profile observability up -d valkey clickhouse seaweedfs seaweedfs-init langfuse langfuse-worker
docker compose --env-file .env.platform --profile observability ps
```

Open `http://localhost:3001` and sign in as `demo@example.test`. The generated owner password is stored as `LANGFUSE_INIT_USER_PASSWORD` in ignored `.env.platform`; project keys are stored there and copied into ignored `.env` for host API/worker processes. When running the application services in Compose, the same option configures their internal Langfuse URL (`http://langfuse:3000`) and project keys. Open signup remains disabled. Restart host API and worker after enabling app tracing. The app admin page reads trace metadata through `/api/admin/traces`; non-admin requests are rejected server-side. Native Langfuse console proxying through the app remains disabled. Application state is retained in named volumes; do not use `down -v` unless intentionally deleting local data. Bedrock in containers additionally needs a deliberately configured AWS credential source; host AWS_PROFILE alone does not mount credentials.

The npm lockfile is present; use `npm ci` for reproducible frontend dependencies. Python dependencies, including the pinned Hatchling/editables build backend, are resolved in `uv.lock`; sync with `uv sync --locked --group dev --group build` (add the configured extras as needed, including `--extra bedrock` for Bedrock client tests). The current CI and backend Docker build use that same lock and disable build isolation only for this project package. Local web and embeddings images have Trivy vulnerability/license reports; the application images are not published or staging-verified artifacts. Hosted Repository and Application checks on commit `497519f4bc55a8775a6bbc68f789d2d92d07b12d` passed the jobs present at that time, but that commit predates the current lock-based setup, container-security job, and infrastructure workflow. On `a6aaca27b60c41c143029e40ea19d65f17f1328f`, Repository and Infrastructure checks passed, and the container scan/SBOM, frontend behavior, and frontend build jobs passed. The Application workflow's backend test failed because its locked install omitted the `bedrock` extra; the local fix adds it, but that fix has not had a hosted rerun. Require ECR digests, provenance, release-artifact scan review, full license review and release validation before promotion. The optional `embeddings` extra selects CPU-only PyTorch for Linux amd64. Building that backend target requires an immutable `EMBEDDING_REVISION` and preloads BGE weights; the final image sets a local model path and disables Hub downloads. For the AWS x86_64 target, build with:

```sh
docker build --platform linux/amd64 --target embeddings \
  --build-arg EMBEDDING_REVISION=5c38ec7c405ec4b44b94cc5a9bb96e735b38267a \
  -t deepresearch/backend:local-embeddings -f Dockerfile.backend .
```

## Checks

The repository checker requires system Bash on `PATH` and uses it only with `-n` to parse GitHub Actions command blocks; it does not execute them.

```sh
python3 scripts/check_repository.py
python3 -m unittest discover -s tests -v
.venv/bin/ruff check backend scripts tests
.venv/bin/mypy backend/research_app --no-incremental
.venv/bin/python -m pytest backend/tests -q
node --test web/tests/*.test.mjs
npm --prefix web run check
.venv/bin/python scripts/run_evals.py --output local-data/evals/offline.json
```

The 20-case offline dataset verifies harness execution/source plumbing, not real research quality. Live evals require `--live` plus explicit model configuration and cost authorization.

## Observability boundary

Langfuse callbacks record every model and tool call with timing, token usage, errors and (with `LANGFUSE_CAPTURE_IO=true`, the default) prompts, completions, tool arguments/results and the run's question and report; transport failure does not destroy a report. Admin trace metadata is available through `/api/admin/traces` when project keys are configured and live permissions allow reads. The native Langfuse console is not exposed publicly; in AWS reach it with `kubectl port-forward` (see `infra/README.md`).
