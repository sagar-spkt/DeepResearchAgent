# Local development checklist

This checklist tracks the developer workstation setup separately from AWS release acceptance. Do not mark an external service call as verified by checking only a nonempty environment variable.

## Environment and services

- [x] Initialize local Git on `main` with the requested author identity; ignore local secrets/data.
- [x] Install the pinned Bedrock, PostgreSQL, test, lint and type-check tools.
- [x] Install CPU BAAI/bge-small-en-v1.5 embeddings, pin/download the model revision, and verify normalized 384-dimensional output.
- [x] Select real Bedrock inference in the ignored root `.env`; keep fake inference limited to explicit offline tests.
- [x] Start local pgvector/PostgreSQL and migrate schema plus LangGraph checkpoints.
- [x] Verify the real Bedrock Converse tool-call path using a synthetic prompt.
- [x] Build the Next.js frontend and start local API, worker and web services.
- [x] Confirm Tavily credentials with one bounded search call; never print or log the key.
- [x] Run one real Deep Agents research request through the API and worker with search calls capped for the smoke test; confirm source citations and that no synthetic provider ran.
- [x] Exercise local browser session, conversation history, report rendering, document upload/index status, attachment and document-grounded research.
- [x] Verify restart recovery against local PostgreSQL and checkpoints.

## Live local acceptance evidence

- A synthetic current-information request completed through the real Bedrock provider and Tavily, stored its report/events in PostgreSQL, and rendered source links in the browser. Tavily search use was capped at two calls for the smoke process; no key values were printed or logged.
- A synthetic TXT file was uploaded through the browser, reached `ready`, was attached to the conversation, and was cited by a real Bedrock research run. This check exposed that models could skip the optional retrieval tool. New runs now receive an automatic owner-scoped retrieval of ready attached evidence before the first model turn; the retrieval tool remains available for later uploads.
- The pinned BGE model was loaded on CPU, the synthetic upload was re-indexed with real embeddings, and a paraphrased query retrieved and cited it in a real Bedrock run.
- A 127.0.0.1 browser origin produced the expected same-origin rejection because the local UI is configured for `http://localhost:3000`. All browser checks passed at the documented `http://localhost:3000` origin.
- After stopping and restarting both API and worker, the API returned ready, and PostgreSQL still returned the completed run, attached ready document and document-use record. The LangGraph checkpoint for the conversation loaded from PostgreSQL.

## Local completion criteria

Local setup is complete when all checks above pass, services use the documented `.env` files, no secret is tracked by Git, and failures remain explicit. Browser/Cognito, live model-quality, AWS restore/promotion/purge and deployment checks stay on the main project roadmap.

## Optional local observability

The self-hosted Langfuse Compose profile remains off. Docker Desktop currently allocates 8 GiB to containers; Langfuse's [self-hosted resource guidance](https://langfuse.com/self-hosting/configuration/scaling) lists a ClickHouse minimum of 8 GiB by itself, alongside separate web, worker, PostgreSQL, Valkey and blob-storage services. The main research app runs without Langfuse. Local Langfuse ingestion, console bootstrap and admin-only trace viewing remain an explicit follow-up before validating observability locally.
