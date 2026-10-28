"""실제 실행에서 드러났거나 점검으로 찾은 버그들의 회귀 테스트."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tenure import stopflag
from tenure.agents.base import Mode, AgentRequest, classify_rate_limit, compile_rate_limit_patterns
from tenure.agents.claude_code import ClaudeCodeAdapter
from tenure.agents.codex import CodexAdapter
from tenure.cli import main
from tenure.config import DEFAULT_RATE_LIMIT_PATTERNS, Config
from tenure.lock import LockBusy, RunLock, lock_path, read_lock
from tenure.runner import pid_alive, run_process
from tenure.state import DONE_APPROVED, DONE_DESIGN, RUNNING, STOPPED_USER, RunState, list_run_dirs
from tenure.workspace import Workspace, WorkdirRefused, check_workdir

PATTERNS = compile_rate_limit_patterns(DEFAULT_RATE_LIMIT_PATTERNS)
LONG_REVIEW_MENTIONING_LIMITS = (
    "## 반드시 고칠 것\n(없음)\n\n## 고치면 좋은 것\n"
    "- API 가 429 Too Many Requests 를 돌려주면 rate limit 처리를 하도록 재시도 로직을 넣으면 좋겠습니다.\n"
    "- usage limit 문서 링크를 README 에 추가하세요.\n" + ("설명 " * 200) +
    "\n<verdict>\nstatus: APPROVED\nmust_fix: 0\n</verdict>"
)


# ====================================================================== rate limit 오탐
def test_classify_rate_limit_rules():
    # 성공 + 긴 본문이 한도를 '언급'만 함 → 한도 아님
    assert not classify_rate_limit(failed=False, text=LONG_REVIEW_MENTIONING_LIMITS, diagnostics=[], patterns=PATTERNS)
    # 성공 + 짧은 안내문 자체 → 한도
    assert classify_rate_limit(failed=False, text="You've hit your usage limit · resets 3pm",
                               diagnostics=[], patterns=PATTERNS)
    # 실패 + stderr 에 한도 문구 → 한도
    assert classify_rate_limit(failed=True, text="", diagnostics=["Error: rate limit exceeded"], patterns=PATTERNS)
    # 실패했지만 다른 이유 → 한도 아님
    assert not classify_rate_limit(failed=True, text="", diagnostics=["segfault"], patterns=PATTERNS)
    # 실패 + stdout 앞부분(대화 기록)에만 언급, 끝부분은 무관 → 한도 아님
    noisy = "model said: handle rate limit\n" + ("x" * 5000) + "\nfatal: crashed"
    assert not classify_rate_limit(failed=True, text="", diagnostics=[noisy], patterns=PATTERNS)


@pytest.mark.parametrize("adapter_cls", [ClaudeCodeAdapter, CodexAdapter])
def test_adapters_do_not_stop_on_review_that_mentions_limits(adapter_cls, fake_cfg: Config, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TENURE_FAKE_TEXT", LONG_REVIEW_MENTIONING_LIMITS)
    r = adapter_cls(fake_cfg).ask(AgentRequest("리뷰", tmp_path, Mode.READ, 20, "review"))
    assert r.ok and not r.rate_limited
    assert "429" in r.text


# ====================================================================== 손자 프로세스 정리
GRANDCHILD_SCRIPT = r"""
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(p.pid))
time.sleep(60)
"""


def test_timeout_kills_grandchildren(tmp_path: Path):
    pid_file = tmp_path / "gc.pid"
    r = run_process([sys.executable, "-c", GRANDCHILD_SCRIPT, str(pid_file)], cwd=tmp_path, timeout_sec=1.5)
    assert r.timed_out
    gc = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while pid_alive(gc) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(gc), "타임아웃 후 손자 프로세스가 살아 있음"


def test_pid_alive_ignores_zombies(tmp_path: Path):
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not pid_alive(p.pid):
            break
        time.sleep(0.05)
    assert not pid_alive(p.pid)  # 아직 reap 안 된 좀비여도 죽은 것으로 본다
    p.wait()
    assert not pid_alive(-1) and pid_alive(os.getpid())


# ====================================================================== 작업 폴더 가드
def _repo_with_commit(path: Path) -> Workspace:
    ws = Workspace(path)
    ws.ensure_repo()
    (path / "app.py").write_text("x = 1\n", encoding="utf-8")
    ws.commit_all("init")
    return ws


def test_guard_ignores_duet_dir_and_allows_design_for_skip(tmp_path: Path):
    _repo_with_commit(tmp_path)
    (tmp_path / ".gitignore").write_text("", encoding="utf-8")  # .tenure/ 무시 규칙 제거
    Workspace(tmp_path).commit_all("no ignore")
    (tmp_path / ".tenure" / "runs" / "x").mkdir(parents=True)
    (tmp_path / ".tenure" / "runs" / "x" / "run.json").write_text("{}", encoding="utf-8")
    assert check_workdir(tmp_path) == tmp_path.resolve()          # .tenure/ 만 바뀜 → 통과
    (tmp_path / "DESIGN.md").write_text("# 설계\n", encoding="utf-8")
    with pytest.raises(WorkdirRefused, match="DESIGN.md"):
        check_workdir(tmp_path)                                    # 전체 실행은 DESIGN.md 를 덮으므로 거부
    assert check_workdir(tmp_path, allow=["DESIGN.md"]) == tmp_path.resolve()


def test_design_only_then_skip_design_in_existing_repo(tmp_path: Path):
    """기존 git 저장소에서 '설계만 → 사람이 수정 → 구현부터' 흐름이 --force 없이 동작해야 한다."""
    wd = tmp_path / "proj"
    wd.mkdir()
    _repo_with_commit(wd)
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0"]) == 0
    (wd / "DESIGN.md").write_text("# 사람이 고친 설계\n", encoding="utf-8")
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--skip-design", "--max-review", "2"]) == 0
    assert (wd / "DESIGN.md").read_text(encoding="utf-8") == "# 사람이 고친 설계\n"
    assert RunState.load(list_run_dirs(wd)[-1]).status == DONE_APPROVED


def test_unicode_filenames_reported_readably(tmp_path: Path):
    _repo_with_commit(tmp_path)
    (tmp_path / "메모 파일.md").write_text("x", encoding="utf-8")
    assert Workspace(tmp_path).dirty_files() == ["메모 파일.md"]


def test_design_md_backup_when_overwritten(tmp_path: Path):
    wd = tmp_path / "x"
    wd.mkdir()
    (wd / "DESIGN.md").write_text("# 원래 설계\n", encoding="utf-8")
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--design-only", "--debate-rounds", "0"]) == 0
    run_dir = list_run_dirs(wd)[-1]
    assert (run_dir / "DESIGN.prev.md").read_text(encoding="utf-8") == "# 원래 설계\n"
    assert (wd / "DESIGN.md").read_text(encoding="utf-8") != "# 원래 설계\n"


# ====================================================================== 잠금
def test_lock_blocks_second_holder_and_clears_stale(tmp_path: Path):
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path(tmp_path).parent.mkdir(parents=True)
        lock_path(tmp_path).write_text(json.dumps({"pid": sleeper.pid, "run_id": "r1", "started_at": "t"}))
        assert read_lock(tmp_path).pid == sleeper.pid
        with pytest.raises(LockBusy, match="이미 tenure 가 실행 중"):
            RunLock(tmp_path).acquire()
    finally:
        sleeper.kill()
        sleeper.wait()
    assert read_lock(tmp_path) is None          # 죽은 잠금은 없는 것과 같다
    with RunLock(tmp_path, "tenure run") as lock:  # 죽은 잠금을 치우고 잡는다
        lock.set_run("r2")
        info = read_lock(tmp_path)
        assert info.pid == os.getpid() and info.run_id == "r2"
    assert not lock_path(tmp_path).exists()


def test_cli_refuses_when_locked(tmp_path: Path, capsys):
    wd = tmp_path / "x"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        lock_path(wd).parent.mkdir(parents=True)
        lock_path(wd).write_text(json.dumps({"pid": sleeper.pid, "run_id": None, "started_at": "t"}))
        assert main(["run", "t", "--workdir", str(wd), "--mock", "--design-only"]) == 2
        assert "이미 tenure 가 실행 중" in capsys.readouterr().err
        assert list_run_dirs(wd) == []
    finally:
        sleeper.kill()
        sleeper.wait()


def test_lock_released_and_console_log_written(tmp_path: Path):
    wd = tmp_path / "x"
    assert main(["run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0"]) == 0
    run_dir = list_run_dirs(wd)[-1]
    assert not lock_path(wd).exists()
    console = (run_dir / "console.log").read_text(encoding="utf-8")
    assert "실행 시작:" in console and "완료: 리뷰 승인으로 종료" in console
    assert console.splitlines()[0].startswith("[")


# ====================================================================== 중지(SIGTERM·중지 요청 파일) → stopped_user → resume
@pytest.mark.parametrize("how", [
    pytest.param("sigterm", marks=pytest.mark.skipif(os.name == "nt", reason="Windows 에는 SIGTERM 이 없다")),
    "stopfile",
])
def test_sigterm_stops_run_and_resume_finishes(tmp_path: Path, how):
    wd = tmp_path / "x"
    env = {**os.environ, "TENURE_MOCK_DELAY": "0.5"}
    proc = subprocess.Popen([sys.executable, "-m", "tenure", "run", "t", "--workdir", str(wd), "--mock",
                             "--debate-rounds", "1"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        info = read_lock(wd)
        if info and info.run_id and list(list_run_dirs(wd)):
            break
        time.sleep(0.05)
    time.sleep(1.2)  # 몇 개의 호출이 진행되게
    if how == "sigterm":
        proc.send_signal(signal.SIGTERM)
    else:
        stopflag.request(wd)                       # UI 의 중지 (DESIGN.md 27.3)
    out, _ = proc.communicate(timeout=15)
    assert proc.returncode == 130, out
    assert "사용자 중단" in out
    run_dir = list_run_dirs(wd)[-1]
    st = RunState.load(run_dir)
    assert st.status == STOPPED_USER and st.error.startswith("사용자 중단")
    assert not lock_path(wd).exists()
    done_before = list(st.done)

    assert main(["resume", "--workdir", str(wd)]) == 0
    st = RunState.load(run_dir)
    assert st.status == DONE_APPROVED
    assert st.done[: len(done_before)] == done_before


def test_skip_design_log_line(tmp_path: Path, capsys):
    wd = tmp_path / "x"
    wd.mkdir()
    (wd / "DESIGN.md").write_text("# d\n", encoding="utf-8")
    main(["run", "t", "--workdir", str(wd), "--mock", "--skip-design", "--max-review", "2"])
    out = capsys.readouterr().out
    assert "설계 생략(기존 DESIGN.md)" in out and "토론 1라운드" not in out
