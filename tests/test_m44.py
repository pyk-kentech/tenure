"""M44 워크플로 노드 편집기 (DESIGN.md 36.29): 정의 읽기·검사·저장 API 와 TOML 쓰기."""

from __future__ import annotations

import tomllib
from importlib import resources

import pytest

from tenure.config import Config
from tenure.registry import Registry
from tenure.ui.dashboard import ApiError, Dashboard, dispatch
from tenure.workflow import load_workflow
from tenure.workflow_toml import dump_workflow


@pytest.fixture
def proj(tmp_path):
    wd = tmp_path / "proj"
    wd.mkdir()
    Registry().add(wd)
    return wd


SECURE = {
    "name": "내 보안",
    "description": "토론 → 구현 + 보안 리뷰 → 감사",
    "roles": {
        "claude": {"agent": "claude", "model": "opus", "instructions": "줄 \"따옴표\"\n두 줄"},
        "codex": {"agent": "codex"},
        "security": {"agent": "codex", "templates": {"review": "review_security"}},
    },
    "steps": [
        {"kind": "debate", "proposers": ["claude", "codex"], "synth": "codex"},
        {"kind": "build", "implementer": "claude", "reviewers": ["codex", "security"], "max_review": 2},
        {"kind": "audit", "roles": ["claude"]},
    ],
}


def test_dump_round_trips_every_builtin_workflow():
    for ref in resources.files("tenure").joinpath("workflows").iterdir():
        if not ref.name.endswith(".toml"):
            continue
        data = tomllib.loads(ref.read_text(encoding="utf-8"))
        assert tomllib.loads(dump_workflow(data)) == data, ref.name


def test_dump_escapes_strings():
    text = dump_workflow(SECURE)
    assert tomllib.loads(text)["roles"]["claude"]["instructions"] == "줄 \"따옴표\"\n두 줄"
    assert '"내 보안"' in text and "[[steps]]" in text


def test_get_builtin_and_classic(proj):
    d = Dashboard()
    out = dispatch(d, "workflow_get", {"project": str(proj), "name": "secure"})
    assert out["source"] == "builtin"
    assert [s["kind"] for s in out["definition"]["steps"]] == ["debate", "build", "audit"]
    assert dispatch(d, "workflow_get", {"project": str(proj), "name": "classic"})["source"] == "config"
    with pytest.raises(ApiError) as e:
        dispatch(d, "workflow_get", {"project": str(proj), "name": "없는것"})
    assert e.value.status == 400


def test_check_reports_engine_errors(proj):
    d = Dashboard()
    ok = dispatch(d, "workflow_check", {"project": str(proj), "workflow": SECURE})
    assert ok["ok"] and "debate" in ok["summary"]
    bad = {**SECURE, "steps": [{"kind": "build", "implementer": "claude", "reviewers": ["claude"]}]}
    out = dispatch(d, "workflow_check", {"project": str(proj), "workflow": bad})
    assert not out["ok"] and "리뷰어가 될 수 없습니다" in out["error"]


def test_save_to_project_then_load_runs_it(proj):
    d = Dashboard()
    out = dispatch(d, "workflow_save", {"project": str(proj), "name": "my-secure", "workflow": SECURE})
    path = proj / ".tenure" / "workflows" / "my-secure.toml"
    assert out["path"] == str(path) and path.is_file()
    wf = load_workflow("my-secure", proj, Config())
    assert wf.source == "project" and wf.roles["claude"].model == "opus"
    with pytest.raises(ApiError) as e:
        dispatch(d, "workflow_save", {"project": str(proj), "name": "my-secure", "workflow": SECURE})
    assert e.value.status == 409
    dispatch(d, "workflow_save", {"project": str(proj), "name": "my-secure", "workflow": SECURE, "overwrite": True})


def test_save_rejects_bad_names_and_invalid_workflows(proj):
    d = Dashboard()
    for name in ("classic", "Big", "../x", ""):
        with pytest.raises(ApiError):
            dispatch(d, "workflow_save", {"project": str(proj), "name": name, "workflow": SECURE})
    bad = {**SECURE, "steps": [{"kind": "audit", "roles": ["claude"]}]}
    with pytest.raises(ApiError) as e:
        dispatch(d, "workflow_save", {"project": str(proj), "name": "x", "workflow": bad})
    assert "audit" in e.value.message
    assert not (proj / ".tenure" / "workflows" / "x.toml").exists()


def test_save_to_user_scope(proj, tmp_path):
    d = Dashboard()
    out = dispatch(d, "workflow_save", {"name": "mine", "scope": "user", "workflow": SECURE})
    assert out["scope"] == "user" and out["path"].endswith("mine.toml")
    assert load_workflow("mine", None, Config()).source == "user"
