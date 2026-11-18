"""Entry point used by the GitHub Actions workflow.

Reads ``eval/run_request.json`` and does one of three things:

* ``list_models``  – saves the current OpenRouter catalogue for the three labs
                     (IDs, prices, context, parameters) to eval/results/models.json
* ``dry_run``      – runs the full pipeline against Inspect's mock model (no cost)
* ``eval``         – runs the benchmark on the requested models and judges

Results are written to ``eval/results/<run_id>/``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


def list_models() -> None:
    req = urllib.request.Request("https://openrouter.ai/api/v1/models")
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)["data"]
    keep = []
    for m in data:
        if not m["id"].split("/")[0] in {"anthropic", "openai", "google"}:
            continue
        keep.append({
            "id": m["id"],
            "name": m.get("name"),
            "created": m.get("created"),
            "context_length": m.get("context_length"),
            "pricing": m.get("pricing"),
            "max_completion_tokens": (m.get("top_provider") or {}).get("max_completion_tokens"),
            "supported_parameters": m.get("supported_parameters"),
        })
    keep.sort(key=lambda m: m.get("created") or 0, reverse=True)
    RESULTS.mkdir(parents=True, exist_ok=True)
    (RESULTS / "models.json").write_text(json.dumps(keep, indent=2), encoding="utf-8")
    print(f"Saved {len(keep)} models")


def run_eval(req: dict, dry: bool) -> None:
    run_id = req["run_id"]
    out = RESULTS / run_id
    log_dir = out / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (out / "request.json").write_text(json.dumps(req, indent=2), encoding="utf-8")

    models = ["mockllm/model"] if dry else req["models"]
    judges = [] if dry else req.get("judges", [])
    cmd = [
        "inspect", "eval", str(ROOT / "eval" / "regbench.py"),
        "--model", ",".join(models),
        "--log-dir", str(log_dir),
        "--log-format", "eval",
        "--max-connections", str(req.get("max_connections", 6)),
        "--reasoning-effort", req.get("reasoning_effort", "high"),
        "--display", "plain",
        "--no-fail-on-error",
        "-T", f"web={'none' if dry else req.get('web', 'search')}",
    ]
    if judges:
        cmd += ["-T", f"judges={','.join(judges)}"]
    if req.get("cases"):
        cmd += ["-T", f"cases={req['cases']}"]
    if req.get("attempts"):
        cmd += ["-T", f"attempts={req['attempts']}"]
    if req.get("max_tokens"):
        cmd += ["--max-tokens", str(req["max_tokens"])]
    if req.get("provider"):
        cmd += ["-M", f"provider={json.dumps(req['provider'])}"]
    print(" ".join(cmd), flush=True)
    with open(out / "run_output.txt", "w", encoding="utf-8") as fh:
        result = subprocess.run(cmd, cwd=ROOT, check=False, stdout=fh, stderr=subprocess.STDOUT)
    with open(out / "report_output.txt", "w", encoding="utf-8") as fh:
        subprocess.run([sys.executable, str(ROOT / "eval" / "report.py"), str(out)],
                       check=False, stdout=fh, stderr=subprocess.STDOUT)
    if result.returncode != 0:
        print(f"inspect exited with {result.returncode}; see the logs and report")


def main() -> None:
    req = json.loads((ROOT / "eval" / "run_request.json").read_text(encoding="utf-8"))
    mode = req.get("mode", "eval")
    if mode == "list_models":
        list_models()
    elif mode == "rescore":
        if not os.environ.get("OPENROUTER_API_KEY"):
            sys.exit("OPENROUTER_API_KEY secret is not set")
        subprocess.run([sys.executable, str(ROOT / "eval" / "rescore.py")], check=True)
    elif mode in {"dry_run", "eval"}:
        if mode == "eval" and not os.environ.get("OPENROUTER_API_KEY"):
            out = RESULTS / req["run_id"]
            out.mkdir(parents=True, exist_ok=True)
            (out / "error.txt").write_text("OPENROUTER_API_KEY secret is not set\n", encoding="utf-8")
            sys.exit("OPENROUTER_API_KEY secret is not set")
        run_eval(req, dry=(mode == "dry_run"))
    else:
        sys.exit(f"Unknown mode: {mode}")


if __name__ == "__main__":
    main()
