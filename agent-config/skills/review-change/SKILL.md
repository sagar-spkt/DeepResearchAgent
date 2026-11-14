---
name: "review-change"
description: "Independently review a stable diff for correctness, security, missing requirements and regressions; report findings without editing files."
---

# Review a change
Read AGENTS.md, the task acceptance criteria, and the actual diff plus relevant surrounding code. Confirm the revision or file-content digest being reviewed. Do not rely solely on the implementer summary or expected answer. Do not edit files or execute commands that mutate the project, install dependencies, call paid services, or affect external systems.

Trace changed behavior and failure paths. Focus on defects, incorrect assumptions, missing requirements, user isolation, recovery, and tests that fail to protect intended behavior. For platform changes also read agent-config/roles/platform-reviewer.md. Do not invent findings to fill a quota or report subjective style as a correctness defect.

For each finding report severity (critical/high/medium/low), file and line, concrete trigger, observable impact, and supporting evidence. Label uncertain claims and request evidence when needed. Return findings first, then inspected scope, revision/digest, and remaining risks. If none, say no findings and state limits; this is not proof of correctness. Return findings to the implementer; do not silently fix them.
