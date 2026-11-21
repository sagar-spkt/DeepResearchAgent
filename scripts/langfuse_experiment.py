"""Langfuse datasets and experiments for the deployed research agent.

    python3 scripts/langfuse_experiment.py dataset --environment staging
    python3 scripts/langfuse_experiment.py run --environment staging [--run-name NAME]

``dataset`` upserts evals/cases.json as the Langfuse dataset named in evals/VERSION
(one item per case: input = prompt + attached document, expected output = rubric).

``run`` is one experiment: it reads the dataset's items from Langfuse, runs each
through the deployed agent inside the worker pod (scripts/eval_case_in_pod.py; same
image, model, tools and limits as production, throwaway database), and links each
resulting trace to the item as a dataset run. Langfuse then shows the run under
Datasets -> <dataset> -> Runs, and LLM-as-a-judge evaluators configured for the
dataset score it inside Langfuse (see docs/evaluations.md).

Needs kubectl access to the cluster (users in ``admin_principal_arns``). The agent's
Bedrock and Tavily usage is billed; ``--max-total-usd`` stops starting new items once
the traced agent cost reaches it. Standard library only.
"""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = "research"
LOCAL_PORT = 13000


class Langfuse:
    def __init__(self, base_url, public_key, secret_key):
        self.base = base_url.rstrip("/") + "/api/public/"
        token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        self.headers = {"Authorization": "Basic " + token, "Content-Type": "application/json"}

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=self.headers)
        with urllib.request.urlopen(req, timeout=30) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def dataset_items(self, dataset):
        items, page = [], 1
        while True:
            query = urllib.parse.urlencode({"datasetName": dataset, "limit": 100, "page": page})
            result = self.request("GET", f"dataset-items?{query}")
            items += [item for item in result["data"] if item.get("status", "ACTIVE") == "ACTIVE"]
            if page >= result["meta"]["totalPages"]:
                return items
            page += 1

    def trace_cost(self, trace_id, wait_seconds=120):
        """Total traced cost once ingestion has the final report; None if not yet available."""
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            try:
                trace = self.request("GET", f"traces/{trace_id}")
                if trace.get("output") is not None and trace.get("totalCost") is not None:
                    return float(trace["totalCost"])
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
            time.sleep(5)
        return None


def kubectl(*args):
    return subprocess.run(["kubectl", "-n", NAMESPACE, *args], check=True, text=True, capture_output=True).stdout


