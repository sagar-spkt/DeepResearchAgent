# ADR-0009: Bootstrap recovery evidence in a closed staging rehearsal

- Status: accepted within existing staging authorization
- Date: 2026-11-16

## Context

Live recovery evidence requires AWS runtime resources. Earlier bootstrap instructions required a successful live recovery rehearsal before any runtime provisioning, which made the first rehearsal impossible. Offline tests demonstrate failure handling but cannot establish that AWS snapshots, ClickHouse backups, and restored records actually work.

## Decision

Use the first staging session as a recovery rehearsal with synthetic data and application ingress disabled. Before creating runtime resources, require a reviewed exact plan, current cost/capacity checks, immutable artifacts, a global session admission/lease, tested backup/restore implementation, and an independently reviewed provisioning-failure cleanup path. Install AWS-side expiry before starting application writers or admitting users. The operator must be able to clean up partially created resources even when no application database or backup exists yet; this path may remove only inventoried resources created by this rehearsal and must never purge retained data.

After provisioning, verify expiry authorization, alert delivery, and backup prerequisites. Create synthetic accounts, conversations, documents, and observability data through private access. Back up, verify the durable receipt, restore into the scoped environment, and compare the restored records and retrieval behavior. Keep ingress closed until the rehearsal succeeds. Record backup/restore timings and teardown inventory, including retained billed resources.

If backup or restore fails, stop acceptance, preserve existing retained data, and repair the failure. Failed recovery does not authorize deletion of data to fit the four-hour session. Alert the owner if safe cleanup cannot complete; estimates and expiry are not billing caps. A first empty environment may use the separately reviewed provisioning-failure cleanup path only before it contains data requiring preservation.

## Consequences

- Initial provisioning can establish live recovery evidence without claiming offline tests are equivalent.
- Normal public staging acceptance and production remain blocked on successful recovery evidence.
- This decision grants no new spend, production, or purge authorization. It uses the owner's conditional staging authorization within the existing session limits.
- Implementations for deployment, provisioning-failure cleanup, restore, and expiry still require independent review; this ADR alone enables none of them.
