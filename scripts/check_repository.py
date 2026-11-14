"""Offline repository contract checks, not comprehensive vendor schema validation."""

import hashlib
import json
import re
import sys
import tomllib
from urllib.parse import unquote

from sync_agent_adapters import ROOT, read_metadata, sync

EXCLUDED = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".terraform",
    "local-data",
    ".agent-artifacts",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".next",
}


def check(root=ROOT):
    errors = []
    try:
        sync(root, check=True)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
    paths = sorted(
        p
        for p in root.rglob("*")
        if p.is_file() and not EXCLUDED.intersection(p.relative_to(root).parts)
    )
    digest = hashlib.sha256()
    for path in paths:
        relative = path.relative_to(root).as_posix()
        if path.suffix not in {
            ".md",
            ".py",
            ".yml",
            ".yaml",
            ".json",
            ".toml",
        } and path.name not in {".gitignore", ".editorconfig"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
            digest.update(relative.encode() + b"\0" + content.encode() + b"\0")
            if not content.endswith("\n") or any(
                line.rstrip() != line for line in content.splitlines()
            ):
                errors.append(f"{relative}: missing final newline or trailing whitespace")
            if path.suffix == ".toml":
                data = tomllib.loads(content)
                if "/codex/agents/" in f"/{relative}" and set(data) != {
                    "name",
                    "description",
                    "developer_instructions",
                }:
                    errors.append(f"{relative}: unsupported Codex adapter fields")
            if path.suffix in {".json", ".yml", ".yaml"}:
                if relative.startswith("helm/") and "/templates/" in relative:
                    # Helm's Go templates are validated by helm lint/template, not JSON parsing.
                    continue
                # Checked-in YAML intentionally uses its JSON-compatible subset.
                data = json.loads(content)
                if "workflows/" in relative:
                    if data.get("permissions") != {"contents": "read"}:
                        errors.append(f"{relative}: CI must have read-only permissions")
                    if not set(data.get("on", {})) <= {"push", "pull_request", "workflow_dispatch"}:
                        errors.append(f"{relative}: unsupported CI event")
                    for job in data.get("jobs", {}).values():
                        for step in job.get("steps", []):
                            if "uses" in step and not re.fullmatch(
                                r"[^@]+@[0-9a-f]{40}", step["uses"]
                            ):
                                errors.append(f"{relative}: action is not SHA-pinned")
            if path.suffix == ".md":
                if content.startswith("---\n"):
                    read_metadata(path)
                for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", content):
                    if re.match(r"[a-zA-Z][\w+.-]*:", target) or target.startswith("#"):
                        continue
                    target = unquote(target.split("#", 1)[0].strip("<>"))
                    if target and not (path.parent / target).exists():
                        errors.append(f"{relative}: broken local link: {target}")
            if re.search(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", content):
                errors.append(f"{relative}: private key signature")
            if re.search(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", content):
                errors.append(f"{relative}: AWS access-key signature")
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{relative}: {exc}")
    return errors, digest.hexdigest()


def main():
    errors, digest = check()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print(f"Repository checks passed; inspected content SHA256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