def connect(environment):
    subprocess.run(
        ["aws", "eks", "update-kubeconfig", "--name", f"deepresearch-{environment}"], check=True, capture_output=True
    )
    keys = {
        key: base64.b64decode(kubectl("get", "secret", "research-application", "-o", f"jsonpath={{.data.{key}}}")).decode()
        for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    }
    forward = subprocess.Popen(
        ["kubectl", "-n", NAMESPACE, "port-forward", "svc/langfuse", f"{LOCAL_PORT}:3000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{LOCAL_PORT}"
    for _ in range(30):
        try:
            urllib.request.urlopen(url + "/api/public/health", timeout=2)
            return forward, Langfuse(url, keys["LANGFUSE_PUBLIC_KEY"], keys["LANGFUSE_SECRET_KEY"])
        except OSError:
            time.sleep(1)
    forward.terminate()
    raise SystemExit("Could not reach Langfuse through kubectl port-forward")


def sync_dataset(langfuse, dataset, cases):
    try:
        langfuse.request(
            "POST",
            "datasets",
            {
                "name": dataset,
                "description": "Synthetic research cases (evals/cases.json). Input: prompt and attached "
                "document; expected output: rubric. See docs/evaluations.md.",
                "metadata": {"source": "evals/cases.json"},
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 409:  # an existing dataset keeps its settings; items are upserted below
            raise
    for case in cases:
        langfuse.request(
            "POST",
            "dataset-items",
            {
                "id": f"{dataset}:{case['id']}",
                "datasetName": dataset,
                "input": {
                    "prompt": case["prompt"],
                    "document": case["document"],
                    "web_search": case.get("web_search_mode", "enabled"),
                },
                "expectedOutput": {
                    "expected_behaviors": case["expected_behaviors"],
                    "disallowed_claims": case["disallowed_claims"],
                    "review_criteria": case["human_review"],
                    "must_cite_attached_document": case["document_required"],
                },
                "metadata": {"case_id": case["id"], "topic": case["topic"]},
            },
        )
    print(f"Dataset {dataset}: {len(cases)} items upserted")


def run_in_pod(item, run_id, session_id, environment):
    case = {
        "id": item["metadata"]["case_id"],
        "topic": item["metadata"].get("topic", ""),
        "prompt": item["input"]["prompt"],
        "document": item["input"]["document"],
        "web_search_mode": item["input"].get("web_search", "enabled"),
    }
    payload = base64.b64encode(
        json.dumps({"case": case, "run_id": run_id, "session_id": session_id, "environment": environment}).encode()
    ).decode()
    completed = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "exec", "-i", "deploy/research-worker", "--", "python", "-", payload],
        input=(ROOT / "scripts" / "eval_case_in_pod.py").read_text(),
        text=True,
        capture_output=True,
        timeout=1500,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("EVAL_RESULT "):
            return json.loads(line[len("EVAL_RESULT "):])
    return {"case_id": case["id"], "status": "failed", "error": f"pod exit {completed.returncode}: {completed.stderr.strip()[-300:]}"}


def run_experiment(langfuse, args, dataset):
    model = kubectl(
        "get", "deploy/research-worker", "-o",
        "jsonpath={.spec.template.spec.containers[0].env[?(@.name=='BEDROCK_MODEL_ID')].value}",
    ).strip()
    release = kubectl("get", "deploy/research-worker", "-o", "jsonpath={.spec.template.spec.containers[0].image}")
    run_name = args.run_name or f"{model.split(':')[0]}-{datetime.now(timezone.utc):%Y%m%d-%H%M}"
    items = langfuse.dataset_items(dataset)
    if args.only:
        items = [item for item in items if item["metadata"].get("case_id") in set(args.only)]
    output = ROOT / "local-data" / "evals" / f"{run_name}.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    results, spent, lock = {}, 0.0, Lock()
    print(f"Experiment {run_name}: {len(items)} items, agent {model}, budget ${args.max_total_usd:.2f}")

    def evaluate(item):
        nonlocal spent
        with lock:
            if spent >= args.max_total_usd:
                return item, {"status": "skipped", "error": "budget reached"}
        run_id = str(uuid.uuid4())
        result = run_in_pod(item, run_id, run_name, args.environment)
        langfuse.request(
            "POST",
            "dataset-run-items",
            {
                "runName": run_name,
                "runDescription": f"Agent {model} on {args.environment}",
                "metadata": {"agent_model": model, "environment": args.environment, "image": release},
                "datasetItemId": item["id"],
                "traceId": run_id,
            },
        )
        result["trace_id"] = run_id
        result["agent_cost_usd"] = langfuse.trace_cost(run_id)
        with lock:
            spent += result["agent_cost_usd"] or 0.0
        return item, result

    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        for item, result in pool.map(evaluate, items):
            case_id = item["metadata"]["case_id"]
            results[case_id] = result
            output.write_text(json.dumps({"run_name": run_name, "dataset": dataset, "agent_model": model, "results": results}, indent=2, ensure_ascii=False) + "\n")
            output.chmod(0o600)
            cost = result.get("agent_cost_usd")
            print(
                f"{case_id}: {result['status']} {result.get('latency_seconds', '-')}s "
                f"{'$%.4f' % cost if cost is not None else 'cost pending'} {result.get('error', '')}",
                flush=True,
            )
    done = sum(1 for r in results.values() if r["status"] == "completed")
    print(f"{done}/{len(results)} completed; traced agent cost ${spent:.4f}; local copy {output.relative_to(ROOT)}")
    print(f"Langfuse: Datasets -> {dataset} -> Runs -> {run_name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["dataset", "run"])
    parser.add_argument("--environment", choices=["staging", "production"], required=True)
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "cases.json")
    parser.add_argument("--run-name", help="Experiment (dataset run) name; default <model>-<UTC time>")
    parser.add_argument("--only", nargs="*", help="Case ids to run (default: every active item)")
    parser.add_argument("--parallel", type=int, default=2, help="Items run at once in the worker pod")
    parser.add_argument("--max-total-usd", type=float, default=10.0)
    args = parser.parse_args()
    dataset = (args.cases.parent / "VERSION").read_text().strip()
    forward, langfuse = connect(args.environment)
    try:
        if args.command == "dataset":
            sync_dataset(langfuse, dataset, json.loads(args.cases.read_text()))
        else:
            run_experiment(langfuse, args, dataset)
    finally:
        forward.terminate()
    return 0


if __name__ == "__main__":
    sys.exit(main())
