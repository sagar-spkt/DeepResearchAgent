# ADR-0008: Use a manual owner-started production release gate

- Status: accepted
- Date: 2026-11-16

## Context

The project is operated by one person who does not want to add another GitHub account. Codex can review code, but GitHub does not accept it as a required environment reviewer. An independent reviewer is therefore unavailable for the current solo setup.

## Decision

Use required CI checks and protected-branch rules for changes, then use a manually started production workflow as the owner's explicit release authorization. The production path must require an exact confirmation phrase, target the protected `production` environment, and record the initiating GitHub identity, source commit, inputs, and outcome. Keep Codex review advisory. Do not claim this is an independent review.

The manual path does not authorize production now. Enable AWS OIDC or promotion only after staging acceptance, backup/restore and shutdown evidence, immutable artifact provenance, least-privilege role policies, and rollback checks have passed independent platform review and the owner authorizes the exact production release.

## Consequences

- No second account or reviewer is needed for this portfolio project's release workflow.
- CI, branch protection, exact artifact identity, environment restrictions, and audit records provide repeatable release controls; the operator remains the sole authorizer.
- A typed workflow input prevents accidental dispatch but is not cryptographic approval or protection from a compromised owner account.
- The current `Promote` workflow only checks for the confirmation phrase and then invokes a fail-closed lifecycle command. It does not contact AWS or promote artifacts.
