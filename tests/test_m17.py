"""M17: 분쟁 중재자 (DESIGN.md 23장)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import tenure.orchestrator as orch_mod
from tenure import arbiter as arb
from tenure.agents.mock import MockAdapter
from tenure.cli import main
from tenure.config import Config
from tenure.state import RunState, list_run_dirs
from tenure.workflow import WorkflowError, parse_workflow
from tests.helpers import FAIL_CMD, file_names


def item(i, sev="P2", status="open", role="codex"):
    return {"id": i, "role": role, "round": 1, "severity": sev, "title": f"t{i}", "file": "a.py",
            "status": status, "closed_round": None, "note": ""}


class S:
    def __init__(self, issues):
        self.issues = issues


# ====================================================================== 모듈 단위
def test_normalize_defaults_to_uphold():
    items = [item("I1"), item("I2"), item("I3")]
    d = arb.normalize({"decisions": [{"id": "I1", "decision": "reject", "reason": "틀림"},
                                     {"id": "I9", "decision": "downgrade", "reason": "x"},
                                     {"id": "I2", "decision": "maybe", "reason": "x"}], "summary": ""}, items)
    assert d["I1"] == {"decision": "reject", "reason": "틀림"}
    assert d["I2"]["decision"] == "uphold" and d["I3"]["decision"] == "uphold" and "I9" not in d
    assert arb.normalize({"summary": "x"}, items) is None and arb.normalize("x", items) is None


def test_apply_and_blocking():
    st = S([item("I1"), item("I2"), item("I3", sev="P1"), item("I4", status="resolved")])
    res = arb.apply(st, {"I1": {"decision": "downgrade", "reason": "밖"}, "I2": {"decision": "reject", "reason": "틀림"},
                         "I3": {"decision": "uphold", "reason": "맞음"}, "I4": {"decision": "reject", "reason": ""}}, 3)
    assert res == {"uphold": ["I3"], "downgrade": ["I1"], "reject": ["I2"]}
    by = {x["id"]: x for x in st.issues}
    assert by["I1"]["severity"] == "P3" and by["I1"]["status"] == "open" and "완화" in by["I1"]["note"]
    assert by["I2"]["status"] == "rejected" and by["I2"]["closed_round"] == 3
    assert by["I4"]["status"] == "resolved"                       # 닫힌 지적은 건드리지 않음
    assert arb.blocking_ids(st, "P2") == ["I3"] and arb.blocking_ids(st, "P1") == ["I3"]


def test_render_and_format():
    items = [item("I1")]
    md = arb.render({"I1": {"decision": "downgrade", "reason": "요구 밖"}}, items, "정리")
    assert "- I1 [P2] tI1 → **완화(P3)**: 요구 밖" in md and "## 요약\n정리" in md
    assert arb.format_issues(items) == "- I1 [P2] tI1 — `a.py` (리뷰어 codex, 1라운드부터 열림)"


def test_schema_strict_compatible():
    node = arb.ARBITER_SCHEMA
    assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
    it = node["properties"]["decisions"]["items"]
    assert it["additionalProperties"] is False and set(it["required"]) == set(it["properties"])


def test_workflow_arbiter_key():
    roles = {"a": {"agent": "claude"}, "b": {"agent": "codex"}, "c": {"agent": "claude"}}
    wf = parse_workflow({"roles": roles, "steps": [{"kind": "build", "implementer": "a", "reviewers": ["b"],
                                                    "arbiter": "c"}]}, name_hint="t", source="file", where="t")
    assert wf.steps[0].arbiter == "c" and "c" in wf.used_roles() and "중재 c" in wf.summary()
    with pytest.raises(WorkflowError, match="중재자"):
        parse_workflow({"roles": roles, "steps": [{"kind": "build", "implementer": "a", "reviewers": ["b"],
                                                   "arbiter": "a"}]}, name_hint="t", source="file", where="t")


def test_defaults():
    c = Config()
    assert c.arbiter is True and c.codex.step_effort["arbitrate"] == "high"


# ====================================================================== 빌드 통합 (mock, classic: 중재자 = synth = codex)
def finding(fid="", sev="P2", file="main.py", title="예외 처리 없음"):
    return {"id": fid, "severity": sev, "new": not fid, "title": title, "file": file, "detail": "d",
            "evidence": f"{file}:1 근거"}


def verdict(*fs):
    return {"status": "CHANGES_REQUESTED" if fs else "APPROVED", "summary": "", "findings": list(fs), "repro": []}


def scripted(reviews, arbitration=None):
    st = {"n": 0}

    def responder(req, n):
        if req.step == "review":
            st["n"] += 1
            return json.dumps(reviews[min(st["n"], len(reviews)) - 1], ensure_ascii=False)
        if req.step == "arbitrate":
            if arbitration is None:
                return "읽을 수 없는 답"
            return json.dumps(arbitration, ensure_ascii=False)
        return MockAdapter(label="codex")._default_response(req)
    return responder


def run(tmp_path, monkeypatch, responder, *extra, toml=""):
    seen: dict = {}

    def factory(cfg, *, mock):
        seen["claude"] = MockAdapter(label="claude")
        seen["codex"] = MockAdapter(label="codex", responder=responder)
        return dict(seen)

    monkeypatch.setattr(orch_mod, "make_agents", factory)
    wd = tmp_path / "w"
    if toml:
        wd.mkdir(parents=True)
        (wd / "tenure.toml").write_text(toml, encoding="utf-8")
    rc = main(["run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", *extra])
    [rd] = list_run_dirs(wd)
    return rc, seen, RunState.load(rd), rd


STUCK = [verdict(finding()), verdict(finding("I1")), verdict(finding("I1")), verdict(finding("I1"))]


def test_stall_then_arbiter_approves(tmp_path, monkeypatch, capsys):
    rc, seen, st, rd = run(tmp_path, monkeypatch, scripted(
        STUCK, {"decisions": [{"id": "I1", "decision": "downgrade", "reason": "요구 밖"}], "summary": "받아들임"}),
        "--max-review", "5")
    assert rc == 0 and st.status == "done_approved" and st.stop_reason == "arbiter"
    assert st.arbiter["result"] == "approved" and st.arbiter["downgraded"] == ["I1"] and st.arbiter["round"] == 3
    assert [x["severity"] for x in st.issues] == ["P3"]
    names = file_names(rd)
    assert names[-2].endswith("_arbiter3_codex.md") and names[-1].endswith("_report_claude.md")
    assert len([n for n in names if "review" in n]) == 3
    arb_call = [c for c in seen["codex"].calls if c.step == "arbitrate"][0]
    assert "- I1 [P2] 예외 처리 없음" in arb_call.prompt and arb_call.output_schema == arb.ARBITER_SCHEMA
    assert "막는 지적 수가 줄지 않음" in arb_call.prompt
    assert "완료: 중재로 승인" in capsys.readouterr().out


def test_arbiter_upholds_then_final_round_approves(tmp_path, monkeypatch):
    reviews = STUCK[:3] + [verdict(finding(title="중재 뒤 새 지적", file="other.py"))]   # I1 해결, 새 P2 는 범위 밖
    rc, seen, st, rd = run(tmp_path, monkeypatch, scripted(
        reviews, {"decisions": [{"id": "I1", "decision": "uphold", "reason": "요구 위반"}], "summary": "I1 만 고쳐라"}),
        "--max-review", "5")
    assert rc == 0 and st.stop_reason is None and len(st.review_history) == 4
    assert st.arbiter["result"] == "continue" and st.arbiter["upheld"] == ["I1"]
    fix = [c for c in seen["claude"].calls if c.step == "fix"][-1]
    assert "중재 결과 (이번이 마지막 라운드)" in fix.prompt and "유지된 지적(I1)만 고쳐라" in fix.prompt
    last_review = [c for c in seen["codex"].calls if c.step == "review"][-1]
    assert "(중재 결과: 아래 중 I1 만 막는 기준이다" in last_review.prompt
    assert st.review_history[-1]["downgraded"]["arbiter"] == 1


def test_final_round_still_blocked_stops(tmp_path, monkeypatch, capsys):
    rc, _, st, _ = run(tmp_path, monkeypatch, scripted(
        STUCK, {"decisions": [{"id": "I1", "decision": "uphold", "reason": "요구 위반"}], "summary": ""}),
        "--max-review", "5")
    assert rc == 1 and st.stop_reason == "arbiter_final" and len(st.review_history) == 4
    assert st.arbiter["upheld"] == ["I1"]
    assert "중재 뒤에도 막는 지적이 남아" in capsys.readouterr().out
    # 수렴 실패처럼 --max-review 없이 이어갈 수 있고, 중재는 다시 부르지 않는다
    assert main(["resume", "--workdir", str(tmp_path / "w")]) in (0, 1)
    st2 = RunState.load(list_run_dirs(tmp_path / "w")[0])
    assert st2.arbiter["round"] == 3 and len(st2.review_history) >= 5


def test_max_review_arbiter_no_extra_round(tmp_path, monkeypatch):
    rc, seen, st, rd = run(tmp_path, monkeypatch, scripted(
        STUCK, {"decisions": [{"id": "I1", "decision": "uphold", "reason": "요구 위반"}], "summary": ""}),
        "--max-review", "2")
    assert rc == 1 and st.stop_reason == "arbiter_upheld" and len(st.review_history) == 2
    assert st.arbiter["reason"] == "max_review" and st.arbiter["result"] == "stop"
    assert "리뷰 상한에 닿음" in [c for c in seen["codex"].calls if c.step == "arbitrate"][0].prompt


def test_max_review_arbiter_can_approve(tmp_path, monkeypatch):
    rc, _, st, _ = run(tmp_path, monkeypatch, scripted(
        STUCK, {"decisions": [{"id": "I1", "decision": "reject", "reason": "근거 틀림"}], "summary": ""}),
        "--max-review", "1")
    assert rc == 0 and st.stop_reason == "arbiter" and st.issues[0]["status"] == "rejected"


def test_failing_tests_block_arbiter_approval(tmp_path, monkeypatch):
    rc, _, st, _ = run(tmp_path, monkeypatch, scripted(
        STUCK, {"decisions": [{"id": "I1", "decision": "downgrade", "reason": "밖"}], "summary": ""}),
        "--max-review", "1", toml=f"test_command = '{FAIL_CMD}'\n")
    assert rc == 1 and st.stop_reason == "arbiter_upheld" and st.arbiter["upheld"] == []
    assert st.tests["1"]["status"] == "failed"


def test_unreadable_arbitration_stops_as_before(tmp_path, monkeypatch):
    rc, _, st, _ = run(tmp_path, monkeypatch, scripted(STUCK, None), "--max-review", "5")
    assert rc == 1 and st.stop_reason == "stalled" and st.arbiter["result"] == "unreadable"


def test_no_arbiter_flag_and_no_ledger(tmp_path, monkeypatch):
    rc, seen, st, rd = run(tmp_path, monkeypatch, scripted(STUCK, {"decisions": [], "summary": ""}),
                           "--max-review", "5", "--no-arbiter")
    assert rc == 1 and st.stop_reason == "stalled" and st.arbiter is None
    assert not any("arbiter" in n for n in file_names(rd)) and st.config["arbiter"] is False
    rc, _, st, _ = run(tmp_path / "b", monkeypatch, scripted(STUCK, {"decisions": [], "summary": ""}),
                       "--max-review", "5", toml="review_gates = false\n")
    assert st.stop_reason == "stalled" and st.arbiter is None                # 원장이 없으면 중재 없음


def test_workflow_explicit_arbiter_role(tmp_path, monkeypatch):
    wd = tmp_path / "w"
    (wd / ".tenure" / "workflows").mkdir(parents=True)
    (wd / ".tenure" / "workflows" / "j.toml").write_text(
        '[roles.dev]\nagent = "claude"\n[roles.rev]\nagent = "codex"\n[roles.judge]\nagent = "claude"\n'
        '[[steps]]\nkind = "debate"\nproposers = ["dev"]\n'
        '[[steps]]\nkind = "build"\nimplementer = "dev"\nreviewers = ["rev"]\narbiter = "judge"\n', encoding="utf-8")
    store: dict = {}

    def factory(cfg, wf, *, mock):
        agents = {n: MockAdapter(label=n) for n in wf.used_roles()}
        agents["rev"] = MockAdapter(label="rev", responder=scripted(STUCK))
        agents["judge"] = MockAdapter(label="judge", responder=scripted(
            [], {"decisions": [{"id": "I1", "decision": "downgrade", "reason": "밖"}], "summary": ""}))
        store["a"] = agents
        return agents

    monkeypatch.setattr(orch_mod, "make_role_agents", factory)
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--workflow", "j", "--max-review", "5"]) == 0
    assert [c.step for c in store["a"]["judge"].calls] == ["arbitrate"]
    st = RunState.load(list_run_dirs(wd)[0])
    assert st.stop_reason == "arbiter" and file_names(list_run_dirs(wd)[0])[-2].endswith("_arbiter3_judge.md")


def test_bench_summary_counts_arbiter():
    from tenure.bench import summarize

    rows = [{"task": "x", "workflow": "classic", "rep": 1, "status": "approved", "approved": True, "arbiter": "arbiter",
             "rounds": 3, "calls": 9, "usage": {}, "hidden": {"passed": 2, "total": 2, "all_passed": True}, "seconds": 5}]
    out = summarize({"meta": {"suite": "s", "tasks": ["x"], "repeat": 1}, "rows": rows})
    assert "| 중재 승인 |" in out and "| classic | 1 | 1/1 | 1 |" in out
