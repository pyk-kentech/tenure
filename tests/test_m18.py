"""M18: 안전장치 (DESIGN.md 24장) — 테스트 변조 감지, UI 교차 출처 차단, Claude 샌드박스."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import tenure.doctor as doctor
import tenure.orchestrator as orch_mod
from tenure import testguard
from tenure.agents.base import AgentRequest, Mode
from tenure.agents.claude_code import ClaudeCodeAdapter
from tenure.agents.mock import MockAdapter
from tenure.cli import main
from tenure.config import Config, ConfigError, load_config
from tenure.state import RunState, list_run_dirs
from tenure.ui.server import make_server
from tenure.workspace import Workspace
from tests.helpers import file_names

TWO_TESTS = "def test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n\ndef test_b():\n    assert f() == 3\n"


def repo(tmp_path: Path, files: dict[str, str]) -> tuple[Workspace, str]:
    ws = Workspace(tmp_path / "r")
    ws.workdir.mkdir(parents=True)
    ws.ensure_repo()
    write(ws, files)
    return ws, ws.commit_all("base")


def write(ws: Workspace, files: dict[str, str | None]) -> None:
    for rel, text in files.items():
        p = ws.workdir / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def kinds(signals) -> dict[str, str]:
    return {s.kind: s.file for s in signals}


# ====================================================================== testguard 단위
def test_is_test_file():
    yes = ["tests/test_x.py", "tests/helpers.py", "pkg/test_x.py", "pkg/x_test.py", "x_test.go",
           "src/a.test.ts", "src/a.spec.js", "conftest.py", "web/__tests__/a.jsx"]
    no = ["main.py", "tests/data.json", "testing.py", "src/contest.py", "README.md"]
    assert all(testguard.is_test_file(p) for p in yes)
    assert not any(testguard.is_test_file(p) for p in no)


def test_scan_detects_weakened_tests(tmp_path):
    ws, base = repo(tmp_path, {"main.py": "x = 1\n", "tests/test_a.py": TWO_TESTS,
                               "tests/test_b.py": "def test_c():\n    assert True\n",
                               "pytest.ini": "[pytest]\ntestpaths = tests\n"})
    write(ws, {"tests/test_a.py": "import pytest\n\n\n@pytest.mark.skip\ndef test_a():\n    assert 1 == 1\n",
               "tests/test_b.py": None,
               "pytest.ini": "[pytest]\ntestpaths = tests\naddopts = --deselect tests/test_a.py::test_b\n",
               "main.py": "x = 2\n"})
    ws.commit_all("weaken")
    sig = testguard.scan(ws, base)
    k = kinds(sig)
    assert set(k) == {"fewer_tests", "fewer_asserts", "skip_added", "deleted", "config"}
    assert k["deleted"] == "tests/test_b.py" and k["config"] == "pytest.ini"
    details = {s.kind: s.detail for s in sig}
    assert details["fewer_tests"] == "테스트 2 → 1개" and details["fewer_asserts"] == "단언 3 → 1개"
    assert "addopts = --deselect" in details["config"] and details["deleted"] == "테스트 1개가 든 파일"


def test_scan_quiet_for_new_and_strengthened_tests(tmp_path):
    ws, base = repo(tmp_path, {"DESIGN.md": "# d\n"})
    write(ws, {"tests/test_a.py": TWO_TESTS, "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
               "conftest.py": "import pytest\n"})
    mid = ws.commit_all("impl 1")
    assert testguard.scan(ws, base) == []                 # 새로 만든 테스트·설정은 경고하지 않는다
    write(ws, {"tests/test_a.py": TWO_TESTS + "\n\ndef test_more():\n    assert g()\n", "main.py": "y = 1\n"})
    ws.commit_all("impl 2")
    assert testguard.scan(ws, mid) == []


def test_scan_new_config_that_excludes_tests(tmp_path):
    ws, base = repo(tmp_path, {"tests/test_a.py": TWO_TESTS})
    write(ws, {"conftest.py": "collect_ignore = ['tests/test_a.py']\n",
               "package.json": '{"scripts": {"test": "jest"}}\n'})
    ws.commit_all("x")
    assert kinds(testguard.scan(ws, base)) == {"config": "conftest.py"}   # 새 package.json 의 test 스크립트는 정상


def test_scan_rename_compares_content(tmp_path):
    ws, base = repo(tmp_path, {"tests/test_a.py": TWO_TESTS})
    ws.git("mv", "tests/test_a.py", "tests/test_renamed.py")
    write(ws, {"tests/test_renamed.py": TWO_TESTS.replace("def test_b", "def helper_b")})
    ws.commit_all("rename")
    [s] = testguard.scan(ws, base)
    assert s.kind == "fewer_tests" and s.file == "tests/test_a.py → tests/test_renamed.py"


def test_scan_repro_changed(tmp_path):
    ws, base = repo(tmp_path, {"main.py": "x = 1\n"})
    reviewed = ws.head()
    write(ws, {"tests/test_repro_x.py": "def test_x():\n    assert f(1) == 2\n"})
    repro_commit = ws.commit_all("tenure: repro tests from review 1")
    rf = testguard.repro_files({"1": {"files": ["tests/test_repro_x.py"], "commit": repro_commit}})
    assert testguard.scan(ws, reviewed, repro_files=rf) == []         # 저장만 된 재현 테스트는 조용
    write(ws, {"tests/test_repro_x.py": "def test_x():\n    pass\n"})
    ws.commit_all("weaken repro")
    [s] = testguard.scan(ws, reviewed, repro_files=rf)
    assert s.kind == "repro_changed" and s.detail == "단언 1 → 0개"
    write(ws, {"tests/test_repro_x.py": None})
    ws.commit_all("delete repro")
    [s] = testguard.scan(ws, reviewed, repro_files=rf)
    assert s.kind == "repro_changed" and s.detail == "삭제됨"


def test_format_signals():
    text = testguard.format_signals([{"kind": "deleted", "file": "tests/t.py", "detail": "파일 삭제"}], "지난 리뷰 이후")
    assert text.startswith("[테스트 변경 경고] tenure 가 지난 리뷰 이후")
    assert "- 테스트 파일 삭제: `tests/t.py` (파일 삭제)" in text and "[P1]" in text
    assert testguard.format_signals([], "x") == ""


# ====================================================================== build 통합
def implementer(req, n):
    cwd = Path(req.cwd)
    if req.step == "implement":
        (cwd / "tests").mkdir(exist_ok=True)
        (cwd / "tests" / "test_a.py").write_text(TWO_TESTS, encoding="utf-8")
        (cwd / "main.py").write_text("def f():\n    return 3\n", encoding="utf-8")
    elif req.step == "fix":
        (cwd / "tests" / "test_a.py").write_text("def test_a():\n    assert 1 == 1\n", encoding="utf-8")
    return f"{req.step} 완료"


def run(tmp_path, monkeypatch, *extra, toml=""):
    seen: dict = {}

    def factory(cfg, *, mock):
        seen["claude"] = MockAdapter(label="claude", responder=implementer)
        seen["codex"] = MockAdapter(label="codex", approve_on_review=2)
        return dict(seen)

    monkeypatch.setattr(orch_mod, "make_agents", factory)
    wd = tmp_path / "w"
    wd.mkdir(parents=True)
    (wd / "tenure.toml").write_text('test_command = "none"\n' + toml, encoding="utf-8")
    rc = main(["run", "t", "--workdir", str(wd), "--mock", "--debate-rounds", "0", *extra])
    [rd] = list_run_dirs(wd)
    return rc, seen, RunState.load(rd), rd


def test_build_warns_reviewer_about_weakened_tests(tmp_path, monkeypatch, capsys):
    rc, seen, st, rd = run(tmp_path, monkeypatch)
    assert rc == 0 and list(st.test_guard) == ["2"]                     # 1라운드(새 테스트)는 조용
    g = st.test_guard["2"]
    assert {s["kind"] for s in g["signals"]} == {"fewer_tests", "fewer_asserts"}
    assert g["file"] in file_names(rd) and g["file"].endswith("_testguard2.md")
    reviews = [c for c in seen["codex"].calls if c.step == "review"]
    assert "[테스트 변경 경고]" not in reviews[0].prompt
    assert "[테스트 변경 경고] tenure 가 지난 리뷰 이후" in reviews[1].prompt and "`tests/test_a.py`" in reviews[1].prompt
    assert "주의: 테스트 변경 감지 2건" in capsys.readouterr().out
    assert "guard:1" in st.done and "guard:2" in st.done


def test_build_guard_can_be_turned_off(tmp_path, monkeypatch):
    rc, seen, st, _ = run(tmp_path, monkeypatch, toml="test_guard = false\n")
    assert rc == 0 and st.test_guard == {} and st.config["test_guard"] is False
    assert all("[테스트 변경 경고]" not in c.prompt for c in seen["codex"].calls)


# ====================================================================== UI 교차 출처 (24.2)
TOKEN = "t-m18"


@pytest.fixture
def server():
    srv = make_server("127.0.0.1", 0, token=TOKEN)
    t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def req(srv, path, headers, method="GET", token=True):
    h = dict(headers)
    if token:
        h["X-Tenure-Token"] = TOKEN
    data = b"{}" if method == "POST" else None
    if data is not None:
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(f"http://127.0.0.1:{srv.port}{path}", data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code


def test_ui_rejects_cross_origin(server):
    own = f"http://127.0.0.1:{server.port}"
    assert req(server, "/api/projects", {"Origin": own}) == 200
    assert req(server, "/api/projects", {"Origin": f"http://localhost:{server.port}"}) == 200
    assert req(server, "/api/projects", {"Origin": "http://evil.example"}) == 403
    assert req(server, "/api/projects", {"Origin": "null"}) == 403
    assert req(server, "/api/projects", {"Origin": "http://evil.example"}, method="POST") == 403
    assert req(server, f"/?t={TOKEN}", {"Origin": "http://evil.example"}, token=False) == 403


def test_ui_rejects_cross_site_fetch(server):
    assert req(server, "/api/projects", {"Sec-Fetch-Site": "same-origin"}) == 200
    assert req(server, "/api/projects", {}) == 200                       # curl 등 (토큰으로 판단)
    assert req(server, "/api/projects", {"Sec-Fetch-Site": "cross-site"}) == 403
    assert req(server, "/api/projects", {"Sec-Fetch-Site": "same-site"}) == 403
    # 페이지 열기는 다른 앱의 링크로 여는 경우가 있어 막지 않는다 (토큰으로 보호)
    assert req(server, f"/?t={TOKEN}", {"Sec-Fetch-Site": "cross-site"}, token=False) == 200


# ====================================================================== Claude 샌드박스 (24.3)
def cmd(cfg: Config, mode: Mode) -> list[str]:
    return ClaudeCodeAdapter(cfg).build_command(AgentRequest(prompt="p", cwd=Path("."), mode=mode, timeout_sec=60, step="implement"))[0]


def test_sandbox_flag_only_for_write_when_enabled():
    assert "--settings" not in cmd(Config(access="standard"), Mode.WRITE)
    on = Config(claude_sandbox=True, access="standard")
    c = cmd(on, Mode.WRITE)
    settings = json.loads(c[c.index("--settings") + 1])
    assert settings["sandbox"]["enabled"] is True and settings["sandbox"]["allowUnsandboxedCommands"] is False
    assert "--settings" not in cmd(on, Mode.READ)
    assert "--settings" not in cmd(Config(claude_sandbox=True), Mode.WRITE)     # 권한 full 은 샌드박스를 쓰지 않음 (33.5)


def test_sandbox_settings_config(tmp_path):
    wd = tmp_path / "w"
    wd.mkdir()
    (wd / "tenure.toml").write_text('access = "standard"\nclaude_sandbox = true\n[claude]\nsandbox_settings = \'{"sandbox": {"enabled": true}}\'\n',
                                  encoding="utf-8")
    cfg = load_config(wd, global_defaults={})
    c = cmd(cfg, Mode.WRITE)
    assert c[c.index("--settings") + 1] == '{"sandbox": {"enabled": true}}'
    (wd / "tenure.toml").write_text("[claude]\nsandbox_settings = '{bad'\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="sandbox_settings"):
        load_config(wd, global_defaults={})


def test_cli_claude_sandbox_flag(tmp_path, monkeypatch):
    rc, _, st, _ = run(tmp_path, monkeypatch, "--claude-sandbox")
    assert st.config["claude_sandbox"] is True


def test_doctor_sandbox_check(monkeypatch):
    monkeypatch.setattr("tenure.config.load_config", lambda *a, **k: Config(claude_sandbox=False))
    assert doctor._sandbox_check() is None
    monkeypatch.setattr("tenure.config.load_config", lambda *a, **k: Config(claude_sandbox=True))
    monkeypatch.setattr(doctor.sys, "platform", "linux")
    monkeypatch.setattr(doctor, "_which", lambda name: "/usr/bin/bwrap" if name == "bwrap" else "")
    c = doctor._sandbox_check()
    assert c.status == "warn" and c.extra["missing"] == ["socat"] and "socat" in c.detail
    monkeypatch.setattr(doctor, "_which", lambda name: f"/usr/bin/{name}")
    assert doctor._sandbox_check().status == "ok"
