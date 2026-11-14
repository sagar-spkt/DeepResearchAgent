# Deep Research Agent

A portfolio application for self-hosted deep research with durable conversations, private documents, explicit memory and reproducible AWS deployment source.

**Current state:** local API, worker, web UI, PostgreSQL/pgvector, pinned BGE CPU embeddings and migrations are running. A bounded Bedrock + Tavily research request completed with source links, a browser upload was indexed and cited in a document-grounded answer, and the API/worker recovered conversation and checkpoint state after restart. The frontend production build succeeds. Broad research quality, local Langfuse, Cognito flows and AWS deployment/restore remain unverified; release remains blocked on the items in the roadmap.

- [Implementation roadmap and evidence](docs/implementation-roadmap.md)
- [Local setup and verification](docs/local-development.md)
- [Project specification](docs/project-specification.md)
- [Contributor workflow](CONTRIBUTING.md)
- [Coding agent setup](docs/agent-setup.md)
- [AWS deployment prerequisites](infra/BOOTSTRAP.md)

Start with the local development guide. No AWS resources are created by ordinary tests or by reading this repository.
