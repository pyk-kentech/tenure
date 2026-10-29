"""M54 꺾는 점·그룹 상자 (DESIGN.md 36.39): [editor] 표에 저장하고 다시 읽기."""

from __future__ import annotations

import tomllib

from tenure.workflow import Workflow, parse_workflow
from tenure.workflow_toml import dump_workflow

WIRE = "step:1/next>step:2/prev"
FLOW = {
    "name": "묶음",
    "roles": {"claude": {"agent": "claude"}, "codex": {"agent": "codex"}},
    "steps": [
        {"kind": "build", "id": "build", "implementer": "claude", "reviewers": ["codex"], "next": "audit"},
        {"kind": "audit", "id": "audit", "roles": ["codex"], "next": "end"},
    ],
    "editor": {
        "nodes": {"step:1": [0, 0], "step:2": [400, 0]},
        "bends": [{"wire": WIRE, "x": 200, "y": -80}, {"wire": WIRE, "x": 300, "y": 40}],
        "groups": [{"title": "구현 묶음", "x": -40, "y": -60, "w": 520, "h": 240}],
    },
}


def _parse(data):
    return parse_workflow(data, name_hint="x", source="file", where="test")


def test_bends_and_groups_round_trip():
    wf = _parse(FLOW)
    assert wf.editor["bends"] == FLOW["editor"]["bends"]
    assert wf.editor["groups"] == FLOW["editor"]["groups"]
    text = dump_workflow(wf.to_dict())
    assert "[[editor.bends]]" in text and "[[editor.groups]]" in text
    again = _parse(tomllib.loads(text))
    assert again.editor == wf.editor
    assert Workflow.from_dict(wf.to_dict()).editor == wf.editor


def test_bad_bends_and_groups_are_dropped():
    bad = {**FLOW, "editor": {
        "bends": [{"wire": "엉터리", "x": 1, "y": 1}, {"wire": WIRE, "x": "1", "y": 1}, "x", {"wire": WIRE, "x": 5, "y": 6}],
        "groups": [{"title": "a", "x": 0, "y": 0, "w": 0, "h": 10}, {"title": 3, "x": 0, "y": 0, "w": 9, "h": 9},
                   {"x": 1, "y": 2, "w": 30, "h": 40, "title": "가" * 200}],
    }}
    ed = _parse(bad).editor
    assert ed["bends"] == [{"wire": WIRE, "x": 5, "y": 6}]
    assert len(ed["groups"]) == 1 and len(ed["groups"][0]["title"]) == 80
    assert "editor" not in _parse({**FLOW, "editor": {"bends": [], "groups": []}}).to_dict()
