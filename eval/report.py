"""Turn Inspect logs into readable Markdown: answers, per-criterion grades and a summary.

Usage: python eval/report.py eval/results/<run_id>
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

from inspect_ai.log import list_eval_logs, read_eval_log


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "-", name.replace("openrouter/", "")).strip("-")


def main(out_dir: str) -> None:
    out = Path(out_dir)
    answers = out / "answers"
    grading = out / "grading"
    answers.mkdir(parents=True, exist_ok=True)
    grading.mkdir(parents=True, exist_ok=True)

    table: dict[str, dict[str, str]] = defaultdict(dict)
    usage_rows = []
    errors = []
    models = []
    for info in list_eval_logs(str(out / "logs")):
        log = read_eval_log(info)
        model = log.eval.model
        models.append(model)
        for sample in log.samples or []:
            case = str(sample.id)
            text = sample.output.completion if sample.output else ""
            if sample.error:
                errors.append(f"- {case} / {model}: {sample.error.message[:300]}")
            (answers / f"{case}__{slug(model)}.md").write_text(text or "(no answer)", encoding="utf-8")

            usage = sample.model_usage or {}
            for used_model, u in usage.items():
                usage_rows.append(
                    f"| {case} | {used_model} | {u.input_tokens:,} | {u.output_tokens:,} | "
                    f"{(u.reasoning_tokens or 0):,} | {u.total_cost if u.total_cost is not None else ''} |"
                )

            scores = sample.scores or {}
            score = next(iter(scores.values()), None)
            if score is None:
                table[case][model] = "not scored"
                continue
            judges = (score.metadata or {}).get("judges", {})
            criteria = (score.metadata or {}).get("criteria", [])
            cell = f"**{float(score.value):.1f}**" if isinstance(score.value, (int, float)) else str(score.value)
            parts = [f"{slug(j)}{'*' if r.get('self') else ''} {r['final']:.1f}"
                     for j, r in judges.items() if "final" in r]
            table[case][model] = cell + (f" ({'; '.join(parts)})" if parts else "")

            lines = [f"# {case} — {model}", "", f"Mean score: {cell}", ""]
            header = "| ID | Max | " + " | ".join(slug(j) for j in judges) + " |"
            lines += [header, "|" + "---|" * (2 + len(judges))]
            for c in criteria:
                awards = []
                for r in judges.values():
                    a = r.get("awards", {}).get(c["id"])
                    awards.append("error" if a is None else f"{a['award']:g}")
                lines.append(f"| {c['id']} | {c['max']:g} | " + " | ".join(awards) + " |")
            lines.append("")
            for j, r in judges.items():
                lines += [f"## Judge: {j}", ""]
                if "error" in r:
                    lines += [f"Error: {r['error']}", ""]
                    continue
                lines.append(f"Positive {r['positive']:.1f}, negative {r['negative']:.1f}, final {r['final']:.1f}")
                lines.append("")
                for c in criteria:
                    a = r["awards"][c["id"]]
                    lines.append(f"- **{c['id']}** ({c['max']:g}) → {a['award']:g}: {a['reason']}")
                lines.append("")
            (grading / f"{case}__{slug(model)}.md").write_text("\n".join(lines), encoding="utf-8")

    models = sorted(set(models))
    summary = ["# Run summary", "",
               "Official score = mean of the judges from other developers. "
               "A judge marked * graded its own developer's model (self-preference check; "
               "not part of the official score).", "",
               "| Case | " + " | ".join(slug(m) for m in models) + " |",
               "|---|" + "---|" * len(models)]
    for case in sorted(table):
        summary.append(f"| {case} | " + " | ".join(table[case].get(m, "—") for m in models) + " |")
    summary += ["", "## Token usage", "", "| Case | Model | Input | Output | Reasoning | Cost (USD) |",
                "|---|---|---|---|---|---|", *usage_rows]
    if errors:
        summary += ["", "## Errors", "", *errors]
    (out / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))


if __name__ == "__main__":
    main(sys.argv[1])
