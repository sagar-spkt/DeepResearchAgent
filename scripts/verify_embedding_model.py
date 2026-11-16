"""Verify the pinned BGE model artifact before using or shipping it."""

from __future__ import annotations

import argparse
import hashlib
import hmac
from pathlib import Path

MODEL_ID = "BAAI/bge-small-en-v1.5"
MODEL_REVISION = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
MODEL_SHA256 = "3c9f31665447c8911517620762200d2245a2518d6e7208acc78cd9db317e21ad"


def verify_model_artifacts(model_dir: Path, revision: str) -> str:
    """Require the expected model revision, weights digest, and model card."""
    if revision != MODEL_REVISION:
        raise ValueError(f"Unexpected {MODEL_ID} revision: {revision}")

    readme = model_dir / "README.md"
    if not readme.is_file() or readme.stat().st_size == 0:
        raise FileNotFoundError(f"Missing or empty model card: {readme}")

    weights = model_dir / "model.safetensors"
    if not weights.is_file():
        raise FileNotFoundError(f"Missing model weights: {weights}")

    digest = hashlib.sha256()
    with weights.open("rb") as artifact:
        for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
            digest.update(chunk)
    actual = digest.hexdigest()
    if not hmac.compare_digest(actual, MODEL_SHA256):
        raise ValueError(f"Unexpected model weights SHA-256: {actual}")
    return actual


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    digest = verify_model_artifacts(args.model_dir, args.revision)
    print(f"Verified {MODEL_ID}@{args.revision}; model.safetensors SHA-256 {digest}")


if __name__ == "__main__":
    main()
