# Coding agent setup

## Sources and discovery

`AGENTS.md` is the shared entrypoint. Canonical skills live in `agent-config/skills/`; canonical roles live in `agent-config/roles/`. This portable location is intentional: the current workspace policy permits reading but blocks writing `.agents/` and `.codex/`. Do not bypass that policy.

`CLAUDE.md` imports shared instructions. `GEMINI.md` directs Antigravity/Gemini-compatible sessions to the shared entrypoint. No tool models, global settings, permission bypasses, plugins or interception hooks are configured.

Generate and inspect native adapters:

```sh
python3 scripts/sync_agent_adapters.py
python3 scripts/sync_agent_adapters.py --check
python3 scripts/check_repository.py
python3 -m unittest discover -s tests -v
```

Generated exports live under `agent-config/adapters/`. In a workspace where the relevant native directories are writable, install them explicitly:

```sh
python3 scripts/sync_agent_adapters.py --install codex
python3 scripts/sync_agent_adapters.py --install claude
python3 scripts/sync_agent_adapters.py --install antigravity
```

These commands write repository-local files only, and preflight all native destinations before writing, refusing to overwrite unmanaged files. They are not permission workarounds. Codex installs roles to `.codex/agents/` and skills to `.agents/skills/`; Claude uses `.claude/agents/` and `.claude/skills/`; Antigravity uses `.agents/agents/` and `.agents/skills/`. After editing a canonical source, regenerate exports and rerun `--install` for each installed tool. The checker detects drift in installed groups as well as exports. Renamed/removed native files require deliberate cleanup; the generator does not delete files. Do not install all exports by simply moving their parent directory.

The metadata format uses YAML frontmatter with JSON-compatible values. Checked-in `.yml` workflows and issue forms use JSON syntax, a YAML-compatible subset, to allow offline standard-library validation. Python 3.11+ is required. The checker validates the subset and our contracts, not every vendor schema or command behavior.

## Using the roles

Start a new session at the repository root after installation. Ask the tool to list available skills/agents and confirm the expected names; do not infer discovery merely because files exist. On versions without compatible discovery, explicitly ask it to read `AGENTS.md` and the relevant canonical skill/role paths.

Example task:

> Read AGENTS.md. Plan and implement the requested change using the implementer role. Once the diff is stable, give a fresh reviewer and test-validator the acceptance criteria and exact revision. Resolve substantiated findings, then validate the final result. Use platform-reviewer only if infrastructure or deployment behavior changes.

For targeted work, invoke the installed skill using the tool's skill UI (Codex `$review-change`, Claude `/review-change`, Antigravity `/review-change` where supported), or provide its canonical path explicitly. Root instructions are the fallback when automatic skill discovery is unavailable.

The coordinator owns scope, acceptance criteria and integration. Each handoff includes base/head revision or a digest plus changed files, requested behavior, available commands and known environmental limitations. Give the reviewer the raw diff and requirements, not a prescribed verdict. A fresh session can provide independent review; two differently named steps in one implementer session cannot.

The reviewer and platform-reviewer report findings without fixing files. Claude adapters allow only Read/Grep/Glob; Antigravity adapters allow view_file/grep_search. The validator also has shell access for verification, so its prohibition on product edits is procedural. Codex custom roles inherit parent permissions: do-not-edit instructions do not establish an independent read-only sandbox. Use parent tool controls or an isolated checkout when stronger enforcement is needed.

Review and validation can run concurrently against a stable result. Concurrent implementation requires explicitly assigned disjoint work and separate worktrees. No recursive delegation by specialist roles. For unavailable subagents, use separate sessions or report independent review as outstanding.

## CI and hosted controls

The `Repository checks` workflow runs offline metadata, adapter, link, syntax, whitespace, limited secret-signature checks and executable regression fixtures. It has read-only GitHub permissions, a pinned checkout action, no persisted checkout credentials and no cloud secrets. It does not run an AI reviewer, cloud validation, or paid model calls. The separate Application checks workflow now runs backend/SDK and frontend behavior checks; its reproducible frontend build gate deliberately fails until a reviewed npm lock exists.

Configure hosted branch protection separately as described in `CONTRIBUTING.md`. There is no Git repository/remote in the initial workspace, so this setup does not create branches, configure GitHub, or activate hosted enforcement. PR/issue templates take effect once pushed to GitHub.

## Compatibility and verification

Initial installed tools: Codex CLI 0.169.0; Claude Code 2.1.266. Antigravity is not on the shell path. Native formats are based on the official references below; capabilities depend on the actual client version.

- Canonical skills, metadata and generated exports: structurally validated by repository checks.
- Claude native adapters: installed repository-locally; live model invocation/discovery is not yet verified.
- Codex native discovery: pending; `.codex/` and `.agents/` writes are blocked by workspace policy. Root AGENTS.md provides instructions in the meantime.
- Antigravity native discovery: pending; protected directory and unavailable CLI. GEMINI.md provides a manual entrypoint.
- Automated fixtures exercise installation in disposable directories; this proves file behavior, not native client discovery.

Do not claim runtime discovery until verified in the relevant client. Follow the client trust/reload process and preserve existing personal configuration.

## Official references

- [Codex repository instructions](https://developers.openai.com/codex/guides/agents-md)
- [Codex custom agents and permission inheritance](https://developers.openai.com/codex/subagents)
- [Claude instruction imports](https://code.claude.com/docs/en/memory)
- [Claude subagent tool restrictions](https://code.claude.com/docs/en/sub-agents)
- [Antigravity skills](https://antigravity.google/docs/skills)
- [Antigravity custom agents](https://antigravity.google/docs/subagents)
