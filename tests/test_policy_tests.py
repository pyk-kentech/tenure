"""심각도 기준 판정, 수렴 실패 감지, tenure 가 직접 돌리는 테스트."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tenure.agents.mock import MockAdapter
from tenure.cli import main
from tenure.phases.build import BuildPhase, extract_section
from tenure.policy import blocking_count, decide, stalled
from tenure.state import DONE_APPROVED, DONE_UNAPPROVED, RunState, list_run_dirs
from tenure.testing import detect_command, resolve_command, run_tests
from tenure.verdict import Status, parse_verdict
from tenure.workspace import Workspace
from tenure.procs import PYTHON_CMD
from tests.helpers import echo_exit, py_cmd
from tests.helpers import file_names, make_ctx

import tenure.orchestrator as orch_mod


def vblock(status="CHANGES_REQUESTED", must=None, p1=None, p2=None, p3=None):
    lines = [f"status: {status}"]
    if must is not None:
        lines.append(f"must_fix: {must}")
    for k, v in (("p1", p1), ("p2", p2), ("p3", p3)):
        if v is not None:
            lines.append(f"{k}: {v}")
    return "## 반드시 고칠 것\n- [P2] 무언가\n\n<verdict>\n" + "\n".join(lines) + "\n</verdict>"


# ====================================================================== 판정
def test_verdict_severity_parsing():
    v = parse_verdict("<verdict>\n**status:** CHANGES_REQUESTED\n- must_fix: 1\n**P1:** 0\n* p2: 1\np3: `4`\n</verdict>")
    assert v.severity == {"P1": 0, "P2": 1, "P3": 4} and v.must_fix == 1
    assert parse_verdict(vblock(must=2)).severity is None


@pytest.mark.parametrize("text,sev,tests_block,approved", [
    (vblock("CHANGES_REQUESTED", 0, 0, 0, 3), "P2", False, True),    # P3 만 → 승인
    (vblock("CHANGES_REQUESTED", 0, 0, 0, 3), "P3", False, False),   # 기준 P3 → 반려
    (vblock("APPROVED", 0, 0, 1, 0), "P2", False, False),            # 리뷰어 승인이어도 P2 남음 → 반려
    (vblock("CHANGES_REQUESTED", 1, 0, 1, 0), "P1", False, True),    # 기준 P1 → P2 무시
    (vblock("APPROVED", 0, 0, 0, 0), "P2", True, False),             # 테스트 실패 → 보류
    (vblock("APPROVED", 0), "P2", False, True),                      # 심각도 없음 → 예전 규칙
    (vblock("CHANGES_REQUESTED", 2), "P2", False, False),
    ("판정 없음", "P2", False, False),
])
def test_decide(text, sev, tests_block, approved):
    d = decide(parse_verdict(text), block_severity=sev, tests_block=tests_block, require_tests_pass=True)
    assert d.approved is approved, d.describe()


def test_decide_tests_not_required():
    d = decide(parse_verdict(vblock("APPROVED", 0)), block_severity="P2", tests_block=True, require_tests_pass=False)
    assert d.approved


def test_blocking_count():
    v = parse_verdict(vblock("CHANGES_REQUESTED", 3, 1, 2, 5))
    assert [blocking_count(v, s) for s in ("P1", "P2", "P3")] == [1, 3, 8]


@pytest.mark.parametrize("series,n,expected", [
    ([3, 1, 2, 1], 2, True),     # memo-cli 에서 본 패턴
    ([3, 2, 1], 2, False),
    ([3, 1, 2], 2, False),
    ([2, 2, 2], 2, True),
    ([2, 2], 2, False),          # 비교할 이전 기록이 부족
    ([5, 4, 4, 4], 0, False),    # 끔
    ([None, 3, None, 3, 3], 2, True),
])
def test_stalled(series, n, expected):
    assert stalled(series, n) is expected


def test_extract_section():
    text = "## 반드시 고칠 것 (P1·P2)\n- [P2] a\n- [P1] b\n\n## 고치면 좋은 것\n- c"
    assert extract_section(text, "반드시 고칠 것") == "- [P2] a\n- [P1] b"
    assert extract_section("없음", "반드시 고칠 것") == ""


# ====================================================================== 테스트 명령 결정·실행
def test_detect_command(tmp_path: Path):
    assert detect_command(tmp_path) is None
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert detect_command(tmp_path) is None            # 테스트가 없으면 안 돌린다
    (tmp_path / "tests").mkdir()
    assert detect_command(tmp_path) in (f"{PYTHON_CMD} -m pytest -q", "uv run pytest -q")
    node = tmp_path / "n"; node.mkdir()
    (node / "package.json").write_text(json.dumps({"scripts": {"test": 'echo "Error: no test specified" && exit 1'}}))
    assert detect_command(node) is None
    (node / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    assert detect_command(node) == "npm test --silent"
    mk = tmp_path / "m"; mk.mkdir()
    (mk / "Makefile").write_text("build:\n\techo\ntest:\n\techo ok\n")
    assert detect_command(mk) == "make test"


def test_resolve_command_precedence(tmp_path: Path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "setup.py").write_text("")
    assert resolve_command(tmp_path, "auto")[1] == "자동 감지"
    (tmp_path / "tenure.toml").write_text('test_command = "make check"\n')
    assert resolve_command(tmp_path, "auto") == ("make check", "tenure.toml")
    assert resolve_command(tmp_path, "pytest -x") == ("pytest -x", "설정")
    assert resolve_command(tmp_path, "none") is None
    (tmp_path / "tenure.toml").write_text('test_command = "none"\n')
    assert resolve_command(tmp_path, "auto") is None


@pytest.mark.parametrize("cmd,status", [
    (echo_exit("3 passed in 0.1s", 0), "passed"),
    (echo_exit("FAILED x", 1), "failed"),
    ("no-such-test-runner-xyz", "error"),
    (echo_exit("no tests ran", 5) + " pytest", "error"),
])
def test_run_tests_status(tmp_path: Path, cmd, status):
    r = run_tests(tmp_path, cmd, "설정", 30)
    assert r.status == status
    assert r.blocks_approval is (status == "failed")


def test_run_tests_timeout(tmp_path: Path):
    r = run_tests(tmp_path, "sleep 10", "설정", 1)
    assert r.status == "timeout" and r.blocks_approval


# ====================================================================== build 단계
def approve_reviewer(prompts=None):
    def rev(req, n):
        if prompts is not None:
            prompts.append(req.prompt)
        return vblock("APPROVED", 0, 0, 0, 0)
    return MockAdapter(label="codex", responder=rev)


def prep(tmp_path, agents, **overrides):
    ctx = make_ctx(tmp_path, agents=agents, overrides=overrides)
    (tmp_path / "DESIGN.md").write_text("# d\n", encoding="utf-8")
    return ctx


def test_tests_run_and_reach_reviewer(tmp_path: Path):
    prompts = []
    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"), "codex": approve_reviewer(prompts)},
               test_command=echo_exit("7 passed in 0.2s", 0))
    assert BuildPhase(ctx).run()
    assert file_names(ctx.run_dir) == ["01_impl1_claude.md", "02_test1.md", "03_review1_codex.md"]
    t = ctx.state.tests["1"]
    assert t["status"] == "passed" and t["summary"] == "7 passed in 0.2s" and t["file"] == "02_test1.md"
    assert "[tenure 가 직접 실행한 테스트]" in prompts[0] and "7 passed" in prompts[0] and "통과" in prompts[0]
    assert "(첫 리뷰)" in prompts[0]
    head = (ctx.run_dir / "02_test1.md").read_text(encoding="utf-8")
    assert "agent: tenure" in head and "test_status: passed" in head
    assert ctx.state.review_history[0]["tests"] == "passed"


def test_failing_tests_block_approval_and_reach_fixer(tmp_path: Path):
    fixer_prompts = []

    def impl(req, n):
        if req.step == "fix":
            fixer_prompts.append(req.prompt)
        (req.cwd / "main.py").write_text(f"v{n}\n", encoding="utf-8")
        return "done"

    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude", responder=impl), "codex": approve_reviewer()},
               test_command=echo_exit("AssertionError: boom", 1), max_review=2, stall_rounds=0)
    assert BuildPhase(ctx).run() is False
    st = ctx.state
    assert st.status == DONE_UNAPPROVED and st.stop_reason == "max_review"
    assert all(h["reviewer_status"] == "APPROVED" and not h["approved"] for h in st.review_history)
    assert "테스트 실패로 승인 보류" in st.review_history[0]["reasons"]
    assert "AssertionError: boom" in fixer_prompts[0] and "실패" in fixer_prompts[0]
    assert any("테스트 실패" in m for m in ctx.logs)


def test_test_artifacts_not_committed(tmp_path: Path):
    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"), "codex": approve_reviewer()},
               test_command="mkdir -p .cache && touch junk.txt .cache/x && echo ok")
    assert BuildPhase(ctx).run()
    ws = Workspace(tmp_path)
    assert not (tmp_path / "junk.txt").exists() and not ws.dirty()
    assert ws.commit_count() == 2   # design + impl1 (테스트 부산물 보존 커밋 없음)
    assert "junk.txt" not in ws.git("ls-files")


def test_implementer_declares_test_command_in_duet_toml(tmp_path: Path):
    def impl(req, n):
        assert "test_command" in req.prompt   # 구현 프롬프트가 안내한다
        (req.cwd / "tenure.toml").write_text('test_command = "echo from-tenure-toml"\n', encoding="utf-8")
        return "done"

    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude", responder=impl), "codex": approve_reviewer()})
    assert BuildPhase(ctx).run()
    assert ctx.state.tests["1"]["command"] == "echo from-tenure-toml" and ctx.state.tests["1"]["source"] == "tenure.toml"


def test_no_test_command_skips(tmp_path: Path):
    prompts = []
    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"), "codex": approve_reviewer(prompts)})
    assert BuildPhase(ctx).run()
    assert ctx.state.tests == {} and "테스트 명령이 없어" in prompts[0]
    assert ctx.state.is_done("test:1")


def test_severity_threshold_approves_p3_only(tmp_path: Path):
    rev = MockAdapter(label="codex", responder=lambda req, n: vblock("CHANGES_REQUESTED", 2, 0, 0, 2))
    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"), "codex": rev})
    assert BuildPhase(ctx).run() is True
    h = ctx.state.review_history[0]
    assert h["approved"] and h["reviewer_status"] == "CHANGES_REQUESTED" and h["blocking"] == 0
    assert ctx.state.last_verdict == "APPROVED"
    ctx2 = prep(tmp_path / "b", {"claude": MockAdapter(label="claude"),
                                 "codex": MockAdapter(label="codex", responder=lambda req, n: vblock("CHANGES_REQUESTED", 2, 0, 0, 2))},
                block_severity="P3", max_review=1)
    assert BuildPhase(ctx2).run() is False


def test_previous_review_shown_to_reviewer(tmp_path: Path):
    prompts = []

    def rev(req, n):
        prompts.append(req.prompt)
        if n == 1:
            return "## 반드시 고칠 것 (P1·P2)\n- [P2] 고유한-이전-지적\n\n## 고치면 좋은 것\n- 사소함\n" + vblock("CHANGES_REQUESTED", 1, 0, 1, 0)[-80:]
        return vblock("APPROVED", 0, 0, 0, 0)

    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", responder=rev)})
    assert BuildPhase(ctx).run()
    assert "고유한-이전-지적" in prompts[1] and "사소함" not in prompts[1].split("[구현 전체 diff]")[0]


def test_stall_stops_and_resume_continues(tmp_path: Path, monkeypatch, capsys):
    wd = tmp_path / "x"
    series = iter([3, 1, 2, 1, 0])

    def make(cfg, *, mock):
        def rev(req, n):
            if req.step != "review":
                return "## 결정 사항\n- x"
            b = next(series)
            return vblock("CHANGES_REQUESTED" if b else "APPROVED", b, 0, b, 0)
        return {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", responder=rev)}

    monkeypatch.setattr(orch_mod, "make_agents", make)
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", "--max-review", "8"]) == 1
    out = capsys.readouterr().out
    assert "수렴하지 않음" in out and "--max-review" not in out.split("계속하려면")[-1]
    st = RunState.load(list_run_dirs(wd)[-1])
    assert st.stop_reason == "stalled" and st.round == 4 and st.status == DONE_UNAPPROVED
    assert [h["blocking"] for h in st.review_history] == [3, 1, 2, 1]
    # 상한(8)이 남아 있으므로 --max-review 없이 이어갈 수 있다. 수렴 판단은 이어간 지점부터 다시 센다.
    assert main(["resume", "--workdir", str(wd)]) == 0
    st = RunState.load(list_run_dirs(wd)[-1])
    assert st.status == DONE_APPROVED and st.stall_baseline_round == 4 and st.stop_reason is None


def test_cli_policy_flags_saved(tmp_path: Path):
    wd = tmp_path / "x"
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0",
                 "--test-command", "none", "--block-severity", "P1", "--stall-rounds", "0"]) == 0
    c = RunState.load(list_run_dirs(wd)[-1]).config
    assert (c["test_command"], c["block_severity"], c["stall_rounds"], c["require_tests_pass"]) == ("none", "P1", 0, True)
    with pytest.raises(SystemExit):
        main(["run", "t", "--workdir", str(tmp_path / "y"), "--mock", "--block-severity", "P9"])


def test_resume_old_run_does_not_retest_reviewed_rounds(tmp_path: Path):
    """테스트 단계가 없던 예전 실행을 이어가면, 이미 리뷰가 끝난 라운드는 테스트하지 않는다."""
    ctx = prep(tmp_path, {"claude": MockAdapter(label="claude"),
                          "codex": MockAdapter(label="codex", responder=lambda r, n: vblock("APPROVED", 0, 0, 0, 0))},
               test_command="echo ok", max_review=3)
    ws = Workspace(tmp_path)
    ws.ensure_repo()
    st = ctx.state
    st.base_commit = ws.commit_all("tenure: design")
    (tmp_path / "main.py").write_text("v1\n", encoding="utf-8")
    ws.commit_all("tenure: impl round 1")
    for k in ("build:base", "impl:1:called", "impl:1", "review:1"):   # 예전 실행: test:1 키가 없음
        st.mark_done(k)
    st.last_review = "x.md"
    ctx.transcript.save("review1_codex", "## 반드시 고칠 것\n- a\n", {"step": "review", "agent": "codex", "ok": True, "exit_code": 0})
    st.last_review = "01_review1_codex.md"
    assert BuildPhase(ctx).run()
    assert "1" not in st.tests and st.tests["2"]["status"] == "passed"


def test_test_summary_prefers_stdout(tmp_path: Path):
    r = run_tests(tmp_path, py_cmd("import sys; print('95 passed in 5.9s'); "
                                   "print('Installed 1 package in 2ms', file=sys.stderr)"), "설정", 30)
    assert r.summary == "95 passed in 5.9s" and r.output_tail.endswith("95 passed in 5.9s")
    r = run_tests(tmp_path, echo_exit("only stderr", 1, stderr=True), "설정", 30)
    assert r.summary == "only stderr"
