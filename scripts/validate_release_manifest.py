"""Validate immutable image promotion inputs without executing shell text."""

import argparse
import json
import re
import sys
from pathlib import Path


def validate(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("manifest must be an object")
    for key in ("backend_image", "web_image"):
        if not re.fullmatch(r"[a-z0-9./:_-]+@sha256:[a-f0-9]{64}", manifest.get(key, "")):
            raise ValueError(f"{key} must be an immutable image digest")
    if not re.fullmatch(r"[a-f0-9]{40}", manifest.get("source_commit", "")):
        raise ValueError("source_commit must be a full commit SHA")
    if manifest.get("staging_validated") is not True:
        raise ValueError("staging validation evidence is required")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        validate(json.loads(args.manifest.read_text()))
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        "Release manifest format valid; validate provenance and live staging evidence separately."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
