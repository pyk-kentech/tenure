"""M45 자유 그래프 워크플로 (DESIGN.md 36.30): 갈림·이어서 고치기·나란히·반복 상한."""

from __future__ import annotations

import threading

import pytest

import tenure.orchestrator as orch_mod
from tenure.agents.mock import MockAdapter
from tenure.cli import main
from tenure.state import RunState, list_run_dirs
from tenure.workflow import WorkflowError, parse_workflow

ROLES = {"dev": {"agent": "claude"}, "rev": {"agent": "codex"}, "fixer": {"agent": "codex"},
         "rev2": {"agent": "claude"}, "aud1": {"agent": "claude"}, "aud2": {"agent": "codex"}}

RESCUE = [
    {"kind": "build", "implementer": "dev", "reviewers": ["rev"], "max_review": 1, "design": "task"},
    {"kind": "branch", "id": "ok", "check": "approved", "yes": "checks", "no": "rescue", "max_visits": 2},
    {"kind": "continue", "id": "rescue", "implementer": "fixer", "reviewers": ["rev2"], "rounds": 2, "next": "ok"},
    {"kind": "parallel", "id": "checks", "branches": ["a1", "a2"], "next": "end"},
    {"kind": "audit", "id": "a1", "roles": ["aud1"]},
    {"kind": "audit", "id": "a2", "roles": ["aud2"]},
]


def parse(steps, roles=ROLES):
    return parse_workflow({"roles": roles, "steps": steps}, name_hint="g", source="file", where="t")


def test_graph_format_is_detected_and_round_trips():
    wf = parse(RESCUE)
    assert wf.is_graph and not parse(RESCUE[:1]).is_graph
    again = parse(wf.to_dict()["steps"])
    assert [s.to_dict() for s in again.steps] == [s.to_dict() for s in wf.steps]
    assert "branch(approved? checks : rescue)" in wf.summary()


@pytest.mark.parametrize("steps, msg", [
    ([*RESCUE, {"kind": "audit", "id": "a1", "roles": ["aud1"]}], "겹치면 안 됩니다"),
    ([{**RESCUE[0]}, {**RESCUE[1], "yes": "nowhere"}, *RESCUE[2:]], "없는 단계"),
    ([RESCUE[0], {"kind": "parallel", "id": "p", "branches": ["b", "a1"]}, {**RESCUE[0], "id": "b"},
      RESCUE[4]], "build 단계는 하나만"),
    ([RESCUE[0], {"kind": "parallel", "id": "p", "branches": ["r", "a1"]},
      {"kind": "continue", "id": "r", "implementer": "fixer", "reviewers": ["rev2"]}, RESCUE[4]], "읽기만 하는"),
    ([{**RESCUE[0], "max_visits": 2}, RESCUE[4]], "max_visits"),
    ([RESCUE[0], {"kind": "audit", "id": "a1", "roles": ["aud1"], "next": "end"},
      {"kind": "audit", "id": "lost", "roles": ["aud2"]}], "닿지 않는"),
    ([{"kind": "explore", "role": "dev", "next": "end"},
      {"kind": "continue", "id": "r", "implementer": "fixer", "reviewers": ["rev2"]}], "build"),
    ([{**RESCUE[1], "yes": "end", "no": "end"}, RESCUE[0]], "build"),
])
def test_graph_checks(steps, msg):
    with pytest.raises(WorkflowError, match=msg):
        parse(steps)


def run_graph(tmp_path, monkeypatch, steps, agents=None, roles=ROLES):
    import tomllib  # noqa: F401  (형식 확인은 dump 가 함)
    from tenure.workflow_toml import dump_workflow

    wd = tmp_path / "w"
    (wd / ".tenure" / "workflows").mkdir(parents=True)
    (wd / ".tenure" / "workflows" / "g.toml").write_text(
        dump_workflow({"name": "g", "roles": roles, "steps": steps}), encoding="utf-8")
    (wd / "tenure.toml").write_text('test_command = "none"\n', encoding="utf-8")
    store: dict = {}

    def factory(cfg, wf, *, mock):
        store["a"] = {n: (agents or {}).get(n) or MockAdapter(label=n) for n in wf.used_roles()}
        return store["a"]

    monkeypatch.setattr(orch_mod, "make_role_agents", factory)
    rc = main(["run", "t", "--workdir", str(wd), "--mock", "--workflow", "g"])
    [rd] = list_run_dirs(wd)
    return rc, RunState.load(rd), store["a"]


def test_branch_continue_and_parallel_audits(tmp_path, monkeypatch):
    rc, st, agents = run_graph(tmp_path, monkeypatch, RESCUE)
    assert rc == 0 and st.status == "done_approved"
    assert st.graph["decisions"] == {"ok#1": "no", "ok#2": "yes"}
    assert [c.step for c in agents["rev"].calls] == ["review", "arbitrate"]   # build 는 1회에서 미승인
    assert [c.step for c in agents["fixer"].calls] == ["fix", "fix"]   # 이어서 고치기 2회
    assert st.graph["continue"] == {"rescue": 3} and len(st.review_history) == 3
    assert set(st.audits) == {"a1", "a2"} and st.graph["last_audits"] == ["a1", "a2"]


def test_parallel_audits_really_run_together(tmp_path, monkeypatch):
    barrier = threading.Barrier(2, timeout=10)

    def together(req, n):
        barrier.wait()        # 둘이 동시에 돌지 않으면 시간 초과로 깨진다
        return "## 반드시 고칠 것\n(없음)\n\n<verdict>\nstatus: APPROVED\nmust_fix: 0\n</verdict>\n"

    steps = [{**RESCUE[0], "max_review": 2}, {"kind": "parallel", "id": "checks", "branches": ["a1", "a2"]},
             RESCUE[4], RESCUE[5]]
    agents = {"aud1": MockAdapter(label="aud1", responder=together),
              "aud2": MockAdapter(label="aud2", responder=together)}
    rc, st, _ = run_graph(tmp_path, monkeypatch, steps, agents)
    assert rc == 0 and all(r["approved"] for sid in ("a1", "a2") for r in st.audits[sid])
    assert not barrier.broken


def test_loop_stops_at_max_visits(tmp_path, monkeypatch):
    # 테스트 명령이 없어 tests_pass 는 늘 아니오 → 고치기 ↔ 갈림을 돌다가 상한에서 멈춘다
    steps = [{**RESCUE[0], "max_review": 1},
             {"kind": "branch", "id": "t", "check": "tests_pass", "yes": "end", "no": "fix", "max_visits": 3},
             {"kind": "continue", "id": "fix", "implementer": "fixer", "reviewers": ["rev2"], "rounds": 1,
              "next": "t", "max_visits": 2}]
    rc, st, agents = run_graph(tmp_path, monkeypatch, steps)
    assert st.graph["decisions"] == {"t#1": "no", "t#2": "no", "t#3": "no"}
    assert st.graph["stopped_at"] == "fix" and len(agents["fixer"].calls) == 2
    assert {"continue:fix", "continue:fix_2"} <= set(st.done)
