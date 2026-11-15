"""Build the production dependency environment from the checked-in uv lock."""

import os
import subprocess
import sys

ALLOWED_EXTRAS = {"bedrock", "embeddings", "local-postgres"}


def command(extras_value):
    extras = [extra for extra in extras_value.split(",") if extra]
    if len(extras) != len(set(extras)) or set(extras) - ALLOWED_EXTRAS:
        raise ValueError("INSTALL_EXTRAS contains an unsupported or duplicate extra")
    return [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--group",
        "build",
        *[arg for item in extras for arg in ("--extra", item)],
    ]


def runtime_command(extras_value):
    extras = [extra for extra in extras_value.split(",") if extra]
    if len(extras) != len(set(extras)) or set(extras) - ALLOWED_EXTRAS:
        raise ValueError("INSTALL_EXTRAS contains an unsupported or duplicate extra")
    return [
        "uv",
        "sync",
        "--locked",
        "--no-dev",
        "--no-editable",
        *[arg for item in extras for arg in ("--extra", item)],
    ]


def main():
    try:
        extras = os.environ.get("INSTALL_EXTRAS", "")
        subprocess.run(command(extras), check=True)
        subprocess.run(runtime_command(extras), check=True)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
