---
name: "validate-change"
description: "Validate a stable change with relevant checks and reproducible evidence, including failures and unavailable checks."
---

# Validate a change
Read AGENTS.md and acceptance criteria. Identify the revision/digest and available check commands from the repository. Choose checks based on behavior and risk. Repository checks are python3 scripts/check_repository.py and python3 -m unittest discover -s tests -v. Read docs/local-development.md for the current application commands (Ruff, mypy, pytest, Node tests and offline evals) and known unavailable live checks.

Run checks without modifying product code. Disposable caches/build/test artifacts are allowed; use temporary directories for destructive fixtures. Default to fake providers and synthetic data. Do not update snapshots or rewrite expected results to make tests pass. Do not invoke cloud operations, real model/search APIs, or install packages without existing authorization.

Record each command, exit code, scope, and result as passed, failed, or unavailable. For failures, isolate a minimal reproduction and report it to the implementer; do not fix product code. Retest affected behavior after fixes. Check the revision/digest did not change during validation. Explain why skipped checks matter; never report a skip as a pass.
