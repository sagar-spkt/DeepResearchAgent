"""Register model prices in Langfuse so traces show dollar costs.

Langfuse's built-in price table has no Amazon Nova models, so generations from
Bedrock Nova models would otherwise cost $0. This script makes the project's
custom model definitions match MODELS: it creates missing ones and replaces
ones whose prices changed. Run it where Langfuse and its project keys are
reachable (the deploy workflow runs it inside the API pod):

    LANGFUSE_BASE_URL=... LANGFUSE_PUBLIC_KEY=... LANGFUSE_SECRET_KEY=... \
        python scripts/langfuse_register_models.py

Langfuse only prices generations ingested after a model is registered.
Standard library only.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.request

# AWS Price List API, AmazonBedrock, us-east-1, Standard on-demand.
# Nova Pro (cloud default), queried 2026-11-20: $0.0008 / 1K input, $0.0032 / 1K output.
# Nova Lite (local development), queried 2026-11-15: $0.00006 / 1K input, $0.00024 / 1K output.
MODELS = [
    {
        "modelName": "amazon.nova-pro-v1:0",
        "matchPattern": r"(?i)^(us\.)?(amazon\.)?nova-pro-v1(:0)?$",
        "unit": "TOKENS",
        "inputPrice": 0.80 / 1_000_000,
        "outputPrice": 3.20 / 1_000_000,
    },
    {
        "modelName": "amazon.nova-lite-v1:0",
        "matchPattern": r"(?i)^(us\.)?(amazon\.)?nova-lite-v1(:0)?$",
        "unit": "TOKENS",
        "inputPrice": 0.06 / 1_000_000,
        "outputPrice": 0.24 / 1_000_000,
    },
]


def plan(existing: list[dict], wanted: list[dict]) -> tuple[list[str], list[dict]]:
    """Return (custom model ids to delete, model definitions to create)."""
    delete, create = [], []
    for model in wanted:
        matches = [
            item
            for item in existing
            if item.get("modelName") == model["modelName"] and not item.get("isLangfuseManaged")
        ]
        current = [
            item
            for item in matches
            if item.get("matchPattern") == model["matchPattern"]
            and item.get("unit") == model["unit"]
            and _same(item.get("inputPrice"), model["inputPrice"])
            and _same(item.get("outputPrice"), model["outputPrice"])
        ]
        keep = current[0]["id"] if current else None
        delete += [item["id"] for item in matches if item["id"] != keep]
        if keep is None:
            create.append(model)
    return delete, create


def _same(a, b) -> bool:
    return a is not None and abs(float(a) - float(b)) < 1e-15


class Client:
    def __init__(self, base_url: str, public_key: str, secret_key: str):
        self.base = base_url.rstrip("/") + "/api/public/"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "Content-Type": "application/json"}

    def request(self, method: str, path: str, body: dict | None = None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def models(self) -> list[dict]:
        items, page = [], 1
        while True:
            result = self.request("GET", f"models?limit=100&page={page}")
            items += result["data"]
            if page >= result["meta"]["totalPages"]:
                return items
            page += 1


def main() -> int:
    try:
        client = Client(
            os.environ.get("LANGFUSE_BASE_URL", "http://langfuse:3000"),
            os.environ["LANGFUSE_PUBLIC_KEY"],
            os.environ["LANGFUSE_SECRET_KEY"],
        )
    except KeyError as exc:
        print(f"Missing setting: {exc.args[0]}", file=sys.stderr)
        return 2
    delete, create = plan(client.models(), MODELS)
    for model_id in delete:
        client.request("DELETE", f"models/{model_id}")
    for model in create:
        client.request("POST", "models", model)
    unchanged = len(MODELS) - len(create)
    print(f"Langfuse model prices: {len(create)} created, {len(delete)} replaced/removed, {unchanged} unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
