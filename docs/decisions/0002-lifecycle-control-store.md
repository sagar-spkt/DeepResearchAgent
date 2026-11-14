# ADR-0002: Retained global session admission and lifecycle lease

- Status: accepted for implementation; live behavior unverified
- Date: 2026-11-13

## Context

Staging and production share one four-vCPU GPU quota and a monthly runtime budget. Separate environment databases disappear on shutdown. GitHub workflow concurrency cannot coordinate manual CLI operations and the AWS expiry runner. An expired timer alone is not evidence that the previous resources were removed.

## Decision

Use a small shared DynamoDB on-demand table in a separate retained `infra/control` state. Conditional transactional writes admit one global session, reserve time against UTC billing-month buckets, and provide a renewable lifecycle lease. The CLI must prove admission and hold the lease before backup, shutdown or expiry mutations. Do not automatically clear an expired active session: an explicit reviewed release collects fresh provider inventory and atomically archives completion before clearing the matching admission. The transaction also checks lease ownership/expiry; it never refunds reserved time. Previously completed environment/session IDs cannot be admitted again.

Reserve six hours per default four-hour demonstration, including a conservative two-hour cleanup allowance. Limit reservations to twenty hours per UTC month. Do not refund unused reservations in v1. This deliberately admits fewer sessions than a refund-based ledger and does not establish a hard billing cap if cleanup fails.

Lease ownership is checked before mutating calls and Terraform apply. All deployment/expiry operators must honor the common lease; AWS administrators can still bypass application controls. Live IAM enforcement, expiry recovery, restore rehearsal and live teardown inventory acceptance remain release gates. This change adds source only, not a deployed table.

## Validation

Use fake-client tests for competing admission, cross-month accounting, budget exhaustion, expired/lost leases and fail-closed cleanup. Provider schema validation and a real concurrent DynamoDB test are required before paid deployment.
