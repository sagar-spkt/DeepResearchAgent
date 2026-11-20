# ADR-0004: Refresh container OS security packages at build time

- Status: Accepted for the local portfolio baseline
- Date: 2026-11-15
- Related requirement or issue: PLT-02 in the project history (the remaining-work plan was retired by ADR-0012)

## Context

Local Trivy scans of the pinned Bookworm application images reported Critical Debian findings and JavaScript findings from npm retained in the web runtime. Moving to the official multi-platform Trixie base manifests and removing npm from the standalone web runtime eliminated those Critical findings and runtime npm findings. The first Trixie scan then identified two High zlib findings with fixes available in the configured Debian security repository. The fixed zlib package was available from `trixie-security` when checked on 2026-11-14.

## Decision and alternatives

Keep the official multi-platform base manifests pinned by digest, install current Debian security package updates in the final runtime stages during image builds, and omit npm/npx from the web runtime. Keep the Python dependency lock and CPU-only embedding wheel selection unchanged except for the separate PyJWT 2.15.1 security update.

The alternative was to rely only on a newer base-image digest. That would retain vulnerable package versions until the upstream image was refreshed. Pinning each Debian package version to the current security repository was rejected because repository package revisions change and exact versions can disappear. A dated Debian snapshot would improve bit-for-bit reproducibility but would delay security fixes unless the snapshot date were regularly advanced.

## Consequences and validation

Base image identity remains pinned, but final OS package layers are time-dependent on the Debian repositories at build time. The exact local image digest and scan report must therefore be retained with each release; rebuilds can produce different runtime image digests. A future release pipeline should run vulnerability and license scans against the exact images it publishes and record their SBOM/provenance. Do not treat the current local scan as release approval.

On 2026-11-14, backend, web, and amd64 CPU embeddings images built locally. The backend imported as non-root; BGE produced a finite, normalized 384-dimensional vector with networking disabled. Trivy 0.76.1 reported zero Critical and zero fixable High findings in the updated web and embeddings images. It still reported 46 and 103 High Debian OS-package findings respectively, with no fix versions in those reports. See the local scan summary and remaining gates in the project history (the remaining-work plan was retired by ADR-0012) and license observations in `docs/dependency-license-inventory.md`. No image was published to ECR and no AWS deployment was performed.
