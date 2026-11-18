"""Grade saved answers without generating them again.

Reads ``eval/run_request.json``:

    {"run_id": "010-round-1-final", "mode": "rescore",
     "sources": ["008-round-1", "009-gemini-retry"],
     "judges": ["openrouter/...", ...], "max_concurrency": 4}

For every complete answer in the source runs (a later source replaces an earlier
answer for the same case and model), each judge's grade is taken from the source
logs when one was already produced and parses; otherwise the judge is called.
Writes ``grades.json``, ``summary.md``, ``answers/`` and ``grading/`` to
``eval/results/<run_id>/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import importlib  # noqa: E402
rb = importlib.import_module("legal_reasoning_bench.legal_reasoning_bench")
from inspect_ai.log import list_eval_logs, read_eval_log  # noqa: E402
from inspect_ai.model import get_model  # noqa: E402
from report import slug  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


def collect(sources: list[str], judges: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    items: dict[tuple[str, str], dict[str, Any]] = {}
    for run in sources:
        for info in list_eval_logs(str(RESULTS / run / "logs")):
            log = read_eval_log(info)
            model = log.eval.model
            for s in log.samples or []:
                answer = s.output.completion if s.output else ""
                complete = bool(answer.strip()) and s.output.stop_reason == "stop"
                if not complete:
                    continue
                case_dir = rb.CASES_DIR / s.metadata["case_dir"]
                criteria = rb.load_criteria(case_dir)
                ids = [c["id"] for c in criteria]
                cached: dict[str, Any] = {}
                for judge in judges:
                    for e in s.events:
                        if (e.event == "model" and str(e.model) == judge and e.output
                                and e.output.completion and e.output.completion != answer):
                            try:
                                awards = rb._parse_awards(e.output.completion, ids)
                            except Exception:  # noqa: BLE001
                                continue
                            cached[judge] = {"awards": awards, **rb._total(criteria, awards),
                                             "source": run}
                items[(str(s.id), model)] = {
                    "case_dir": s.metadata["case_dir"], "answer": answer,
                    "answer_sha": hashlib.sha256(answer.encode()).hexdigest()[:16],
                    "criteria": criteria, "ids": ids, "grades": cached, "source": run,
                }
    return items


def merge_saved_grades(items: dict, grade_sources: list[str]) -> None:
    """Reuse grades saved by earlier rescore runs for identical answers."""
    for run in grade_sources:
        path = RESULTS / run / "grades.json"
        if not path.exists():
            continue
        saved = json.loads(path.read_text(encoding="utf-8"))
        for key, it in items.items():
            old = saved.get(f"{key[0]}|{key[1]}")
            if not old or old.get("answer_sha") != it["answer_sha"]:
                continue
            for judge, g in old.get("grades", {}).items():
                if "final" in g and judge not in it["grades"]:
                    it["grades"][judge] = g


async def grade_missing(items: dict, judges: list[str], limit: int) -> None:
    sem = asyncio.Semaphore(limit)

    async def one(key: tuple[str, str], judge: str) -> None:
        item = items[key]
        case_dir = rb.CASES_DIR / item["case_dir"]
        prompt = rb.JUDGE_PROMPT.format(
            ids=", ".join(item["ids"]),
            protocol=(case_dir / "evaluation" / "grading_protocol.md").read_text(encoding="utf-8"),
            rubric=(case_dir / "evaluation" / "rubric.md").read_text(encoding="utf-8"),
            golden=(case_dir / "reference" / "golden.md").read_text(encoding="utf-8"),
            response=item["answer"],
        )
        last_error = None
        async with sem:
            for attempt in range(3):
                try:
                    out = await get_model(judge).generate(prompt, config=rb.JUDGE_CONFIG)
                    awards = rb._parse_awards(out.completion, item["ids"])
                    item["grades"][judge] = {"awards": awards,
                                             **rb._total(item["criteria"], awards),
                                             "source": "rescore"}
                    print(f"graded {key[0]} / {key[1]} by {judge}", flush=True)
                    return
                except Exception as err:  # noqa: BLE001
                    last_error = f"{type(err).__name__}: {str(err)[:300]}"
                    await asyncio.sleep(30 * (attempt + 1))
        item["grades"][judge] = {"error": last_error}
        print(f"FAILED {key[0]} / {key[1]} by {judge}: {last_error}", flush=True)

    tasks = [one(k, j) for k, it in items.items() for j in judges if j not in it["grades"]]
    print(f"{len(tasks)} judgments to run", flush=True)
    await asyncio.gather(*tasks)


def write_report(out: Path, items: dict, judges: list[str]) -> None:
    (out / "answers").mkdir(parents=True, exist_ok=True)
    (out / "grading").mkdir(parents=True, exist_ok=True)
    models = sorted({m for _, m in items})
    cases = sorted({c for c, _ in items} | {
        rb.case_meta(p)["case_id"] for p in rb.CASES_DIR.iterdir() if (p / "solver").is_dir()})
    table: dict[str, dict[str, Any]] = {}
    for (case, model), it in items.items():
        fam = rb._family(model)
        for j, g in it["grades"].items():
            g["self"] = rb._family(j) == fam
        indep = [g["final"] for g in it["grades"].values() if "final" in g and not g["self"]]
        selfg = [g["final"] for g in it["grades"].values() if "final" in g and g["self"]]
        it["official"] = mean(indep) if indep else None
        it["self_grade"] = mean(selfg) if selfg else None
        table.setdefault(case, {})[model] = it
        name = f"{case}__{slug(model)}.md"
        (out / "answers" / name).write_text(it["answer"], encoding="utf-8")
        lines = [f"# {case} — {model}", "",
                 f"Official score (other developers' judges): "
                 f"{it['official']:.1f}" if it["official"] is not None else "Official score: n/a",
                 "", "| ID | Max | " + " | ".join(slug(j) + ("*" if rb._family(j) == fam else "")
                                                 for j in judges) + " |",
                 "|" + "---|" * (2 + len(judges))]
        for c in it["criteria"]:
            cells = []
            for j in judges:
                a = it["grades"].get(j, {}).get("awards", {}).get(c["id"])
                cells.append("—" if a is None else f"{a['award']:g}")
            lines.append(f"| {c['id']} | {c['max']:g} | " + " | ".join(cells) + " |")
        for j in judges:
            g = it["grades"].get(j, {})
            lines += ["", f"## Judge: {j}{' (same developer)' if g.get('self') else ''}", ""]
            if "final" not in g:
                lines.append(f"No grade: {g.get('error', 'not run')}")
                continue
            lines += [f"Positive {g['positive']:.1f}, negative {g['negative']:.1f}, "
                      f"final {g['final']:.1f}", ""]
            for c in it["criteria"]:
                a = g["awards"][c["id"]]
                lines.append(f"- **{c['id']}** ({c['max']:g}) → {a['award']:g}: {a['reason']}")
        (out / "grading" / name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def cell(it: dict | None) -> str:
        if not it:
            return "no answer"
        if it["official"] is None:
            return "not graded"
        s = f"**{it['official']:.1f}**"
        if it["self_grade"] is not None:
            s += f" (self {it['self_grade']:.1f})"
        return s

    summary = ["# Round 1 — summary", "",
               "Official score: mean of the judges from the other two developers (0–100). "
               "\"self\" is the grade given by the judge from the same developer; it is a "
               "self-preference check and not part of the official score.", "",
               "| Case | " + " | ".join(slug(m) for m in models) + " |",
               "|---|" + "---|" * len(models)]
    for case in cases:
        summary.append(f"| {case} | " + " | ".join(cell(table.get(case, {}).get(m))
                                                    for m in models) + " |")
    avgs = []
    for m in models:
        vals = [table[c][m]["official"] for c in table
                if m in table[c] and table[c][m]["official"] is not None]
        avgs.append(f"**{mean(vals):.1f}** ({len(vals)} cases)" if vals else "—")
    summary.append("| **Mean** | " + " | ".join(avgs) + " |")
    summary += ["", "## Judge agreement", "",
                "| Case | Model | " + " | ".join(slug(j) for j in judges) + " |",
                "|---|---|" + "---|" * len(judges)]
    for (case, model), it in sorted(items.items()):
        cells = []
        for j in judges:
            g = it["grades"].get(j, {})
            cells.append((f"{g['final']:.1f}" + ("*" if g.get("self") else ""))
                         if "final" in g else "—")
        summary.append(f"| {case} | {slug(model)} | " + " | ".join(cells) + " |")
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    slim = {f"{c}|{m}": {k: v for k, v in it.items() if k not in ("answer", "criteria", "ids")}
            for (c, m), it in items.items()}
    (out / "grades.json").write_text(json.dumps(slim, indent=1), encoding="utf-8")
    print("\n".join(summary))


def main() -> None:
    req = json.loads((ROOT / "eval" / "run_request.json").read_text(encoding="utf-8"))
    out = RESULTS / req["run_id"]
    out.mkdir(parents=True, exist_ok=True)
    (out / "request.json").write_text(json.dumps(req, indent=2), encoding="utf-8")
    judges = req["judges"]
    items = collect(req["sources"], judges)
    merge_saved_grades(items, req.get("grade_sources", []))
    print(f"{len(items)} complete answers found", flush=True)
    if not req.get("offline"):
        asyncio.run(grade_missing(items, judges, req.get("max_concurrency", 4)))
    write_report(out, items, judges)


if __name__ == "__main__":
    main()
