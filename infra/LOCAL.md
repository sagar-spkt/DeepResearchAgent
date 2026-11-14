# Local dependencies

Copy .env.platform.example to an ignored .env.platform, choose a random database password and a 64-character hexadecimal encryption key. Run `docker compose --env-file .env.platform up -d postgres` for application persistence. Containers bind database/console ports to 127.0.0.1 only.

Optional observability: `docker compose --env-file .env.platform --profile observability up -d`. This creates a local MinIO bucket. Set Langfuse headless initialization values (owner/project/API keys) in a private override before first startup; open signup is disabled. Local image tags are explicit versions but not verified immutable digests; resolve supported images and pin digests before any AWS deployment. Local startup/image availability have not been tested here because no network pulls are authorized.

Application processes run through the parent project's backend/frontend commands. Set MODEL_PROVIDER=bedrock with explicit model identifier and an existing local AWS profile for real tests; fake remains the offline-test provider. No vLLM GPU container runs on a Mac by default. Docker volumes retain local state; do not use `down -v` unless intentionally discarding it.
