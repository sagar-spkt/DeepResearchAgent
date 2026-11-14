# Local development

## Current execution boundary

Offline tests use a fake provider, but the default local application configuration uses real Amazon Bedrock responses; it never silently falls back to fake output. AWS deployment uses private vLLM. In this workspace, package installation, AWS profile/model discovery, real BGE CPU embeddings, a Bedrock tool-call request, one bounded Tavily-backed research run with rendered citations, a semantically phrased document retrieval answer, browser history/report/upload/attachment flow, Git initialization, Docker, PostgreSQL migration, local API/web startup and API/worker restart recovery against PostgreSQL checkpoints have been verified. Broad research quality, Cognito and AWS deployment remain unverified.

## Backend

Use Python 3.12. Create `.venv` if it does not exist (`uv venv --python 3.12 .venv`). The existing environment in this workspace is Python 3.12.14. In a network-enabled local terminal, install the Bedrock adapter and project if needed, then configure local settings:

```sh
uv pip install --python .venv/bin/python -e '.[bedrock,local-postgres,embeddings]' pytest==9.1.1 ruff==0.17.2 mypy==1.20.2
scripts/download_embedding_model.sh
test -e .env || cp .env.example .env
if test ! -e .env.platform; then
  printf 'LOCAL_DB_PASSWORD=%s\nLOCAL_LANGFUSE_ENCRYPTION_KEY=%s\n' "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" > .env.platform
  chmod 600 .env.platform
fi
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

Copy `.env.platform.example` to a private `.env.platform` and generate local secrets. Validate configuration before starting:

```sh
docker compose --env-file .env.platform config --quiet
docker compose --env-file .env.platform up --build
```

Compose includes pgvector, migration, API, worker and web, plus an optional `observability` profile for self-hosted Langfuse dependencies. That profile has not been started in this workspace: Docker Desktop allocates 8 GiB, while Langfuse's [resource guidance](https://langfuse.com/self-hosting/configuration/scaling) lists 8 GiB for ClickHouse alone plus its separate web, worker, PostgreSQL, Valkey and blob-storage services. Local research does not depend on Langfuse. Local Langfuse ingestion and admin trace viewing remain unverified. Application state is retained in named volumes; do not use `down -v` unless intentionally deleting local data. Bedrock in containers additionally needs a deliberately configured AWS credential source; host AWS_PROFILE alone does not mount credentials.

The npm lockfile is present; use `npm ci` for reproducible frontend dependencies. A cross-platform uv lock is still absent. The Dockerfiles are development build recipes; do not promote them as reproducible AWS release artifacts until locks and image digests have been resolved and verified. Building the `embeddings` backend target requires an immutable EMBEDDING_REVISION and preloads weights; runtime embedding requests are offline-only.

## Checks

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

The 20-case offline dataset verifies harness execution/source plumbing, not real research quality. Live evals require `--live` plus explicit model configuration and cost authorization. `scripts/benchmark_inference.py` measures a configured private vLLM endpoint; no GPU results have been fabricated.

## Observability boundary

Langfuse callbacks batch metadata-only generation/tool traces; transport failure does not destroy a report. Admin trace metadata is available through `/api/admin/traces` when project keys are configured and live permissions allow reads. The legacy native-console subpath proxy deliberately returns503. Optional separate-host Helm source serves the native console at `https://observability.<application-host>/` with per-request administrator authorization. It is disabled by default until NGINX, cookies, assets and revocation are tested in a browser. Do not treat metadata tests as a complete console integration. Console organization membership remains a separate owner-managed operation.
