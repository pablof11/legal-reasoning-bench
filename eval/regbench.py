"""Regulatory Benchmark (Customs, Trade and Tax) as an Inspect evaluation.

Each case in ``cases/`` becomes one sample. The model under test receives only the
contents of the case's ``solver/`` folder, rendered as text, plus one fixed
instruction in the case language. Answers are scored against the case rubric by
one or more judge models; a judge never grades a model from its own developer.

Run (from the repository root):

    inspect eval eval/regbench.py --model openrouter/anthropic/<model> \
        -T judges=openrouter/openai/<model>,openrouter/google/<model>

Task options (-T):
    cases     comma-separated case IDs (default: all)
    web       "search" (official-source web search through OpenRouter) or "none"
    judges    comma-separated judge models; empty string disables scoring
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import shutil
import subprocess
from pathlib import Path
from statistics import mean
from typing import Any

from inspect_ai import Task, task
from inspect_ai.dataset import MemoryDataset, Sample
from inspect_ai.model import GenerateConfig, ResponseSchema, get_model
from inspect_ai.scorer import Score, Target, mean as mean_metric, scorer, stderr
from inspect_ai.solver import Generate, TaskState, solver
from inspect_ai.util import json_schema
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent.parent
CASES_DIR = ROOT / "cases"
NEGATIVE_CAP = -30.0

INSTRUCTION = {
    "English": (
        "Please complete the assignment set out in solver_prompt.md. All case materials "
        "are reproduced below, one file per <file> block; read and use every file provided."
    ),
    "Spanish": (
        "Por favor, realice el encargo descrito en solver_prompt.md. Todo el material del "
        "caso se reproduce a continuación, un archivo por bloque <file>; lea y utilice "
        "todos los archivos proporcionados."
    ),
}

WEB_NOTE = {
    "English": (
        "A web search tool is available. Use it only for the official legal sources "
        "described in legal_research_sources.md."
    ),
    "Spanish": (
        "Dispone de una herramienta de búsqueda web. Úsela solo para las fuentes "
        "jurídicas oficiales descritas en legal_research_sources.md."
    ),
}


# --------------------------------------------------------------------------- #
# Case loading
# --------------------------------------------------------------------------- #
def _pdf_text(path: Path) -> str:
    if shutil.which("pdftotext"):
        out = subprocess.run(
            ["pdftotext", "-q", "-layout", str(path), "-"],
            capture_output=True, text=True, check=False,
        ).stdout
        if out.strip():
            return out
    from pypdf import PdfReader

    return "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _xlsx_text(path: Path) -> str:
    import openpyxl

    wb = openpyxl.load_workbook(path, data_only=True)
    parts = []
    for ws in wb.worksheets:
        buf = io.StringIO()
        writer = csv.writer(buf)
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                writer.writerow(["" if v is None else v for v in row])
        parts.append(f"--- sheet: {ws.title} ---\n{buf.getvalue()}")
    return "\n".join(parts)


def _file_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_text(path)
    if suffix in {".xlsx", ".xlsm"}:
        return _xlsx_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def case_meta(case_dir: Path) -> dict[str, Any]:
    readme = (case_dir / "README.md").read_text(encoding="utf-8")
    title = re.search(r"^# (.+)$", readme, re.M)
    lang = re.search(r"\*\*Case language\*\* \| (\w+)", readme)
    sources = (case_dir / "solver" / "legal_research_sources.md").read_text(encoding="utf-8")
    domains = sorted(
        {re.sub(r"^www\d?\.", "", d) for d in re.findall(r"https?://([^/\s)>]+)", sources)}
    )
    return {
        "case_id": case_dir.name.upper(),
        "title": title.group(1).strip() if title else case_dir.name,
        "language": lang.group(1) if lang else "English",
        "domains": domains,
    }


def render_solver(case_dir: Path) -> str:
    solver = case_dir / "solver"
    order = ["solver_prompt.md", "README_START_HERE.md", "legal_research_sources.md"]
    files = [solver / name for name in order if (solver / name).exists()]
    files += sorted(
        p for p in solver.rglob("*")
        if p.is_file() and p.name != "SHA256SUMS.txt" and p not in files
    )
    blocks = []
    for p in files:
        rel = p.relative_to(solver).as_posix()
        blocks.append(f'<file name="{rel}">\n{_file_text(p).strip()}\n</file>')
    return "\n\n".join(blocks)


def load_criteria(case_dir: Path) -> list[dict[str, Any]]:
    import openpyxl

    ws = openpyxl.load_workbook(case_dir / "evaluation" / "rubric.xlsx", data_only=True)["Scorecard"]
    criteria = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        cid, area, expectation, weight = (row + (None,) * 4)[:4]
        if not cid or not isinstance(weight, (int, float)):
            continue
        if re.search(r"TOTAL|FINAL|PUNTUA", str(cid), re.I):
            continue
        criteria.append({
            "id": str(cid).strip(),
            "area": area,
            "expectation": expectation,
            "max": float(weight),
            "negative": bool(re.match(r"^N\d+$", str(cid).strip())),
        })
    return criteria


def _as_list(value: str | list[str] | None) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


def build_dataset(cases: str | list[str] | None, web: str) -> MemoryDataset:
    wanted = {c.lower() for c in _as_list(cases)} or None
    samples = []
    for case_dir in sorted(p for p in CASES_DIR.iterdir() if (p / "solver").is_dir()):
        if wanted and case_dir.name.lower() not in wanted:
            continue
        meta = case_meta(case_dir)
        lang = meta["language"] if meta["language"] in INSTRUCTION else "English"
        text = INSTRUCTION[lang]
        if web == "search":
            text += " " + WEB_NOTE[lang]
        samples.append(Sample(
            id=meta["case_id"],
            input=f"{text}\n\n{render_solver(case_dir)}",
            target="See evaluation/rubric.md",
            metadata={**meta, "case_dir": case_dir.name},
        ))
    if not samples:
        raise ValueError(f"No cases matched: {cases}")
    return MemoryDataset(samples)


# --------------------------------------------------------------------------- #
# Rubric scorer
# --------------------------------------------------------------------------- #
JUDGE_PROMPT = """You are grading a legal work product against a fixed rubric.

