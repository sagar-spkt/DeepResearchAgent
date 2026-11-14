"""Explicit vLLM benchmark; never launched by ordinary CI."""

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requests", type=int, default=4)
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    if not (1 <= args.requests <= 100 and 1 <= args.concurrency <= 8):
        parser.error("Use 1-100 requests and 1-8 concurrent clients")
    endpoint = os.environ.get("VLLM_BASE_URL", "").rstrip("/")
    model = os.environ.get("VLLM_MODEL", "")
    key = os.environ.get("VLLM_API_KEY", "")
    if not all((endpoint, model, key)):
        parser.error("Explicit VLLM_BASE_URL, VLLM_MODEL and VLLM_API_KEY required")

    def request(index):
        started = time.perf_counter()
        first = None
        usage = {}
        with httpx.stream(
            "POST",
            endpoint + "/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": "Explain continuous batching and prefix caching in 150 words.",
                    }
                ],
                "max_tokens": 256,
                "temperature": 0,
                "stream": True,
                "stream_options": {"include_usage": True},
                "cache_salt": "benchmark-synthetic-only",
            },
            timeout=120,
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    data = json.loads(line[6:])
                    if any(
                        choice.get("delta", {}).get("content") for choice in data.get("choices", [])
                    ):
                        first = first or time.perf_counter()
                    usage = data.get("usage") or usage
        elapsed = time.perf_counter() - started
        return {
            "index": index,
            "seconds": elapsed,
            "ttft_seconds": first - started if first else None,
            "usage": usage,
            "output_tokens_per_second": usage.get("completion_tokens", 0) / elapsed,
        }

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        rows = list(pool.map(request, range(args.requests)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "model": model,
                "concurrency": args.concurrency,
                "samples": rows,
                "quality_verified": False,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Wrote {len(rows)} measured inference samples")


if __name__ == "__main__":
    main()
