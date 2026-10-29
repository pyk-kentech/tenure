"""M52 써 보며 찾은 불편 2차 (DESIGN.md 36.37): 저장 이름 분리, 플로 보관·지우기, 실행 전 로그인 점검."""

from __future__ import annotations

import pytest

from tenure import doctor, worktree
from tenure.config import Config
from tenure.registry import Registry
from tenure.state import CallRecord, RunState, create_run_dir
from tenure.ui import dashboard as dashmod
from tenure.ui.dashboard import ApiError, Dashboard, dispatch
from tenure.workflow import load_workflow


@pytest.fixture
def proj(tmp_path):
    wd = tmp_path / "proj"
    wd.mkdir()
    Registry().add(wd)
    return wd


FLOW = {
    "name": "x",
    "roles": {"claude": {"agent": "claude"}, "codex": {"agent": "codex"}},
    "steps": [{"kind": "build", "implementer": "claude", "reviewers": ["codex"]}],
}
SOLO = {
    "name": "x",
    "roles": {"a": {"agent": "claude"}, "b": {"agent": "claude"}},
    "steps": [{"kind": "build", "implementer": "a", "reviewers": ["b"]}],
}


def _save(d, proj, **kw):
    return dispatch(d, "workflow_save", {"project": str(proj), "workflow": FLOW, **kw})


# ------------------------------------------------------------------ 4 저장 이름
def test_korean_title_gets_a_file_name(proj):
    d = Dashboard()
    out = _save(d, proj, title="내 빠른 흐름")
    assert out["id"] == "workflow" and out["name"] == "내 빠른 흐름"
    assert load_workflow("workflow", proj, Config()).name == "내 빠른 흐름"
    assert _save(d, proj, title="또 하나")["id"] == "workflow-2"          # 덮어쓰지 않고 새 이름
    assert _save(d, proj, title="My Flow 1!")["id"] == "my-flow-1"
    assert _save(d, proj, title="quick")["id"] == "quick-2"               # 내장 이름은 가리지 않는다
    listed = {w["id"]: w["name"] for w in dispatch(d, "workflows", {"project": str(proj)})["workflows"]}
    assert listed["workflow-2"] == "또 하나"


def test_title_with_id_overwrites_that_file(proj):
    d = Dashboard()
    first = _save(d, proj, title="처음")
    with pytest.raises(ApiError) as e:
        _save(d, proj, title="바꿈", id=first["id"])
    assert e.value.status == 409
    out = _save(d, proj, title="바꿈", id=first["id"], overwrite=True)
    assert out["id"] == first["id"] and load_workflow(first["id"], proj, Config()).name == "바꿈"


def test_title_rules(proj):
    d = Dashboard()
    for kw in ({"title": "가" * 81}, {"title": "x", "id": "Big"}, {}):
        with pytest.raises(ApiError) as e:
            _save(d, proj, **kw)
        assert e.value.status == 400


# ------------------------------------------------------------------ 9 보관·지우기
def _run(proj, run_id, **kw) -> RunState:
    rd = create_run_dir(proj, run_id)
    st = RunState(run_id=rd.name, task="할 일", **kw)
    st.save(rd)
    return st


def test_archive_hides_from_inbox_and_comes_back(proj):
    d = Dashboard()
    _run(proj, "20261029-100000", status="running")                       # 멈춘 채 남은 실행
    runs = dispatch(d, "runs", {"project": str(proj)})["runs"]
    assert runs[0]["archived_at"] is None and runs[0]["created_at"]
    assert dispatch(d, "inbox", {})["items"]
    before = runs[0]["updated_at"]
    dispatch(d, "archive_run", {"project": str(proj), "run": "20261029-100000"})
    after = dispatch(d, "runs", {"project": str(proj)})["runs"][0]
    assert after["archived_at"] and after["updated_at"] == before       # 걸린 시간은 그대로
    assert not dispatch(d, "inbox", {})["items"]
    dispatch(d, "archive_run", {"project": str(proj), "run": "20261029-100000", "archived": False})
    assert dispatch(d, "runs", {"project": str(proj)})["runs"][0]["archived_at"] is None


def test_delete_removes_the_record(proj):
    d = Dashboard()
    _run(proj, "20261029-100000", status="done_approved")
    _run(proj, "20261029-110000", status="done_approved")
    dispatch(d, "delete_run", {"project": str(proj), "run": "20261029-100000"})
    ids = [r["run_id"] for r in dispatch(d, "runs", {"project": str(proj)})["runs"]]
    assert ids == ["20261029-110000"]
    with pytest.raises(ApiError) as e:
        dispatch(d, "delete_run", {"project": str(proj), "run": "20261029-100000"})
    assert e.value.status == 404


def test_delete_refuses_live_and_unmerged(proj, monkeypatch):
    d = Dashboard()
    _run(proj, "20261029-100000", status="running")
    monkeypatch.setattr(Dashboard, "_run_live", lambda self, rd: {"run_id": rd.name})
    with pytest.raises(ApiError, match="멈추세요"):
        dispatch(d, "delete_run", {"project": str(proj), "run": "20261029-100000"})
    with pytest.raises(ApiError, match="보관할 수 없습니다"):
        dispatch(d, "archive_run", {"project": str(proj), "run": "20261029-100000"})
    monkeypatch.setattr(Dashboard, "_run_live", lambda self, rd: None)
    tree = worktree.Tree(run_id="20261029-100000", path=str(proj), branch="tenure/x", base_branch="main",
                         base_commit="0" * 40, created="")
    monkeypatch.setattr(worktree, "find", lambda wd, rid: tree)
    with pytest.raises(ApiError, match="반영하거나 버리세요"):
        dispatch(d, "delete_run", {"project": str(proj), "run": "20261029-100000"})
    assert (proj / ".tenure" / "runs" / "20261029-100000").is_dir()


# ------------------------------------------------------------------ 14 실행 전 로그인
def _logins(monkeypatch, **state):
    def fake(agent):
        status, logged = state.get(agent, ("ok", True))
        return doctor.Check(agent, agent.title(), status, extra={"logged_in": logged})
    monkeypatch.setattr(dashmod, "login_check", fake)


def test_preflight_warns_only_for_used_agents(proj, monkeypatch):
    _logins(monkeypatch, codex=("warn", False))
    d = Dashboard()
    out = dispatch(d, "preflight", {"project": str(proj), "workflow": "quick"})
    assert [a["agent"] for a in out["agents"]] == ["claude", "codex"]
    assert len(out["warnings"]) == 1 and "로그인되어 있지 않습니다" in out["warnings"][0]
    dispatch(d, "workflow_save", {"project": str(proj), "id": "solo", "workflow": SOLO})
    assert dispatch(d, "preflight", {"project": str(proj), "workflow": "solo"})["warnings"] == []


def test_preflight_remembers_a_recent_login_stop(proj, monkeypatch):
    _logins(monkeypatch)
    _run(proj, "20261029-100000", status="failed",
         calls=[CallRecord("codex", "review", 3.0, 1, outcome="blocked", reason="로그인 필요")])
    out = dispatch(Dashboard(), "preflight", {"project": str(proj), "workflow": "auto"})
    assert len(out["warnings"]) == 1 and "만료" in out["warnings"][0]
    _run(proj, "20261029-110000", status="done_approved",
         calls=[CallRecord("codex", "review", 3.0, 0, outcome="done")])
    assert dispatch(Dashboard(), "preflight", {"project": str(proj), "workflow": "auto"})["warnings"] == []