Grade ONLY against the rubric, the credit anchors and the grading protocol below. The
reference answer shows one complete correct response; a different structure or wording
earns full credit when it reaches the same legally correct proposition. Do not reward
length. Do not penalise an upstream error again in downstream figures that inherit it.

Award every criterion exactly 0, 0.5 or 1:
- positive criteria (R…, I… and similar IDs): 1 = fully met, 0.5 = partially met, 0 = not met;
- negative criteria (N…): 1 = the harmful act clearly occurs, 0.5 = it partly occurs, 0 = it
  does not occur. A negative requires a clear, material and affirmative error that the
  negative criterion describes, not an omission and not a legal position that the credit
  anchors accept or that a competent practitioner could reasonably hold. When in doubt, award 0.
  An error already penalised by a positive criterion is not also a negative unless the
  negative criterion describes it specifically.

Return a JSON object with one entry per criterion. Keep each reason under 40 words and quote
or point to the part of the response it relies on. Include every one of these IDs exactly
once: {ids}

<grading_protocol>
{protocol}
</grading_protocol>

<rubric_with_credit_anchors>
{rubric}
</rubric_with_credit_anchors>

<reference_answer>
{golden}
</reference_answer>

<response_to_grade>
{response}
</response_to_grade>
"""


class CriterionGrade(BaseModel):
    id: str = Field(description="Criterion ID exactly as in the rubric")
    award: float = Field(description="0, 0.5 or 1")
    reason: str = Field(description="Under 40 words")


class Grades(BaseModel):
    criteria: list[CriterionGrade]


Grades.model_rebuild(_types_namespace={"CriterionGrade": CriterionGrade})
GRADES_SCHEMA = ResponseSchema(name="grades", json_schema=json_schema(Grades))
JUDGE_CONFIG = GenerateConfig(
    max_tokens=64000, reasoning_effort="medium", response_schema=GRADES_SCHEMA
)


def _family(model_name: str) -> str:
    parts = model_name.split("/")
    if parts[0] == "openrouter" and len(parts) > 1:
        return parts[1]
    return parts[0]


def _parse_awards(text: str, ids: list[str]) -> dict[str, dict[str, Any]]:
    """Accept the requested {"criteria": [...]} shape and the common variants judges
    return: a bare list, or an object keyed by criterion ID; "award" or "score"."""
    match = re.search(r"[\[{].*[\]}]", text, re.S)
    if not match:
        raise ValueError("no JSON in judge output")
    data = json.loads(match.group(0))
    if isinstance(data, dict) and isinstance(data.get("criteria"), list):
        items = data["criteria"]
    elif isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = [{"id": k, **v} if isinstance(v, dict) else {"id": k, "award": v}
                 for k, v in data.items()]
    else:
        raise ValueError("unrecognised judge output")
    awards = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id", "")).strip()
        if cid in ids:
            raw = item.get("award", item.get("score", 0))
            try:
                award = float(raw)
            except (TypeError, ValueError):
                award = 0.0
            awards[cid] = {
                "award": award if award in (0.0, 0.5, 1.0) else 0.0,
                "reason": str(item.get("reason", "")),
            }
    missing = [i for i in ids if i not in awards]
    if len(missing) > len(ids) // 4:
        raise ValueError(f"judge omitted {len(missing)} criteria")
    for cid in missing:
        awards[cid] = {"award": 0.0, "reason": "Not graded by judge (treated as 0)."}
    return awards


def _total(criteria: list[dict[str, Any]], awards: dict[str, dict[str, Any]]) -> dict[str, float]:
    pos = sum(c["max"] * awards[c["id"]]["award"] for c in criteria if not c["negative"])
    neg = sum(c["max"] * awards[c["id"]]["award"] for c in criteria if c["negative"])
    neg = max(neg, NEGATIVE_CAP)
    return {"positive": pos, "negative": neg, "final": min(100.0, max(0.0, pos + neg))}


@scorer(metrics=[mean_metric(), stderr()])
def rubric_judge(judges: list[str]):
    async def score(state: TaskState, target: Target) -> Score:
        case_dir = CASES_DIR / state.metadata["case_dir"]
        criteria = load_criteria(case_dir)
        ids = [c["id"] for c in criteria]
        response = state.output.completion
        if not response.strip():
            return Score(value=0.0, explanation="Empty response.", metadata={"judges": {}})
        evaluated = _family(str(state.model))
        prompt = JUDGE_PROMPT.format(
            ids=", ".join(ids),
            protocol=(case_dir / "evaluation" / "grading_protocol.md").read_text(encoding="utf-8"),
            rubric=(case_dir / "evaluation" / "rubric.md").read_text(encoding="utf-8"),
            golden=(case_dir / "reference" / "golden.md").read_text(encoding="utf-8"),
            response=response,
        )
        results: dict[str, Any] = {}
        for judge_name in judges:
            judge = get_model(judge_name)
            last_error = None
            for _ in range(3):
                out = await judge.generate(prompt, config=JUDGE_CONFIG)
                try:
                    awards = _parse_awards(out.completion, ids)
                    results[judge_name] = {"awards": awards, **_total(criteria, awards)}
                    break
                except (ValueError, json.JSONDecodeError) as err:
                    last_error = str(err)
            else:
                results[judge_name] = {"error": last_error}
            results[judge_name]["self"] = _family(judge_name) == evaluated
        # Official score: mean of judges from other developers. Self-grades are kept
        # separately to measure self-preference.
        finals = [r["final"] for r in results.values() if "final" in r and not r["self"]]
        self_finals = [r["final"] for r in results.values() if "final" in r and r["self"]]
        if not finals:
            return Score(value=0.0, explanation="No independent judge produced a valid grade.",
                         metadata={"judges": results})
        summary = "; ".join(
            f"{name}{' (self)' if r['self'] else ''}: {r['final']:.1f} "
            f"(+{r['positive']:.1f} / {r['negative']:.1f})"
            for name, r in results.items() if "final" in r
        )
        return Score(value=mean(finals), explanation=summary,
                     metadata={"judges": results, "criteria": criteria,
                               "self_grade": mean(self_finals) if self_finals else None})

    return score


# --------------------------------------------------------------------------- #
# Solver
# --------------------------------------------------------------------------- #
@solver
def answer_case(web: str = "search", attempts: int = 5):
    """Single generation; with web search, OpenRouter runs one shared search tool
    for every model, limited to the case's official legal-source domains."""

    async def solve(state: TaskState, generate: Generate) -> TaskState:
        kwargs: dict[str, Any] = {}
        if web == "search":
            kwargs["extra_body"] = {
                "tools": [{
                    "type": "openrouter:web_search",
                    "parameters": {
                        "allowed_domains": state.metadata["domains"],
                        "max_results": 5,
                    },
                }],
                "max_tool_calls": 20,
            }
        # Retry when the provider returns no complete answer (e.g. a transient overload
        # or a stream cut off mid-answer).
        messages = list(state.messages)
        for attempt in range(1, attempts + 1):
            state.messages = list(messages)
            state = await generate(state, **kwargs)
            if (state.output and state.output.completion.strip()
                    and state.output.stop_reason == "stop"):
                break
            if attempt < attempts:
                await asyncio.sleep(60 * attempt)
        state.metadata["attempts"] = attempt
        return state

    return solve


# --------------------------------------------------------------------------- #
# Task
# --------------------------------------------------------------------------- #
@task
def regbench(
    cases: str | list[str] | None = None,
    web: str = "search",
    judges: str | list[str] | None = None,
    attempts: int = 5,
) -> Task:
    if web not in {"search", "none"}:
        raise ValueError("web must be 'search' or 'none'")
    dataset = build_dataset(cases, web)
    judge_list = _as_list(judges)

    return Task(
        dataset=dataset,
        solver=answer_case(web, int(attempts)),
        scorer=rubric_judge(judge_list) if judge_list else None,
        config=GenerateConfig(max_tokens=60000, reasoning_effort="high"),
    )
