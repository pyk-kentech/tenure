"""M49 편집 화면 배치 [editor] (DESIGN.md 36.34): 노드 위치·선 묶기가 저장·읽기를 거쳐 그대로 돌아온다."""

from __future__ import annotations

import tomllib

import pytest

from tenure.config import Config
from tenure.registry import Registry
from tenure.ui.dashboard import Dashboard, dispatch
from tenure.workflow import Workflow, load_workflow, parse_workflow
from tenure.workflow_toml import dump_workflow


@pytest.fixture
def proj(tmp_path):
    wd = tmp_path / "proj"
    wd.mkdir()
    Registry().add(wd)
    return wd


EDITOR = {
    "nodes": {"role:claude": [-340, 0], "role:codex": [-340, 130], "step:1": [0, 0], "step:2": [300, -12]},
    "ties": [
        {"x": 120, "y": 40, "angle": 15,
         "wires": ["role:claude/out>step:2/implementer", "role:codex/out>step:2/reviewers"]},
    ],
}
FLOW = {
    "name": "묶기",
    "roles": {"claude": {"agent": "claude"}, "codex": {"agent": "codex"}},
    "steps": [
        {"kind": "debate", "proposers": ["claude", "codex"], "synth": "codex"},
        {"kind": "build", "implementer": "claude", "reviewers": ["codex"]},
    ],
    "editor": EDITOR,
}


def _parse(data):
    return parse_workflow(data, name_hint="t", source="file", where="시험")


def test_editor_round_trips_through_toml():
    text = dump_workflow(FLOW)
    assert "[editor]" in text and "[[editor.ties]]" in text
    assert tomllib.loads(text)["editor"] == EDITOR
    assert _parse(tomllib.loads(text)).editor == EDITOR


def test_editor_is_optional_and_does_not_change_the_run():
    plain = {k: v for k, v in FLOW.items() if k != "editor"}
    a, b = _parse(plain), _parse(FLOW)
    assert a.editor == {} and "editor" not in a.to_dict()
    assert a.summary() == b.summary() and [s.to_dict() for s in a.steps] == [s.to_dict() for s in b.steps]
    assert "[editor]" not in dump_workflow(plain)


def test_malformed_editor_entries_are_dropped_not_fatal():
    bad = {**FLOW, "editor": {
        "nodes": {"role:claude": [1, 2], "step:0": [1, 2], "nope": [1, 2], "step:3": [1.5, 2],
                  "role:codex": [1, 2, 3], "step:2": [True, 0]},
        "ties": [
            {"x": 1, "y": 2, "angle": 400, "wires": ["step:1/next>step:2/prev", "bad wire", 3,
                                                     "step:1/next>step:2/prev"]},
            {"x": 1, "y": 2, "wires": ["not-a-wire"]},
            {"x": "1", "y": 2, "wires": ["step:1/next>step:2/prev"]},
            "tie",
        ],
        "extra": 1,
    }}
    wf = _parse(bad)
    assert wf.editor == {
        "nodes": {"role:claude": [1, 2]},
        "ties": [{"x": 1, "y": 2, "angle": 40, "wires": ["step:1/next>step:2/prev"]}],
    }
    assert _parse({**FLOW, "editor": "x"}).editor == {}


def test_run_snapshot_keeps_editor():
    wf = _parse(FLOW)
    assert Workflow.from_dict(wf.to_dict()).editor == EDITOR


def test_api_get_and_save_keep_editor(proj):
    d = Dashboard()
    dispatch(d, "workflow_save", {"project": str(proj), "name": "tied", "workflow": FLOW})
    assert load_workflow("tied", proj, Config()).editor == EDITOR
    out = dispatch(d, "workflow_get", {"project": str(proj), "name": "tied"})
    assert out["definition"]["editor"] == EDITOR
    assert "editor" not in dispatch(d, "workflow_get", {"project": str(proj), "name": "secure"})["definition"]
