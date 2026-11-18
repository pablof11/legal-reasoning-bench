"""Offline tests: no API key or network access needed (mockllm only)."""

from __future__ import annotations

import importlib
import json

import pytest
from inspect_ai import eval as inspect_eval
from inspect_ai.model import ModelOutput, get_model
from inspect_ai.solver import TaskState

from legal_reasoning_bench import legal_reasoning_bench

lrb = importlib.import_module("legal_reasoning_bench.legal_reasoning_bench")

CASE_IDS = ["cbam-001", "co-fx-001", "co-val-001", "uk-ori-001", "uk-val-001", "us-pen-001"]


def test_build_dataset_loads_every_case():
    dataset = lrb.build_dataset(None, "none")
    assert sorted(s.metadata["case_dir"] for s in dataset) == CASE_IDS
    for sample in dataset:
        assert "<file name=" in sample.input
        assert sample.metadata["domains"], "each case lists its official legal-source domains"
        assert lrb.load_criteria(lrb.CASES_DIR / sample.metadata["case_dir"])


def test_build_dataset_filters_cases_and_adds_web_note():
    dataset = lrb.build_dataset("uk-val-001", "search")
    assert len(dataset) == 1
    assert lrb.WEB_NOTE["English"] in dataset[0].input
    with pytest.raises(ValueError):
        lrb.build_dataset("no-such-case", "none")


def test_parse_awards_accepts_common_judge_shapes():
    ids = ["P1", "P2", "N1"]
    expected = {"P1": 1.0, "P2": 0.5, "N1": 0.0}
    shapes = [
        {"criteria": [{"id": k, "award": v, "reason": "r"} for k, v in expected.items()]},
        [{"id": k, "score": v} for k, v in expected.items()],
        {k: {"award": v} for k, v in expected.items()},
    ]
    for shape in shapes:
        awards = lrb._parse_awards("Here you go: " + json.dumps(shape), ids)
        assert {k: a["award"] for k, a in awards.items()} == expected


def test_parse_awards_rejects_invalid_output():
    ids = ["P1", "P2", "P3", "P4"]
    with pytest.raises(ValueError):
        lrb._parse_awards("no json here", ids)
    with pytest.raises(ValueError):
        lrb._parse_awards(json.dumps([{"id": "P1", "award": 1}]), ids)
    awards = lrb._parse_awards(json.dumps([{"id": i, "award": 0.7} for i in ids]), ids)
    assert all(a["award"] == 0.0 for a in awards.values()), "off-scale awards count as 0"


def test_total_caps_penalties_and_bounds_score():
    criteria = [
        {"id": "P1", "max": 60, "negative": False},
        {"id": "N1", "max": -25, "negative": True},
        {"id": "N2", "max": -25, "negative": True},
    ]
    awards = {"P1": {"award": 1.0}, "N1": {"award": 1.0}, "N2": {"award": 1.0}}
    total = lrb._total(criteria, awards)
    assert total["negative"] == lrb.NEGATIVE_CAP
    assert total["final"] == 60 + lrb.NEGATIVE_CAP
    awards["P1"]["award"] = 0.0
    assert lrb._total(criteria, awards)["final"] == 0.0


def test_family_groups_openrouter_models_by_developer():
    assert lrb._family("openrouter/openai/some-model") == "openai"
    assert lrb._family("anthropic/some-model") == "anthropic"


@pytest.mark.asyncio
async def test_rubric_judge_excludes_self_grade(monkeypatch):
    case_dir = "uk-val-001"
    criteria = lrb.load_criteria(lrb.CASES_DIR / case_dir)
    full = json.dumps({"criteria": [
        {"id": c["id"], "award": 0 if c["negative"] else 1, "reason": "ok"} for c in criteria]})
    empty = json.dumps({"criteria": [{"id": c["id"], "award": 0, "reason": "no"} for c in criteria]})
    outputs = {"openrouter/openai/judge": full, "openrouter/anthropic/judge": empty}

    def fake_get_model(name, *args, **kwargs):
        return get_model("mockllm/model",
                         custom_outputs=[ModelOutput.from_content("mockllm/model", outputs[name])])

    monkeypatch.setattr(lrb, "get_model", fake_get_model)
    state = TaskState(
        model="openrouter/anthropic/model-under-test", sample_id="UK-VAL-001", epoch=1,
        input="", messages=[], output=ModelOutput.from_content("mockllm/model", "An answer."),
        metadata={"case_dir": case_dir},
    )
    score = await lrb.rubric_judge(list(outputs))(state, None)
    expected = lrb._total(criteria, {c["id"]: {"award": 0.0 if c["negative"] else 1.0}
                                     for c in criteria})["final"]
    assert score.value == pytest.approx(expected)
    assert score.metadata["judges"]["openrouter/anthropic/judge"]["self"] is True
    assert score.metadata["self_grade"] == 0.0


def test_answer_case_requires_openrouter_for_web_search():
    [log] = inspect_eval(legal_reasoning_bench(cases="uk-val-001", web="search", judges="none"),
                         model="mockllm/model", display="none", log_dir="/tmp/lrb-test-logs")
    assert log.status == "error"
    assert "OpenRouter" in log.error.message


def test_end_to_end_with_mockllm():
    [log] = inspect_eval(legal_reasoning_bench(cases="uk-val-001", web="none", judges="none"),
                         model="mockllm/model", display="none", log_dir="/tmp/lrb-test-logs")
    assert log.status == "success"
    assert log.samples and log.samples[0].output.completion
