"""Apply the author's review decisions to the judges' grades and compute final scores.

Usage: python eval/apply_review.py <grades.json> <human_review.json> <out.json>
For each answer, every independent (other-developer) judge's award on a reviewed
criterion is replaced by the author's award; the final score is the mean of the
independent judges' recomputed totals.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

sys.path.insert(0, str(Path(__file__).resolve().parent))
import regbench as rb  # noqa: E402


def main(grades_path: str, review_path: str, out_path: str) -> None:
    grades = json.loads(Path(grades_path).read_text(encoding="utf-8"))
    review = json.loads(Path(review_path).read_text(encoding="utf-8"))["decisions"]
    overrides: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for d in review:
        overrides[(d["case"], d["model"])][d["criterion"]] = d["award"]
    out: dict[str, dict[str, float]] = defaultdict(dict)
    for key, item in grades.items():
        case, model = key.split("|")
        case_dir = next(p for p in rb.CASES_DIR.iterdir() if p.name.upper() == case)
        criteria = rb.load_criteria(case_dir)
        finals = []
        for g in item["grades"].values():
            if "final" not in g or g.get("self"):
                continue
            awards = {k: v["award"] for k, v in g["awards"].items()}
            awards.update(overrides.get((case, model), {}))
            finals.append(rb._total(criteria, {c["id"]: {"award": awards[c["id"]]}
                                               for c in criteria})["final"])
        out[model][case] = round(mean(finals), 2)
    Path(out_path).write_text(json.dumps(out, indent=1, sort_keys=True), encoding="utf-8")
    print(json.dumps(out, indent=1, sort_keys=True))


if __name__ == "__main__":
    main(*sys.argv[1:4])
