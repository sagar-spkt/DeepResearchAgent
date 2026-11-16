# AWS lifecycle workflows

The checked-in lifecycle workflows are manual, preparation-only gates. They do not provision, back up, restore, shut down, promote, or purge AWS resources. Each currently fails closed in `scripts/platform_lifecycle.py` before creating AWS clients. The staging and production OIDC roles trust this repository but have no AWS permissions; workflows request no OIDC token.

## Solo production release authorization

This portfolio project uses CI plus a deliberate owner-started workflow instead of a second GitHub account or independent reviewer. The `Promote` workflow targets the GitHub `production` environment and, when production is selected, requires the exact input `I AUTHORIZE PRODUCTION PROMOTION`. Its run records the workflow actor, commit, and outcome; the job log records when the production confirmation matched. This is an auditable self-authorization step, not an independent code review or a substitute for the required CI checks.

The confirmation input is only one gate. Production promotion remains disabled until staging acceptance, backup/restore and shutdown evidence, artifact provenance, least-privilege OIDC policies, rollback checks, and a task-specific release decision are complete. Do not run the promote workflow now: it is still a fail-closed placeholder. When the workflow is ready to operate, the owner will start it from **Actions → Promote → Run workflow**, select `production`, enter the exact confirmation phrase, and inspect the run's commit and deployment summary before it proceeds.

Staging and production environments are restricted to protected branches, and the `main` ruleset requires the configured CI checks. No environment reviewer is configured. Never use `pull_request_target` to execute untrusted code or expose cloud credentials to fork pull requests.

All AWS lifecycle mutations must share the global concurrency group and an AWS-side lease that also excludes console or external changes. Build images once, publish immutable digests with provenance, and promote the same tested digests. Validate release manifests; schema checks are not provenance verification. Infrastructure plans can contain secrets and must not be public artifacts or logs.

Implement AWS-side four-hour expiry before permitting deployment, including backup, drain, failure notification, and billed-resource inventory. Read `infra/README.md` for retained/runtime ownership and restoration blockers. Purge requires its own explicit confirmation, verified backup, exact environment inventory, and ordered state disposition; never interpolate an environment input into a shell command.
