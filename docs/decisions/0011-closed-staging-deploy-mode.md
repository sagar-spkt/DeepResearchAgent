# ADR-0011: Separate closed deployment readiness from recovery acceptance

## Status

Accepted for the staging workflow implementation; no cloud deployment authorized by this ADR.

## Context

The staging deploy workflow previously had one path gated on recovery acceptance. That made a closed first deployment impossible while recovery acceptance was still pending. Treating first deployment as already accepted would weaken the release gate. A first deployment also must not create public ingress or start a GPU-backed model service before the closed rehearsal is complete.

## Decision

Add an explicit manual `closed-rehearsal` mode. It requires a distinct confirmation phrase, a dispatch on `main`, the existing in-VPC runner readiness, and explicit environment readiness flags for admission, installed expiry controls, immutable expiry artifacts/permissions, and first-provision cleanup. It deploys the existing staging chart with ingress and vLLM disabled, makes no DNS alias, and reports that the deployment is not restore acceptance. The public deployment path retains its prior recovery and network acceptance gates. No fake model provider is configured.

## Consequences

The closed workflow can be used after its prerequisite controls are actually installed and verified, without asserting recovery acceptance in advance. It does not provision the expiry stack or prove recovery. Current lifecycle backup requires application and observability writer workloads that do not exist in an empty namespace, and current restore only prepares snapshot inputs. Database and ClickHouse/Valkey restoration with synthetic preservation comparisons remain required before `STAGING_RECOVERY_ACCEPTED` may be set. See [closed staging deployment path](../staging-closed-rehearsal.md).
