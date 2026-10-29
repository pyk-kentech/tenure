"""M50 실행 보기 실시간 진행 (DESIGN.md 36.35): 호출·로그 이벤트에 워크플로 단계 id 가 붙는다."""

from __future__ import annotations

import threading

from tenure.events import read_events
from tenure.phases.common import in_wf_step, wf_step_meta
from tenure.state import list_run_dirs
from tenure.transcript import Transcript
from tests.test_m45 import RESCUE, run_graph


def _calls(rd):
    return [e for e in read_events(rd) if e["kind"] == "step_start"]


def test_graph_run_tags_every_call_with_its_step(tmp_path, monkeypatch):
    rc, _st, _agents = run_graph(tmp_path, monkeypatch, RESCUE)
    assert rc == 0
    [rd] = list_run_dirs(tmp_path / "w")
    starts = _calls(rd)
    by_role = {}
    for e in starts:
        by_role.setdefault(e["role"], set()).add((e.get("wf_step"), e.get("visit", 1)))
    assert by_role["dev"] == {("build", 1)}
    assert by_role["fixer"] == {("rescue", 1)}
    # Why: parallel branches run in their own threads; each still knows its own step.
    assert by_role["aud1"] == {("a1", 1)} and by_role["aud2"] == {("a2", 1)}
    ends = [e for e in read_events(rd) if e["kind"] == "step_end"]
    assert all(e.get("wf_step") for e in ends if e["role"] != "report")
    branch_logs = [e for e in read_events(rd) if e["kind"] == "log" and e.get("wf_step") == "ok"]
    assert [e.get("visit", 1) for e in branch_logs] == [1, 2]       # 분기를 두 번 지남
    metas = [e.meta for e in Transcript(rd).entries()]
    assert {m.get("wf_step") for m in metas if m.get("role") == "aud2"} == {"a2"}


def test_linear_run_tags_calls(tmp_path, monkeypatch):
    steps = [{"kind": "build", "implementer": "dev", "reviewers": ["rev"], "max_review": 2, "design": "task"},
             {"kind": "audit", "id": "check", "roles": ["aud1"]}]
    rc, st, _ = run_graph(tmp_path, monkeypatch, steps)
    assert rc == 0 and st.status == "done_approved"
    [rd] = list_run_dirs(tmp_path / "w")
    tagged = {e["role"]: e.get("wf_step") for e in _calls(rd)}
    # Why: the closing summary is not part of any step.
    assert tagged == {"dev": "build", "rev": "build", "aud1": "check", "report": None}


def test_step_mark_is_per_thread_and_restored():
    assert wf_step_meta() == {}
    seen = {}

    def other():
        seen["thread"] = wf_step_meta()

    with in_wf_step("audit", 2):
        assert wf_step_meta() == {"wf_step": "audit", "visit": 2}
        t = threading.Thread(target=other)
        t.start()
        t.join()
    assert seen["thread"] == {} and wf_step_meta() == {}


def test_background_runs_get_a_hidden_console_on_windows(monkeypatch):
    """콘솔 없이 띄우면 그 아래 콘솔 프로그램마다 창이 떴다 꺼진다(2026-10-29). 숨은 콘솔로 띄운다."""
    from tenure import procs

    monkeypatch.setattr(procs, "IS_WINDOWS", True)
    flags = procs.detached_kwargs()["creationflags"]
    assert flags & 0x08000000            # CREATE_NO_WINDOW
    assert not flags & 0x00000008        # DETACHED_PROCESS
