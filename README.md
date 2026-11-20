# Deep Research Agent

A multi-user deep research app built on the open-source Deep Agents SDK. It
researches the web (Tavily) and your uploaded documents, writes cited reports,
and keeps durable conversations, private documents and explicit saved memory.

- **Stack:** FastAPI API + worker, Next.js web, PostgreSQL/pgvector, BGE CPU
  embeddings, Amazon Bedrock, self-hosted Langfuse, Cognito sign-in.
- **Local:** Docker Compose; see [local development](docs/local-development.md).
- **AWS:** staging and production are created, updated and destroyed only by the
  GitHub Actions *Deploy* and *Destroy* workflows; see
  [AWS environments](infra/README.md).

More: [project specification](docs/project-specification.md) ·
[contributing](CONTRIBUTING.md) · [coding agent setup](docs/agent-setup.md) ·
[decisions](docs/decisions/).

## License

Apache License 2.0. See [LICENSE](LICENSE).
