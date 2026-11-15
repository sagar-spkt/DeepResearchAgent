# Contributing

Start with `AGENTS.md` and the project specification. Use a short-lived descriptive branch and a focused PR once Git is initialized. Do not initialize a remote, publish, or change hosted repository settings as part of an ordinary code task.

## Definition of done

- Acceptance criteria are met, with behavior and failure modes demonstrated.
- Relevant automated checks pass; missing tools, credentials, or infrastructure are recorded as unavailable, not passing.
- A fresh reviewer inspected the actual final diff; substantive findings are resolved or explicitly recorded as blockers. No silent waiver of correctness/security findings.
- A validator reports commands, outcomes, and the revision or content digest tested. Changes after review require review of the affected delta.
- API/schema changes include compatibility and migration considerations. Deployment changes include rollback and cost/data implications. Documentation reflects actual behavior.

For tiny changes, keep the process brief. No blanket coverage target or mandatory test-first ritual; use regression tests when they protect meaningful behavior.

## Checks available now

```sh
python3 scripts/check_repository.py
python3 -m unittest discover -s tests -v
```

The checker validates repository-specific metadata contracts, generated adapter consistency, local Markdown file links, TOML/JSON syntax, whitespace, and a limited set of obvious secret signatures. It is not a full vendor schema validator or comprehensive secret scanner.

## Subsystem check status

| Subsystem | Current checks and remaining release requirements |
| --- | --- |
| Python API/workers | Ruff, mypy and pytest implemented; ownership, retry, cancellation and failure tests run. Cross-platform `uv.lock` includes runtime, dev, and project build dependencies; CI installs with `uv sync --locked --group dev --group build`. |
| Next.js UI | Node behavior/module checks and npm lock are present; production build succeeds. Selected Chrome flows for local session, history, upload, report, attachment, memory CRUD, admin trace view, local non-admin denial, and native Langfuse tracing were exercised. Active-run reconnect, cancellation, upload-during-run, Cognito and admin-revocation acceptance remain outstanding. Current source is JavaScript. |
| Database/documents | SQLite regressions and local PostgreSQL/pgvector migration, checkpoint recovery, upload indexing, and real CPU BGE inference were exercised. Broader concurrency/capacity and production RDS acceptance remain outstanding. |
| Deep Agents/models | Real SDK offline execution, 100-turn compaction and 20 synthetic cases run. Live Bedrock/vLLM evaluations remain outstanding. |
| Terraform/Helm | Cross-platform provider locks are present and all four Terraform roots validate with backend disabled; Helm lint/render and Compose checks run with synthetic release values. Intended EKS/controller compatibility, real artifact digests and cloud integration remain outstanding. |
| Deployment/lifecycle | Preparation workflows remain deployment-blocked. Require live expiry, staging smoke, backup/restore and teardown inventory evidence before promotion. |

See docs/local-development.md for executable commands. Update this matrix, CI and agent instructions when checks are added; never call outstanding checks passing. Default CI must not need cloud credentials or paid APIs. Fork PRs never receive secrets.

## Hosted GitHub settings

After creating a GitHub repository, configure protected `main`: require PRs, at least one human approval, resolved review conversations, and the `Repository checks` status; prohibit force pushes and deletion. Where the account/plan supports it, require approval of the latest push and prevent bypass. These are administrator actions, not enforced by these files.

Protect the production GitHub environment and restrict deployment identities through OIDC. CI passing and AI review do not authorize deployment or replace a human release decision. Preparation workflows exist, but are not functioning release pipelines; no AWS credentials are stored. Also require Backend behavior, Frontend behavior, Frontend reproducible build, and Container scans and SBOMs statuses once CI is activated.
