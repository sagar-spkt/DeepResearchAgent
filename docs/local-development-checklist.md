# Local development checklist

This checklist tracks the developer workstation setup separately from AWS release acceptance. Do not mark an external service call as verified by checking only a nonempty environment variable.

## Environment and services

- [x] Initialize local Git on `main` with the requested author identity; ignore local secrets/data.
- [x] Install the pinned Bedrock, PostgreSQL, test, lint and type-check tools.
- [x] Install CPU BAAI/bge-small-en-v1.5 embeddings, pin/download the model revision, and verify normalized 384-dimensional output.
- [x] Select real Bedrock inference in the ignored root `.env`; keep fake inference limited to explicit offline tests.
- [x] Make host runtime settings fail closed without an explicit real Bedrock model ID and embedding model; offline fixtures opt into fake providers explicitly.
- [x] Start local pgvector/PostgreSQL and migrate schema plus LangGraph checkpoints.
- [x] Verify the real Bedrock Converse tool-call path using a synthetic prompt.
- [x] Build the Next.js frontend and start local API, worker and web services.
- [x] Confirm Tavily credentials with one bounded search call; never print or log the key.
- [x] Run one real Deep Agents research request through the API and worker with search calls capped for the smoke test; confirm source citations and that no synthetic provider ran.
- [x] Exercise local browser session, conversation history, report rendering, document upload/index status, attachment and document-grounded research.
- [x] Exercise active-run reconnect/cancellation and mid-run upload, real BGE indexing/retrieval, and document-use persistence through the browser using a temporary no-model worker lifecycle harness; no synthetic report was generated.
- [x] Verify restart recovery against local PostgreSQL and checkpoints.
- [x] Start the self-hosted Langfuse Compose profile with authenticated Valkey and SeaweedFS event storage; bootstrap its owner/project and verify callback ingestion/readback plus the admin-only trace metadata view.
- [x] Configure Compose API, worker and migration services to use real Bedrock and BGE providers instead of synthetic defaults; include worker Langfuse settings. Build the images and verify real BGE inference offline. Containerized Bedrock calls still need an explicit credential source and spend authorization.

## Live local acceptance evidence

- A synthetic current-information request completed through the real Bedrock provider and Tavily, stored its report/events in PostgreSQL, and rendered source links in the browser. Tavily search use was capped at two calls for the smoke process; no key values were printed or logged.
- A synthetic TXT file was uploaded through the browser, reached `ready`, was attached to the conversation, and was cited by a real Bedrock research run. This check exposed that models could skip the optional retrieval tool. New runs now receive an automatic owner-scoped retrieval of ready attached evidence before the first model turn; the retrieval tool remains available for later uploads.
- The pinned BGE model was loaded on CPU, the synthetic upload was re-indexed with real embeddings, and a paraphrased query retrieved and cited it in a real Bedrock run.
- A 127.0.0.1 browser origin produced the expected same-origin rejection because the local UI is configured for `http://localhost:3000`. All browser checks passed at the documented `http://localhost:3000` origin.
- After stopping and restarting both API and worker, the API returned ready, and PostgreSQL still returned the completed run, attached ready document and document-use record. The LangGraph checkpoint for the conversation loaded from PostgreSQL.
- A later Chrome pass uploaded a second synthetic TXT file to `ready`, attached a document to a new conversation, completed a real Bedrock/Tavily research run, reopened the earlier conversation from history, and showed the run in both the app's admin trace list and native Langfuse tracing UI. The app metadata view denied an unapproved identity (403). The Langfuse trace showed 56,559 input tokens and 931 output tokens; the report also included a contradictory file-access sentence. This is a quality/cost follow-up, not broad live evaluation.
- Chrome showed a Next.js hydration warning caused by Grammarly-injected attributes. This was browser-extension interference; no application-code change was made for it.

## Local completion criteria

Core local setup and the optional Langfuse profile have been exercised. Memory CRUD, local non-admin denial, active-run reconnect/cancellation, and mid-run upload/index/retrieval are covered by browser checks. The lifecycle harness used the real document worker and BGE retrieval code but intentionally bypassed model generation; it does not establish the real agent's tool-call choice. Remaining local work is the budgeted research quality/cost evaluation and any unverified application failure paths tracked in the project history (the remaining-work plan was retired by ADR-0012). Cognito, AWS restore/promotion/purge, and deployment remain separate release gates.

## Optional local observability

The self-hosted Langfuse Compose profile has been started locally with signup disabled. A synthetic metadata-only callback was read back through Langfuse; the browser showed model/tool spans and token counts while the trace root had no prompt or output content. The app admin trace list rendered metadata, and the API denied an unapproved identity. This verifies low-load local ingestion and metadata access, not production capacity or the optional native-console proxy. Docker Desktop allocates 8 GiB; Langfuse's [self-hosted resource guidance](https://langfuse.com/self-hosting/configuration/scaling) lists 8 GiB for ClickHouse alone for its recommended deployment sizing. Do not infer production capacity from the observed low-load local profile.
