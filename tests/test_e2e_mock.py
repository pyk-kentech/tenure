"""e2e(mock): CLI 진입점부터 전체 흐름. 실제 CLI 는 호출하지 않는다."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

import tenure.orchestrator as orch_mod
from tenure.agents.mock import MockAdapter
from tenure.cli import main
from tenure.state import DONE_APPROVED, DONE_DESIGN, DONE_UNAPPROVED, FAILED, STOPPED_RATE_LIMIT, RunState, list_run_dirs
from tenure.workspace import Workspace
from tests.helpers import file_names


def run_cli(*argv: str) -> int:
    return main(list(argv))


def only_run(workdir: Path) -> Path:
    runs = list_run_dirs(workdir)
    assert len(runs) == 1
    return runs[0]


def test_full_run_approved(tmp_path: Path, capsys):
    wd = tmp_path / "memo"
    assert run_cli("run", "메모 CLI", "--workdir", str(wd), "--mock") == 0
    run_dir = only_run(wd)
    st = RunState.load(run_dir)
    assert st.status == DONE_APPROVED and st.phase == "done"
    assert file_names(run_dir) == [
        "01_propose_claude.md", "02_propose_codex.md",
        "03_r1_critique_claude.md", "04_r1_critique_codex.md",
        "05_synthesize_codex.md",
        "06_impl1_claude.md", "07_review1_codex.md",
        "08_impl2_claude.md", "09_review2_codex.md",
        "10_report_claude.md",                                   # 결과 요약 (DESIGN.md 32장)
    ]
    ws = Workspace(wd)
    assert ws.commit_count() == 3 and not ws.dirty()
    assert (wd / "DESIGN.md").exists() and (wd / "main.py").exists()
    assert ".tenure/" in (wd / ".gitignore").read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "설계안 작성: claude" in out and "판정: APPROVED" in out and "완료:" in out
    assert "실행 시작:" in out and "mock" in out


def test_design_only(tmp_path: Path):
    wd = tmp_path / "x"
    assert run_cli("run", "할일 CLI", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0") == 0
    run_dir = only_run(wd)
    st = RunState.load(run_dir)
    assert st.status == DONE_DESIGN and st.is_done("debate")
    assert file_names(run_dir) == ["01_propose_claude.md", "02_propose_codex.md", "03_synthesize_codex.md"]
    assert (wd / "DESIGN.md").exists() and not (wd / ".git").exists()
    # 끝난 실행은 resume 불가
    assert run_cli("resume", "--workdir", str(wd)) == 2


def test_skip_design(tmp_path: Path):
    wd = tmp_path / "x"
    wd.mkdir()
    # DESIGN.md 없으면 거부
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--skip-design") == 2
    (wd / "DESIGN.md").write_text("# 내 설계\n", encoding="utf-8")
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--skip-design", "--max-review", "1",
                   "--no-arbiter") == 1
    run_dir = only_run(wd)
    st = RunState.load(run_dir)
    assert st.status == DONE_UNAPPROVED
    assert file_names(run_dir) == ["01_impl1_claude.md", "02_review1_codex.md", "03_report_claude.md"]
    assert (wd / "DESIGN.md").read_text(encoding="utf-8") == "# 내 설계\n"


def test_roles_and_toml_precedence(tmp_path: Path):
    wd = tmp_path / "x"
    wd.mkdir()
    (wd / "tenure.toml").write_text('implementer = "codex"\nreviewer = "claude"\nmax_review = 1\n', encoding="utf-8")
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", "--max-review", "2") == 0
    run_dir = only_run(wd)
    st = RunState.load(run_dir)
    assert st.config["implementer"] == "codex" and st.config["reviewer"] == "claude"
    assert st.config["synth"] == "claude" and st.config["max_review"] == 2
    assert file_names(run_dir)[2:] == ["03_synthesize_claude.md", "04_impl1_codex.md", "05_review1_claude.md",
                                       "06_impl2_codex.md", "07_review2_claude.md", "08_report_claude.md"]


def test_task_from_file(tmp_path: Path):
    task_file = tmp_path / "task.md"
    task_file.write_text("# 과제\n긴 설명\n", encoding="utf-8")
    wd = tmp_path / "x"
    assert run_cli("run", f"@{task_file}", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0") == 0
    assert RunState.load(only_run(wd)).task == "# 과제\n긴 설명"
    assert run_cli("run", "@/nonexistent/task.md", "--workdir", str(tmp_path / "y"), "--mock") == 2


def test_workdir_guard(tmp_path: Path, capsys):
    assert run_cli("run", "t", "--workdir", str(Path.home()), "--mock") == 2
    assert "홈 디렉터리" in capsys.readouterr().err
    wd = tmp_path / "dirty"
    ws = Workspace(wd); ws.ensure_repo(); ws.commit_all("init")
    (wd / "wip.txt").write_text("x", encoding="utf-8")
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0") == 2
    assert "커밋되지 않은 변경" in capsys.readouterr().err
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0", "--force") == 0


def test_bad_config_value(tmp_path: Path, capsys):
    assert run_cli("run", "t", "--workdir", str(tmp_path / "x"), "--mock", "--debate-rounds", "9") == 2
    assert "debate_rounds" in capsys.readouterr().err


def test_resume_after_failure(tmp_path: Path, monkeypatch, capsys):
    wd = tmp_path / "x"
    calls = {"n": 0}

    def factory(cfg, *, mock):
        calls["n"] += 1
        if calls["n"] == 1:
            # 첫 실행: codex 의 2번째 호출(= 첫 리뷰)부터 연속 실패
            return {"claude": MockAdapter(label="claude"),
                    "codex": MockAdapter(label="codex", fail_on_calls={3, 4})}
        return {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", approve_on_review=1)}

    monkeypatch.setattr(orch_mod, "make_agents", factory)
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0") == 3
    run_dir = only_run(wd)
    st = RunState.load(run_dir)
    assert st.status == FAILED and st.is_done("impl:1") and not st.is_done("review:1")
    assert "resume" in capsys.readouterr().out
    before = file_names(run_dir)
    assert before[-2:] == ["05_review1_codex_failed.md", "06_review1_codex_failed.md"]

    assert run_cli("resume", "--workdir", str(wd)) == 0
    st = RunState.load(run_dir)
    assert st.status == DONE_APPROVED
    assert file_names(run_dir) == before + ["07_review1_codex.md", "08_report_claude.md"]
    assert Workspace(wd).commit_count() == 2
    assert len(list_run_dirs(wd)) == 1
    out = capsys.readouterr().out
    assert "이어하기:" in out and "완료:" in out


def test_resume_after_rate_limit_keeps_roles(tmp_path: Path, monkeypatch):
    wd = tmp_path / "x"
    first = {"claude": MockAdapter(label="claude", rate_limit_on_calls={1}), "codex": MockAdapter(label="codex")}
    second = {"claude": MockAdapter(label="claude", approve_on_review=1), "codex": MockAdapter(label="codex")}
    it = iter([first, second])
    monkeypatch.setattr(orch_mod, "make_agents", lambda cfg, *, mock: next(it))
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0",
                   "--implementer", "codex", "--reviewer", "claude", "--max-review", "1") == 3
    run_dir = only_run(wd)
    assert RunState.load(run_dir).status == STOPPED_RATE_LIMIT
    assert run_cli("resume", "--workdir", str(wd)) == 0
    assert file_names(run_dir) == ["01_propose_claude_failed.md", "02_propose_claude.md", "03_propose_codex.md",
                                   "04_synthesize_claude.md", "05_impl1_codex.md", "06_review1_claude.md",
                                   "07_report_claude.md"]
    assert [c.step for c in second["claude"].calls] == ["propose", "synthesize", "review"]


def test_resume_without_runs(tmp_path: Path, capsys):
    assert run_cli("resume", "--workdir", str(tmp_path)) == 2
    assert "실행 기록이 없습니다" in capsys.readouterr().err


def test_log_command(tmp_path: Path, capsys):
    wd = tmp_path / "x"
    assert run_cli("log", "--workdir", str(wd)) == 2
    run_cli("run", "메모 CLI", "--workdir", str(wd), "--mock", "--debate-rounds", "0")
    capsys.readouterr()
    assert run_cli("log", "--workdir", str(wd)) == 0
    out = capsys.readouterr().out
    assert "[done_approved]" in out and "과제: 메모 CLI" in out and "마지막 판정: APPROVED" in out
    assert "propose" in out and "review" in out and "호출 8회" in out

    assert run_cli("log", "--workdir", str(wd), "--full") == 0
    out = capsys.readouterr().out
    assert "01_propose_claude.md" in out and "## 결정 사항" in out and "status: APPROVED" in out

    run_id = only_run(wd).name
    assert run_cli("log", "--workdir", str(wd), "--all") == 0
    assert run_id in capsys.readouterr().out
    assert run_cli("log", "--workdir", str(wd), "--run", run_id) == 0
    assert run_cli("log", "--workdir", str(wd), "--run", "nope") == 2


def test_console_entrypoint_subprocess(tmp_path: Path):
    wd = tmp_path / "x"
    r = subprocess.run([sys.executable, "-m", "tenure", "run", "t", "--workdir", str(wd), "--mock",
                        "--design-only", "--debate-rounds", "0"], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr
    assert "설계만 완료" in r.stdout


def test_resume_unapproved_with_higher_max_review(tmp_path: Path, monkeypatch, capsys):
    wd = tmp_path / "x"
    first = {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", approve_on_review=0)}
    second = {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", approve_on_review=1)}
    it = iter([first, second])
    monkeypatch.setattr(orch_mod, "make_agents", lambda cfg, *, mock: next(it))
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", "--max-review", "1",
                   "--no-arbiter") == 1
    run_dir = only_run(wd)
    assert RunState.load(run_dir).status == DONE_UNAPPROVED
    capsys.readouterr()

    # 상한을 안 늘리면 거부 (같거나 작아도 거부)
    assert run_cli("resume", "--workdir", str(wd)) == 2
    assert "--max-review 2" in capsys.readouterr().err
    assert run_cli("resume", "--workdir", str(wd), "--max-review", "1") == 2
    capsys.readouterr()
    # 범위 밖
    assert run_cli("resume", "--workdir", str(wd), "--max-review", "11") == 2
    capsys.readouterr()

    assert run_cli("resume", "--workdir", str(wd), "--max-review", "3") == 0
    out = capsys.readouterr().out
    assert "리뷰 상한 1 → 3회" in out and "[2/3] 리뷰 반영: claude" in out and "완료:" in out
    st = RunState.load(run_dir)
    assert st.status == DONE_APPROVED and st.config["max_review"] == 3
    # 미승인으로 끝날 때 만든 요약(06)은 이어서 리뷰를 더 돈 뒤 끝에서 새로 만든다(09)
    assert file_names(run_dir) == ["01_propose_claude.md", "02_propose_codex.md", "03_synthesize_codex.md",
                                   "04_impl1_claude.md", "05_review1_codex.md", "06_report_claude.md",
                                   "07_impl2_claude.md", "08_review2_codex.md", "09_report_claude.md"]
    assert st.report == "09_report_claude.md"
    assert [c.step for c in second["claude"].calls] == ["fix"]        # 구현이 아니라 리뷰 반영
    assert "## 반드시 고칠 것" in second["claude"].calls[0].prompt      # 이전 리뷰가 프롬프트에 들어감
    assert Workspace(wd).commit_count() == 3
    assert len(list_run_dirs(wd)) == 1

    # 승인된 실행은 상한을 늘려도 resume 불가
    assert run_cli("resume", "--workdir", str(wd), "--max-review", "5") == 2
    assert "이미 끝났습니다" in capsys.readouterr().err


def test_resume_running_run_can_raise_max_review(tmp_path: Path, monkeypatch):
    wd = tmp_path / "x"
    first = {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", fail_on_calls={3, 4})}
    second = {"claude": MockAdapter(label="claude"), "codex": MockAdapter(label="codex", approve_on_review=2)}
    it = iter([first, second])
    monkeypatch.setattr(orch_mod, "make_agents", lambda cfg, *, mock: next(it))
    assert run_cli("run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", "--max-review", "1") == 3
    assert run_cli("resume", "--workdir", str(wd), "--max-review", "2") == 0
    assert RunState.load(only_run(wd)).config["max_review"] == 2
