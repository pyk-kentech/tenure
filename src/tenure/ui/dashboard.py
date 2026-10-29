"""관리자 UI 의 데이터·동작 계층. HTTP 와 분리해 단위 테스트할 수 있게 한다.

모든 경로 인자(project, run, file, commit)는 여기서 검증한다:
- project 는 등록된 작업 폴더여야 한다.
- run 은 그 폴더의 .tenure/runs 아래 실제 실행이어야 한다.
- file 은 그 실행의 기록 파일(NN_*.md) 이름이어야 한다.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import stat
import sys
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from subprocess import Popen
from typing import Any

from tenure.events import last_event, read_events
from tenure.outcome import run_activity
from tenure import merge as mergelib
from tenure import notes as notelib
from tenure import procs, slots, stopflag, worktree
from tenure.ui import files as fileslib
from tenure.phases.race import DRAFTS_DIR as race_drafts_dir
from tenure.ui.cache import RunDirs, StatCache, TtlCache
from tenure.activity import agent_activity, call_start_epoch
from tenure.workflow import WorkflowError, list_workflows, load_workflow, role_agent_map
from tenure.config import AGENT_NAMES, CLASSIC_WORKFLOW, WORKFLOW_NAME_RE, ConfigError, MAX_DEBATE_ROUNDS, MAX_REVIEW_ROUNDS, describe_effort, load_config
from tenure.lock import lock_path, read_lock
from tenure.orchestrator import CONSOLE_LOG
from tenure.phases.build import build_review_diff
from tenure.phases.debate import BACKUP_FILENAME, DESIGN_FILENAME
from tenure.doctor import login_check, ready, run_checks
from tenure.registry import Registry, tenure_home, normalize
from tenure.runner import ExecutableNotFound, pid_alive, spawn_detached
from tenure.agents.claude_code import load_claude_limits
from tenure.usage import claude_local_usage, codex_rate_limits
from tenure.state import DONE_APPROVED, DONE_UNAPPROVED, LEGACY_DIR, TENURE_DIR, RUNNING, RunState, project_dir
from tenure.transcript import Transcript, split_header
from tenure.verdict import parse_verdict
from tenure.workspace import GitError, Workspace, WorkdirRefused, check_workdir

MAX_VERDICT_CACHE = 2048
SUMMARY_KEYS = ("run_id", "status", "phase", "step", "round", "task", "created_at", "updated_at", "calls", "total_sec",
                "last_verdict", "config", "error", "archived_at")
RUN_ID_RE = re.compile(r"^\d{8}-\d{6}(?:-\d+)?$")
ENTRY_RE = re.compile(r"^(?:drafts/[a-z0-9][a-z0-9_-]{0,31}/)?\d{2,}_[\w.\-]+\.md$")   # 초안 기록 포함 (36.15)
COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")
MAX_DIFF_BYTES = 400_000
MAX_CONSOLE_CHUNK = 200_000
MAX_TASK_CHARS = 200_000
UI_DIR = "ui"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def self_command() -> list[str]:
    """이 tenure 를 다시 실행하는 명령 (python -m tenure · tenure.pyz · 얼린 엔진, DESIGN.md 36.20)."""
    from tenure.selfcmd import self_argv

    return self_argv()


def jobs_dir() -> Path:
    return tenure_home() / "jobs"


def _read_job(path: Path) -> dict[str, Any] | None:
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    rc_path = path.with_suffix(".rc")
    rc = None
    if rc_path.exists():
        try:
            rc = int(rc_path.read_text().strip())
        except (OSError, ValueError):
            rc = -1
    running = rc is None and pid_alive(int(rec.get("pid") or 0))
    if rc is None and not running:
        rc = -1  # 기록 없이 사라짐(강제 종료 등)
    try:
        tail = Path(rec["log_path"]).read_text(encoding="utf-8", errors="replace")[-4000:]
    except (OSError, KeyError):
        tail = ""
    return {**rec, "running": running, "returncode": rc, "log_tail": tail}


@dataclass
class Dashboard:
    registry: Registry = field(default_factory=Registry)
    _popens: list[Popen] = field(default_factory=list)   # 이 프로세스가 띄운 자식(좀비 방지용 reap)
    _jobs_lock: threading.Lock = field(default_factory=threading.Lock)
    _verdict_cache: dict[tuple[str, float], dict] = field(default_factory=dict)
    _reserved: set[str] = field(default_factory=set)      # 이 프로세스가 미리 정한 실행 id (36.14)
    # 읽기 캐시 (36.18): 파일이 그대로면 다시 읽지 않는다
    _states: StatCache = field(default_factory=lambda: StatCache(64))     # 실행 화면용 전체 상태 (최근 것만)
    _bases: StatCache = field(default_factory=StatCache)                   # 목록·사용량용 작은 요약
    _events: StatCache = field(default_factory=StatCache)
    _indexes: StatCache = field(default_factory=lambda: StatCache(512))
    _dirs: RunDirs = field(default_factory=RunDirs)
    _locks: StatCache = field(default_factory=lambda: StatCache(1024))
    _lock_paths: TtlCache = field(default_factory=lambda: TtlCache(ttl=30.0))   # project_dir(옛 .duet 옮기기 확인) 결과
    _logins: TtlCache = field(default_factory=lambda: TtlCache(ttl=60.0))       # CLI 로그인 상태 (36.37)
    _live_now: TtlCache = field(default_factory=lambda: TtlCache(ttl=0.3))      # 폴더별 진행 중 실행 (36.38)

    # ================================================================ validation
    def project(self, path: str | None) -> Path:
        if not path:
            raise ApiError(400, "project 가 필요합니다.")
        p = normalize(path)
        if p not in self.registry.list():
            # 등록하지 않는 벤치마크 실행도 알려진 결과 폴더 안이면 열 수 있다 (DESIGN.md 28.3)
            root = bench_root_of(Path(p))
            if root is None or root not in self.bench_dirs():
                raise ApiError(404, f"등록되지 않은 프로젝트입니다: {p}")
        return Path(p)

    def run_dir(self, project: Path, run_id: str | None) -> Path:
        if not run_id or not RUN_ID_RE.match(run_id):
            raise ApiError(400, "run 형식이 잘못되었습니다.")
        for d in self._run_dirs(project):              # worktree 실행 포함 (36.14)
            if d.name == run_id:
                return d
        raise ApiError(404, f"실행을 찾을 수 없습니다: {run_id}")

    @staticmethod
    def code_dir(rd: Path) -> Path:
        """실행의 코드 폴더: 프로젝트 또는 그 실행의 worktree. git·잠금·세션 기록은 여기서 본다."""
        return worktree.code_dir(rd)

    def _run_live(self, rd: Path) -> dict[str, Any] | None:
        live = self._live(self.code_dir(rd))
        return live if live and live.get("run_id") == rd.name else None

    def _tree_info(self, project: Path, run_id: str) -> dict[str, Any] | None:
        t = worktree.find(project, run_id)
        return asdict(t) if t is not None else None

    def _code_ws(self, project: Path, rd: Path) -> Workspace:
        """열린 worktree 면 그 폴더, 병합·버린 실행이면 프로젝트(브랜치는 남아 있다)."""
        return Workspace(self.code_dir(rd))

    def _head_ref(self, project: Path, rd: Path) -> str:
        t = worktree.find(project, rd.name)
        return t.branch if t is not None and t.status != worktree.OPEN else "HEAD"

    def _project_live(self, project: Path) -> dict[str, Any] | None:
        """프로젝트에서 도는 실행 하나(프로젝트 폴더 먼저, 다음은 열린 worktree)와 그 수."""
        found = [x for x in [self._live(project)] if x]
        for t in self._trees(project):
            if t.status == worktree.OPEN:
                x = self._live(Path(t.path))
                if x:
                    found.append(x)
        return {**found[0], "count": len(found)} if found else None

    # ================================================================ projects
    def projects(self) -> list[dict[str, Any]]:
        self.reap()
        out = []
        for p in self.registry.list():
            path = Path(p)
            runs = self._run_dirs(path) if path.is_dir() else []
            latest = self._run_summary(path, runs[-1]) if runs else None
            bench = bench_root_of(path)
            out.append({"path": p, "name": path.name or p, "exists": path.is_dir(),
                        "bench": bench is not None, "bench_root": str(bench) if bench else None,
                        "run_count": len(runs), "latest": latest, "live": self._project_live(path) if path.is_dir() else None})
        _display_names(out)
        return out

    def prune_projects(self) -> dict[str, Any]:
        """폴더가 없는 프로젝트를 목록에서만 지운다 (DESIGN.md 28.2). 파일은 건드리지 않는다."""
        removed = [p for p in self.registry.list() if not Path(p).is_dir()]
        for p in removed:
            self.registry.remove(p)
        return {"removed": removed}

    # ================================================================ bench (DESIGN.md 28.3)
    def bench_dirs(self) -> list[Path]:
        from tenure.bench import known_result_dirs

        roots: set[Path] = set(known_result_dirs())
        for p in self.registry.list():
            b = bench_root_of(Path(p))
            if b is not None:
                roots.add(b)
        return sorted(roots, key=lambda p: p.name, reverse=True)

    def bench_list(self) -> dict[str, Any]:
        out = []
        for d in self.bench_dirs():
            try:
                data = json.loads((d / "results.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            meta, rows = data.get("meta") or {}, data.get("rows") or []
            out.append({"dir": str(d), "name": d.name, "suite": meta.get("suite"), "workflows": meta.get("workflows"),
                        "tasks": meta.get("tasks"), "repeat": meta.get("repeat"), "mock": meta.get("mock"),
                        "started": meta.get("started"), "runs": len(rows),
                        "approved": sum(1 for r in rows if r.get("approved")),
                        "all_passed": sum(1 for r in rows if (r.get("hidden") or {}).get("all_passed"))})
        return {"results": out}

    def bench_result(self, path: str | None) -> dict[str, Any]:
        allowed = {str(d): d for d in self.bench_dirs()}
        d = allowed.get(str(path or ""))
        if d is None:
            raise ApiError(404, "벤치마크 결과 폴더를 찾을 수 없습니다.")
        from tenure.bench import workflow_stats
        data = json.loads((d / "results.json").read_text(encoding="utf-8"))
        rows = data.get("rows") or []
        for r in rows:
            r["has_runs"] = bool(r.get("workdir")) and (Path(r["workdir"], TENURE_DIR, "runs").is_dir() or Path(r["workdir"], LEGACY_DIR, "runs").is_dir())
        return {"dir": str(d), "meta": data.get("meta") or {}, "rows": rows, "stats": workflow_stats(rows)}

    def add_project(self, path: str | None) -> dict[str, Any]:
        if not path or not str(path).strip():
            raise ApiError(400, "경로를 입력하세요.")
        raw = Path(str(path).strip()).expanduser()
        if not raw.is_absolute():
            raise ApiError(400, "절대 경로를 입력하세요 (예: /home/me/proj).")
        try:
            wd = check_workdir(raw, force=True)
        except WorkdirRefused as e:
            raise ApiError(400, str(e)) from e
        return {"path": self.registry.add(wd)}

    def remove_project(self, path: str | None) -> dict[str, Any]:
        p = self.project(path)
        self.registry.remove(p)
        return {"removed": str(p)}

    # ================================================================ runs
    def runs(self, project: str | None) -> dict[str, Any]:
        wd = self.project(project)
        runs = [self._run_summary(wd, d) for d in reversed(self._run_dirs(wd))]
        return {"project": str(wd), "live": self._project_live(wd), "runs": runs,
                "has_design": (wd / DESIGN_FILENAME).is_file(), "defaults": self._defaults(wd)}

    def run(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        st = self._state(rd)
        code = self.code_dir(rd)
        live = self._live(code)
        entries = []
        sources = [("", rd)] + [(d.name, d) for d in sorted((rd / race_drafts_dir).glob("*")) if d.is_dir()]
        for draft, folder in sources:
            for e in Transcript(folder).entries():
                name = f"{race_drafts_dir}/{draft}/{e.name}" if draft else e.name
                item: dict[str, Any] = {"name": name, "seq": e.seq, "label": e.label, "meta": e.meta}
                if draft:
                    item["draft"] = draft
                if e.meta.get("step") == "review" and e.meta.get("exit_code") == "0":
                    item["verdict"] = self._verdict(e.path)
                entries.append(item)
        if len(sources) > 1:                     # 경쟁 초안: 초안 기록을 시간순으로 끼워 넣는다 (36.15)
            entries.sort(key=lambda x: str(x["meta"].get("timestamp") or ""))
        console = rd / CONSOLE_LOG
        return {
            "project": str(wd), "run_dir": str(rd), "state": st.to_dict(), "entries": entries,
            "live": bool(live and live.get("run_id") == st.run_id),
            "live_pid": live.get("pid") if live and live.get("run_id") == st.run_id else None,
            "stale": st.status == RUNNING and not (live and live.get("run_id") == st.run_id),
            "console_size": console.stat().st_size if console.is_file() else 0,
            "has_design_backup": (rd / BACKUP_FILENAME).is_file(),
            "current_call": self._current_call(rd) if live and live.get("run_id") == st.run_id else None,
            "resume": self._resume_hint(st, code),
            "code_dir": str(code), "worktree": self._tree_info(wd, rd.name),
            "notes_open": sum(1 for n in notelib.load(rd) if n.get("status") == notelib.OPEN),
            "now": dt.datetime.now().isoformat(timespec="seconds"),
            "activity": self._activity(rd, st, bool(live and live.get("run_id") == st.run_id)),
            "last_seq": (self._last_event(rd) or {}).get("seq", 0),
        }

    def events(self, project: str | None, run_id: str | None, since: int = 0) -> dict[str, Any]:
        """seq 가 since 보다 큰 이벤트 (실시간 타임라인, DESIGN.md 36.3). SSE 가 없는 원격·브라우저는 이것으로 폴링."""
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        evs = read_events(rd, since=int(since or 0))
        return {"events": evs, "last_seq": evs[-1]["seq"] if evs else int(since or 0)}

    def mark_seen(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        """끝난 실행을 열어 봤다고 적는다(받은편지함에서 '끝났는데 안 봄'이 사라짐). 진행 중이면 그대로."""
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        if self._run_live(rd):
            return {"ok": False, "reason": "진행 중"}
        st = RunState.load(rd)
        st.seen_at = dt.datetime.now().isoformat(timespec="seconds")
        st.save(rd)
        return {"ok": True, "seen_at": st.seen_at}

    def archive_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """플로 목록에서 숨기거나(보관) 다시 꺼낸다. 기록은 그대로 둔다 (DESIGN.md 36.37 M52)."""
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        if self._run_live(rd):
            raise ApiError(409, "진행 중인 플로는 보관할 수 없습니다.")
        st = RunState.load(rd)
        st.archived_at = dt.datetime.now().isoformat(timespec="seconds") if payload.get("archived", True) else None
        st.save(rd, touch=False)                       # 목록의 걸린 시간이 바뀌지 않게
        return {"ok": True, "archived_at": st.archived_at}

    def delete_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """플로 기록 폴더를 지운다. 진행 중이거나 반영·버리기 전인 따로 브랜치가 있으면 거절 (36.37 M52)."""
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        if self._run_live(rd):
            raise ApiError(409, "진행 중인 플로는 지울 수 없습니다. 먼저 멈추세요.")
        t = worktree.find(wd, rd.name)
        if t is not None and t.status == worktree.OPEN:
            raise ApiError(409, "이 플로의 변경이 아직 따로 브랜치에 있습니다. 먼저 반영하거나 버리세요.")

        def unlock(fn: Any, path: str, _exc: Any) -> None:   # 윈도의 읽기 전용 파일
            os.chmod(path, stat.S_IWRITE)
            fn(path)

        shutil.rmtree(rd, onerror=unlock)
        return {"ok": True, "deleted": rd.name}

    def inbox(self, limit: int = 50, per_project: int = 20) -> dict[str, Any]:
        """프로젝트를 가로질러 주의 순으로: 나를 기다림 > 끝났는데 안 봄 > 진행 중 (DESIGN.md 36.7 받은편지함)."""
        items: list[dict[str, Any]] = []
        for path in self.registry.list():          # projects() 를 다시 부르지 않는다 (36.18)
            wd = Path(path)
            if not wd.is_dir():
                continue
            for rd in list(reversed(self._run_dirs(wd)))[:per_project]:
                summary = self._run_summary(wd, rd)
                if (summary.get("worktree") or {}).get("status") == worktree.DISCARDED or summary.get("archived_at"):
                    continue                                   # 버린·보관한 작업은 더 볼 일이 없다 (36.14, 36.37)
                act = summary.get("activity") or {}
                if act.get("state") in ("blocked", "done-unseen", "working", "unverifiable") or (
                        act.get("state") == "failed" and summary.get("status") == RUNNING):
                    items.append({"project": str(wd), "run_id": summary["run_id"], "task": summary.get("task", "")[:200],
                                  "status": summary.get("status"), "activity": act,
                                  "updated_at": summary.get("updated_at")})
        items.sort(key=lambda x: str(x.get("updated_at") or ""), reverse=True)   # 같은 순위 안에서는 최근 것 먼저
        items.sort(key=lambda x: -x["activity"].get("rank", 0))
        return {"items": items[:limit], "counts": {k: sum(1 for i in items if i["activity"]["state"] == k)
                                                    for k in ("blocked", "done-unseen", "working", "unverifiable")}}

    def activity(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        """지금 진행 중인 호출에서 에이전트가 하는 일(파일 읽기·수정·명령·메시지)."""
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        if not self._run_live(rd):
            return {"live": False, "call": None, "events": []}
        call = self._current_call(rd)
        if not call:
            return {"live": True, "call": None, "events": []}
        since = call_start_epoch(call["started"])
        return {"live": True, "call": call, "events": agent_activity(call["cli"], self.code_dir(rd), since)}

    def _current_call(self, rd: Path) -> dict[str, Any] | None:
        """지금 호출. 로그에는 역할 이름이 찍히므로 세션 기록을 찾을 CLI 이름(cli)을 붙인다 (DESIGN.md 19.4)."""
        call = current_call(rd / CONSOLE_LOG)
        if call is None:
            return None
        try:
            roles = role_agent_map(RunState.load(rd).workflow)
        except (OSError, ValueError):
            roles = {}
        call["cli"] = roles.get(call["agent"], call["agent"])
        return call

    def workflows(self, project: str | None) -> dict[str, Any]:
        wd: Path | None = None
        if project:
            try:
                wd = _workflow_api().workdir_of(self, project)   # 등록 전 작업공간 폴더도 (36.29)
            except ApiError:
                wd = None
        try:
            cfg = load_config(wd)
        except ConfigError as e:
            return {"workflows": [], "default": CLASSIC_WORKFLOW, "error": str(e)}
        return {"workflows": list_workflows(wd, cfg), "default": cfg.workflow}

    def entry(self, project: str | None, run_id: str | None, name: str | None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        if not name or not ENTRY_RE.match(name) or not (rd / name).is_file():
            raise ApiError(404, "기록 파일을 찾을 수 없습니다.")
        meta, body = split_header((rd / name).read_text(encoding="utf-8", errors="replace"))
        return {"name": name, "meta": meta, "body": body}

    def console(self, project: str | None, run_id: str | None, offset: int = 0) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        path = rd / CONSOLE_LOG
        if not path.is_file():
            return {"text": "", "offset": 0, "size": 0}
        size = path.stat().st_size
        offset = max(0, min(int(offset), size))
        if size - offset > MAX_CONSOLE_CHUNK:
            offset = size - MAX_CONSOLE_CHUNK
        with path.open("rb") as f:
            f.seek(offset)
            data = f.read(MAX_CONSOLE_CHUNK)
        return {"text": data.decode("utf-8", errors="replace"), "offset": offset + len(data), "size": size}

    def design(self, project: str | None, run_id: str | None = None, which: str = "current") -> dict[str, Any]:
        wd = self.project(project)
        if which == "backup":
            rd = self.run_dir(wd, run_id)
            path = rd / BACKUP_FILENAME
        else:
            # 변경 설계로 끝난 실행은 그 실행의 docs/changes/<run>.md (DESIGN.md 19.5)
            rd = self.run_dir(wd, run_id) if run_id else None
            doc = RunState.load(rd).design_doc if rd is not None else None
            path = (self.code_dir(rd) if rd is not None else wd) / (doc or DESIGN_FILENAME)
        if not path.is_file():
            return {"text": None, "path": str(path)}
        return {"text": path.read_text(encoding="utf-8", errors="replace"), "path": str(path)}

    # ================================================================ git
    def commits(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        st = RunState.load(rd)
        ws = self._code_ws(wd, rd)
        if not st.base_commit or not ws.is_repo():
            return {"base": None, "commits": []}
        try:
            fmt = "%H%x1f%h%x1f%s%x1f%cI"
            out = ws.git("log", f"--format={fmt}", f"{st.base_commit}^!", "--")
            out += ws.git("log", "--reverse", f"--format={fmt}", f"{st.base_commit}..{self._head_ref(wd, rd)}", "--")
        except GitError as e:
            raise ApiError(500, str(e)) from e
        commits = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                commits.append({"sha": parts[0], "short": parts[1], "subject": parts[2], "date": parts[3]})
        return {"base": st.base_commit, "commits": commits}

    def diff(self, project: str | None, run_id: str | None, commit: str | None = None,
             draft: str | None = None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        st = RunState.load(rd)
        ws = self._code_ws(wd, rd)
        if not st.base_commit or not ws.is_repo():
            return {"text": "", "truncated": False, "title": "아직 커밋이 없습니다."}
        try:
            if draft:                            # 경쟁 초안 하나의 기준 대비 차이 (36.15)
                info = ((st.race or {}).get("drafts") or {}).get(draft)
                if not info:
                    raise ApiError(404, f"초안을 찾을 수 없습니다: {draft}")
                try:
                    globs = load_config(wd).diff_excludes
                except ConfigError:
                    globs = []
                text = build_review_diff(ws, st.race["base"], globs, MAX_DIFF_BYTES * 2, head=info["branch"])
                text = "" if text == "(변경 없음)" else text
                truncated = len(text) > MAX_DIFF_BYTES
                return {"text": text[:MAX_DIFF_BYTES], "truncated": truncated, "title": f"{draft} 초안 ({info['branch']})"}
            if commit:
                if not COMMIT_RE.match(commit):
                    raise ApiError(400, "commit 형식이 잘못되었습니다.")
                if ws.git("cat-file", "-t", commit, check=False).strip() != "commit":
                    raise ApiError(404, "커밋을 찾을 수 없습니다.")
                text = ws.git("show", "--no-color", "--format=commit %H%n%s%n", commit)
                title = f"커밋 {commit[:7]}"
            else:
                try:
                    globs = load_config(wd).diff_excludes
                except ConfigError:
                    globs = []
                text = build_review_diff(ws, st.base_commit, globs, MAX_DIFF_BYTES * 2, head=self._head_ref(wd, rd))
                if text == "(변경 없음)":
                    text = ""
                title = f"기준 {st.base_commit} 대비 전체 변경 (리뷰어가 받는 diff 와 같은 규칙: DESIGN.md·잠금 파일 제외)"
        except GitError as e:
            raise ApiError(500, str(e)) from e
        truncated = len(text) > MAX_DIFF_BYTES
        return {"text": text[:MAX_DIFF_BYTES], "truncated": truncated, "title": title}

    # ================================================================ actions
    def start_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = str(payload.get("task") or "").strip()
        if not task:
            raise ApiError(400, "과제를 입력하세요.")
        if len(task) > MAX_TASK_CHARS:
            raise ApiError(400, "과제가 너무 깁니다.")
        raw = str(payload.get("project") or "").strip()
        if not raw or not Path(raw).expanduser().is_absolute():
            raise ApiError(400, "작업 폴더는 절대 경로여야 합니다.")
        design_only, skip_design = bool(payload.get("design_only")), bool(payload.get("skip_design"))
        mock, force = bool(payload.get("mock")), bool(payload.get("force"))
        if design_only and skip_design:
            raise ApiError(400, "'설계만'과 '설계 생략'은 함께 고를 수 없습니다.")
        try:
            wd = check_workdir(Path(raw), force=force, allow=(DESIGN_FILENAME,) if skip_design else ())
        except WorkdirRefused as e:
            raise ApiError(400, str(e)) from e
        overrides: dict[str, Any] = {}
        workflow = str(payload.get("workflow") or "").strip()
        if workflow:                                   # classic 도 명시해서 넘긴다(기본이 auto 라서, 36.5)
            if not WORKFLOW_NAME_RE.match(workflow):
                raise ApiError(400, f"워크플로 이름이 잘못되었습니다: {workflow}")
            overrides["workflow"] = workflow
        for key in ("implementer", "reviewer", "synth"):
            v = payload.get(key)
            if v and overrides.get("workflow", CLASSIC_WORKFLOW) == CLASSIC_WORKFLOW:
                if v not in AGENT_NAMES:
                    raise ApiError(400, f"{key} 값이 잘못되었습니다: {v}")
                overrides[key] = v
        for key in ("debate_rounds", "max_review"):
            v = payload.get(key)
            if v not in (None, ""):
                try:
                    overrides[key] = int(v)
                except (TypeError, ValueError):
                    raise ApiError(400, f"{key} 는 숫자여야 합니다.") from None
        for agent in AGENT_NAMES:
            for field_ in ("model", "effort"):
                v = payload.get(f"{agent}_{field_}")
                if v not in (None, ""):
                    overrides[f"{agent}.{field_}"] = str(v).strip()
        step_items = self._step_items(payload.get("step_overrides"))
        if step_items:
            from tenure.config import parse_set_step

            try:
                overrides["step_overrides"] = parse_set_step(step_items)
            except ConfigError as e:
                raise ApiError(400, f"설정 오류: {e}") from e
        if str(payload.get("test_command") or "").strip():
            overrides["test_command"] = str(payload["test_command"]).strip()
        if payload.get("block_severity"):
            overrides["block_severity"] = str(payload["block_severity"])
        if payload.get("stall_rounds") not in (None, ""):
            try:
                overrides["stall_rounds"] = int(payload["stall_rounds"])
            except (TypeError, ValueError):
                raise ApiError(400, "stall_rounds 는 숫자여야 합니다.") from None
        from tenure.options import OptionError, clean as clean_options, to_cli

        try:
            advanced = clean_options(payload.get("options") or {})   # 고급 설정 (DESIGN.md 28.4)
        except OptionError as e:
            raise ApiError(400, str(e)) from e
        advanced = {k: v for k, v in advanced.items() if k not in overrides}
        overrides.update(advanced)
        try:
            cfg = load_config(wd if wd.is_dir() else None, overrides)
        except ConfigError as e:
            raise ApiError(400, f"설정 오류: {e}") from e
        # auto 는 실행할 때 과제를 보고 고른다(36.5). 여기서는 classic 으로 미리 확인해 둔다(설계만 등)
        check_name = CLASSIC_WORKFLOW if cfg.workflow == "auto" else cfg.workflow
        try:
            wf = load_workflow(check_name, wd if wd.is_dir() else None, cfg)
        except WorkflowError as e:
            raise ApiError(400, f"워크플로 오류: {e}") from e
        unknown = sorted(set(cfg.step_overrides) - set(wf.roles))
        if unknown:
            raise ApiError(400, f"칸 단위 지정의 역할 {', '.join(unknown)} 이 워크플로 {wf.name} 에 없습니다.")
        if design_only and wf.first("debate") is None:
            raise ApiError(400, f"워크플로 {wf.name} 에는 설계 단계가 없어 '설계만'을 고를 수 없습니다.")
        if skip_design and not (wd / DESIGN_FILENAME).is_file():
            raise ApiError(400, f"'설계 생략'인데 {wd / DESIGN_FILENAME} 이 없습니다.")
        # worktree 실행은 폴더가 따로라 프로젝트에 다른 실행이 돌아도 시작할 수 있다 (36.14)
        use_tree = not design_only and wd.is_dir() and worktree.should_use(wd, cfg.worktree)
        if not use_tree:
            self._ensure_not_busy(wd)
        run_id = self._reserve_run_id(wd, git=use_tree)
        scope = project_dir(wd) / worktree.TREES_DIR / run_id if use_tree else wd

        uploads = self._decode_attachments(payload.get("attachments"))
        wd.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        attach_paths = []
        if uploads:
            adir = project_dir(wd) / UI_DIR / "attachments" / stamp
            adir.mkdir(parents=True, exist_ok=True)
            for name, data in uploads:
                target = adir / name
                n = 2
                while target.exists():
                    target = adir / f"{Path(name).stem}-{n}{Path(name).suffix}"
                    n += 1
                target.write_bytes(data)
                attach_paths.append(target)
        task_file = project_dir(wd) / UI_DIR / "tasks" / f"{stamp}.md"
        task_file.parent.mkdir(parents=True, exist_ok=True)
        task_file.write_text(task, encoding="utf-8")

        argv = ["run", f"@{task_file}", "--workdir", str(wd)]
        for key, flag in (("workflow", "--workflow"), ("implementer", "--implementer"), ("reviewer", "--reviewer"), ("synth", "--synth"),
                          ("debate_rounds", "--debate-rounds"), ("max_review", "--max-review"),
                          ("claude.model", "--claude-model"), ("claude.effort", "--claude-effort"),
                          ("codex.model", "--codex-model"), ("codex.effort", "--codex-effort"),
                          ("test_command", "--test-command"), ("block_severity", "--block-severity"),
                          ("stall_rounds", "--stall-rounds")):
            if key in overrides:
                argv += [flag, str(overrides[key])]
        for key, value in advanced.items():
            argv += ["--set", f"{key}={to_cli(value)}"]
        for item in step_items:
            argv += ["--set-step", item]
        for p in attach_paths:
            argv += ["--attach", str(p)]
        for on, flag in ((design_only, "--design-only"), (skip_design, "--skip-design"),
                         (mock, "--mock"), (force, "--force")):
            if on:
                argv.append(flag)
        argv += ["--run-id", run_id]
        self.registry.add(wd)
        return {**self._spawn("run", wd, argv, scope=scope), "run_id": run_id, "worktree": use_tree}

    def _reserve_run_id(self, wd: Path, *, git: bool) -> str:
        with self._jobs_lock:
            base = worktree.unique_run_id(wd, git=git)
            rid, n = base, 1
            while rid in self._reserved:
                n += 1
                rid = f"{base}-{n}"
                try:
                    worktree.unique_run_id(wd, rid, git=git)
                except worktree.WorktreeError:
                    continue
            self._reserved.add(rid)
            return rid

    # ================================================================ merge (DESIGN.md 36.14)
    def merge_check(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        try:
            return mergelib.readiness(wd, rd.name).to_dict()
        except mergelib.MergeError as e:
            raise ApiError(400, str(e)) from e

    def merge(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        mode = str(payload.get("mode") or mergelib.SQUASH)
        try:
            return mergelib.merge(wd, rd.name, mode)
        except (mergelib.MergeError, GitError) as e:
            raise ApiError(409, str(e)) from e

    def discard(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        self._ensure_not_busy(self.code_dir(rd))
        try:
            return mergelib.discard(wd, rd.name)
        except (mergelib.MergeError, GitError) as e:
            raise ApiError(409, str(e)) from e

    def followup(self, payload: dict[str, Any], kind: str) -> dict[str, Any]:
        """충돌 해결(resolve)·줄 메모 보내기(notes)를 작업으로 띄운다."""
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        st = RunState.load(rd)
        if not st.finished:
            raise ApiError(400, "끝난 실행에만 할 수 있습니다.")
        if kind == "notes" and not notelib.open_notes(rd):
            raise ApiError(400, "보낼 메모가 없습니다.")
        if kind == "resolve":
            t = worktree.find(wd, rd.name)
            if t is None or t.status != worktree.OPEN:
                raise ApiError(400, "열린 worktree 실행에서만 충돌을 풀 수 있습니다.")
        code = self.code_dir(rd)
        self._ensure_not_busy(code)
        argv = [kind, "--workdir", str(wd), "--run", rd.name]
        if st.config.get("mock"):
            argv.append("--mock")
        return self._spawn(kind, wd, argv, scope=code)

    def flow_notes(self, payload: dict[str, Any]) -> dict[str, Any]:
        """데스크톱 앱의 차이 메모를 이 폴더의 마지막 끝난 흐름에 붙이고 고치기 회차를 띄운다 (36.23 M39)."""
        wd = self.project(payload.get("project"))
        raw = payload.get("notes")
        if not isinstance(raw, list) or not raw:
            raise ApiError(400, "보낼 메모가 없습니다.")
        target = None
        for rd in sorted(self._run_dirs(wd), key=lambda d: d.name, reverse=True):
            try:
                st = RunState.load(rd)
            except (OSError, ValueError):
                continue
            if st.finished:
                target = (rd, st)
                break
            if self._run_live(rd):
                raise ApiError(409, "이 폴더에서 흐름이 진행 중입니다. 끝난 뒤에 보내세요.")
        if target is None:
            raise ApiError(400, "이 폴더에 끝난 흐름이 없습니다. 먼저 흐름을 돌리세요.")
        rd, st = target
        root = str(wd).replace("\\", "/").rstrip("/") + "/"
        added = 0
        for item in raw[: notelib.MAX_NOTES]:
            if not isinstance(item, dict):
                continue
            file = str(item.get("file") or "").replace("\\", "/")
            if file.lower().startswith(root.lower()):
                file = file[len(root):]
            try:
                notelib.add(rd, self.code_dir(rd), file=file, line=int(item.get("line") or 0), side=notelib.NEW,
                            text=str(item.get("text") or ""), base=st.base_commit)
                added += 1
            except (notelib.NoteError, TypeError, ValueError) as e:
                raise ApiError(400, f"메모를 붙이지 못했습니다 ({file}): {e}") from e
        job = self.followup({"project": str(wd), "run": rd.name}, "notes")
        return {**job, "run_id": rd.name, "added": added}

    def pick(self, payload: dict[str, Any]) -> dict[str, Any]:
        """경쟁 초안 중 다른 것을 고른다 (36.15). 병합 전, 끝난 실행에서만."""
        from tenure.phases.race import RaceError, pick

        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        t = worktree.find(wd, rd.name)
        if t is not None and t.status != worktree.OPEN:
            raise ApiError(400, "이미 병합하거나 버린 실행입니다.")
        code = self.code_dir(rd)
        self._ensure_not_busy(code)
        try:
            return pick(code, rd, str(payload.get("draft") or ""))
        except (RaceError, GitError) as e:
            raise ApiError(409, str(e)) from e

    # ================================================================ notes (DESIGN.md 36.14)
    def notes(self, project: str | None, run_id: str | None) -> dict[str, Any]:
        wd = self.project(project)
        rd = self.run_dir(wd, run_id)
        st = RunState.load(rd)
        return {"notes": notelib.with_stale(rd, self.code_dir(rd), st.base_commit)}

    def note_add(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        st = RunState.load(rd)
        try:
            line = int(payload.get("line") or 0)
        except (TypeError, ValueError):
            raise ApiError(400, "줄 번호가 잘못되었습니다.") from None
        try:
            note = notelib.add(rd, self.code_dir(rd), file=str(payload.get("file") or ""), line=line,
                               side=str(payload.get("side") or notelib.NEW), text=str(payload.get("text") or ""),
                               base=st.base_commit)
        except notelib.NoteError as e:
            raise ApiError(400, str(e)) from e
        return {"note": note}

    def note_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        if not notelib.remove(rd, str(payload.get("id") or "")):
            raise ApiError(404, "메모를 찾을 수 없습니다.")
        return {"ok": True}

    @staticmethod
    def _decode_attachments(raw: Any) -> list[tuple[str, bytes]]:
        """UI 가 보낸 [{name, data(base64)}] → [(안전한 이름, 바이트)] (DESIGN.md 33.3)"""
        import base64
        import binascii

        from tenure.attachments import MAX_FILES, MAX_TOTAL_BYTES, safe_name

        if raw in (None, "", []):
            return []
        if not isinstance(raw, list) or len(raw) > MAX_FILES:
            raise ApiError(400, f"첨부는 {MAX_FILES}개까지입니다.")
        out, total = [], 0
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("data"), str):
                raise ApiError(400, "첨부 형식이 잘못되었습니다.")
            try:
                data = base64.b64decode(item["data"], validate=True)
            except (binascii.Error, ValueError):
                raise ApiError(400, f"첨부를 읽을 수 없습니다: {item.get('name')}") from None
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise ApiError(400, f"첨부는 합쳐서 {MAX_TOTAL_BYTES // (1024 * 1024)}MB 까지입니다.")
            out.append((safe_name(str(item.get("name") or "file")), data))
        return out

    @staticmethod
    def _step_items(raw: Any) -> list[str]:
        """UI 의 {역할: {단계: {model, effort}}} → ["역할.단계.model=값", ...] (검증은 parse_set_step·Config.validate)"""
        if raw in (None, "", {}):
            return []
        if not isinstance(raw, dict):
            raise ApiError(400, "step_overrides 는 객체여야 합니다.")
        items: list[str] = []
        for role, steps in raw.items():
            if not isinstance(steps, dict):
                raise ApiError(400, f"step_overrides.{role} 는 객체여야 합니다.")
            for step, vals in steps.items():
                if not isinstance(vals, dict):
                    raise ApiError(400, f"step_overrides.{role}.{step} 는 객체여야 합니다.")
                for k, v in vals.items():
                    v = str(v or "").strip()
                    if v:
                        if any(c in f"{role}{step}{k}" for c in ".="):
                            raise ApiError(400, "step_overrides 의 이름에 '.' 이나 '=' 를 쓸 수 없습니다.")
                        items.append(f"{role}.{step}.{k}={v}")
        return items

    def make_report(self, payload: dict[str, Any]) -> dict[str, Any]:
        """끝난 실행의 결과 요약을 작업으로 만든다 (DESIGN.md 32.3)."""
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        st = RunState.load(rd)
        if st.status not in (DONE_APPROVED, DONE_UNAPPROVED):
            raise ApiError(400, "승인·미승인으로 끝난 실행만 요약할 수 있습니다.")
        code = self.code_dir(rd)
        self._ensure_not_busy(code)
        argv = ["report", "--workdir", str(wd), "--run", rd.name]
        if st.config.get("mock"):
            argv.append("--mock")
        return self._spawn("report", wd, argv, scope=code)

    def resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        rd = self.run_dir(wd, payload.get("run"))
        argv = ["resume", "--workdir", str(wd), "--run", rd.name]
        mr = payload.get("max_review")
        if mr not in (None, ""):
            try:
                mr = int(mr)
            except (TypeError, ValueError):
                raise ApiError(400, "max_review 는 숫자여야 합니다.") from None
            if not 1 <= mr <= MAX_REVIEW_ROUNDS:
                raise ApiError(400, f"max_review 는 1~{MAX_REVIEW_ROUNDS} 사이여야 합니다.")
            argv += ["--max-review", str(mr)]
        st = RunState.load(rd)
        code = self.code_dir(rd)
        hint = self._resume_hint(st, code)
        if not hint["possible"]:
            raise ApiError(400, hint["reason"])
        if hint["needs_max_review"] and (mr in (None, "") or int(mr) <= hint["current_max_review"]):
            raise ApiError(400, f"리뷰 상한을 {hint['current_max_review']}보다 크게 지정하세요.")
        self._ensure_not_busy(code)
        return self._spawn("resume", wd, argv, scope=code)

    def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        wd = self.project(payload.get("project"))
        if payload.get("run"):                     # worktree 실행은 그 worktree 의 잠금 (36.14)
            wd = self.code_dir(self.run_dir(wd, payload.get("run")))
        info = read_lock(wd)
        if info is None:
            raise ApiError(409, "실행 중인 tenure 가 없습니다.")
        if not _looks_like_tenure(info.pid):
            raise ApiError(409, f"pid {info.pid} 가 tenure 프로세스가 아닌 것 같아 중지하지 않았습니다.")
        if payload.get("force"):
            # 정상 중지가 듣지 않을 때. 상태는 running 으로 남아 UI 가 '멈춤'으로 보이고 resume 할 수 있다
            procs.kill_pid_tree(info.pid)
            return {"stopped": info.pid, "run_id": info.run_id, "method": "force"}
        stopflag.request(wd)                      # 모든 OS (DESIGN.md 27.3)
        signaled = procs.terminate_pid(info.pid)  # POSIX 는 SIGTERM 도 (더 빠르다)
        return {"stopped": info.pid, "run_id": info.run_id, "method": "signal" if signaled else "request"}

    def jobs_list(self, limit: int = 20) -> list[dict[str, Any]]:
        self.reap()
        d = jobs_dir()
        if not d.is_dir():
            return []
        out = []
        for path in sorted(d.glob("*.json"), reverse=True)[:limit]:
            rec = _read_job(path)
            if rec:
                out.append(rec)
        return out

    def reap(self) -> None:
        with self._jobs_lock:
            self._popens = [p for p in self._popens if p.poll() is None]

    # ================================================================ helpers
    def _spawn(self, kind: str, wd: Path, argv: list[str], scope: Path | None = None) -> dict[str, Any]:
        """scope: 이 작업이 쓰는 코드 폴더(프로젝트 또는 worktree). 같은 폴더의 작업이 겹치지 않게 본다."""
        log_dir = project_dir(wd) / UI_DIR / "jobs"
        log_dir.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        job_id = f"{stamp}-{kind}"
        log_path = log_dir / f"{job_id}.log"
        rec_dir = jobs_dir()
        rec_dir.mkdir(parents=True, exist_ok=True)
        rec_path = rec_dir / f"{job_id}.json"
        # _job 래퍼가 끝날 때 종료 코드를 .rc 에 남긴다(UI 를 다시 켜거나 원격에서도 결과를 알 수 있게)
        cmd = [*self_command(), "_job", str(rec_path.with_suffix(".rc")), "--", *argv]
        try:
            with log_path.open("ab") as log_file:
                popen = spawn_detached(cmd, wd, log_file=log_file, env={"PYTHONUNBUFFERED": "1"})
        except ExecutableNotFound as e:
            raise ApiError(500, str(e)) from e
        rec = {"id": job_id, "kind": kind, "project": str(wd), "scope": str(scope or wd), "argv": argv, "pid": popen.pid,
               "log_path": str(log_path), "started_at": dt.datetime.now().isoformat(timespec="seconds")}
        rec_path.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        with self._jobs_lock:
            self._popens.append(popen)
        return _read_job(rec_path) or {**rec, "running": True, "returncode": None, "log_tail": ""}

    def _ensure_not_busy(self, wd: Path) -> None:
        info = read_lock(wd)
        if info is not None:
            raise ApiError(409, f"이 작업 폴더에서 이미 실행 중입니다 (pid {info.pid}, 실행 {info.run_id}).")
        for j in self.jobs_list(limit=50):
            if j.get("scope", j.get("project")) == str(wd) and j["running"]:
                raise ApiError(409, "이 작업 폴더에서 UI 가 시작한 실행이 아직 끝나지 않았습니다.")

    def _live(self, wd: Path) -> dict[str, Any] | None:
        """잠금 파일 내용은 (수정 시각, 크기)로 기억하고, 프로세스가 살아 있는지는 매번 본다(멈춤·시작이 바로 보이게)."""
        def parse(path: Path) -> dict[str, Any] | None:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            pid = data.get("pid") if isinstance(data, dict) else None
            return {"pid": pid, "run_id": data.get("run_id"), "started_at": data.get("started_at")}                 if isinstance(pid, int) and not isinstance(pid, bool) else None
        def look() -> dict[str, Any] | None:
            path = self._lock_paths.get(str(wd), lambda: lock_path(wd))
            info = self._locks.get(path, parse)
            return dict(info) if info is not None and pid_alive(info["pid"]) else None
        # 한 번의 목록 요청 안에서 같은 폴더(대부분 프로젝트 하나)를 수백 번 보지 않게 아주 잠깐 기억한다 (36.38)
        info = self._live_now.get(str(wd), look)
        return dict(info) if info is not None else None

    def _run_summary(self, wd: Path, rd: Path) -> dict[str, Any]:
        try:
            b = self._base(rd)
        except (OSError, ValueError, TypeError) as e:
            return {"run_id": rd.name, "status": "broken", "error": str(e)}
        is_live = self._run_live(rd) is not None
        tree = self._tree(wd, b["run_id"]) if b["worktree"] else None
        out = {k: b[k] for k in SUMMARY_KEYS}
        out.update(live=is_live, stale=b["status"] == RUNNING and not is_live, activity=self._activity(rd, b, is_live),
                   worktree={"branch": tree.branch, "status": tree.status, "base_branch": tree.base_branch} if tree else None,
                   tokens=sum(int(u.get("input_tokens") or 0) for u in b["usage"].values()), race=b["race"])
        if is_live:                              # 지금 하는 일 (작업대의 두 번째 줄, 36.21)
            ev = self._last_event(rd) or {}
            out["doing"] = {k: ev.get(k) for k in ("kind", "agent", "role", "step", "round", "text", "ts") if ev.get(k) is not None}
        return out

    def works(self, per_project: int = 15) -> dict[str, Any]:
        """작업대의 목록 (36.21): 프로젝트마다 최근 실행 요약과 상태별 개수. 벤치마크 결과 폴더는 뺀다."""
        groups: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for path in self.registry.list():
            wd = Path(path)
            if not wd.is_dir() or bench_root_of(wd) is not None:
                continue
            items = [self._run_summary(wd, rd) for rd in list(reversed(self._run_dirs(wd)))[:per_project]]
            items = [it for it in items
                     if (it.get("worktree") or {}).get("status") != worktree.DISCARDED and not it.get("archived_at")]
            for it in items:
                st = (it.get("activity") or {}).get("state")
                if st:
                    counts[st] = counts.get(st, 0) + 1
            groups.append({"path": str(wd), "name": wd.name or str(wd), "items": items})
        groups.sort(key=lambda g: max([str(i.get("updated_at") or "") for i in g["items"]] or [""]), reverse=True)
        return {"groups": groups, "counts": counts, "now": dt.datetime.now().isoformat(timespec="seconds")}

    def _activity(self, rd: Path, st: RunState | dict, is_live: bool) -> dict[str, Any]:
        """화면이 정렬·표시할 활동 상태 (DESIGN.md 36.3.1)."""
        ev = self._last_event(rd)
        # 활동 판단에 필요한 칸만 넘긴다 (전체 to_dict 는 호출 기록까지 깊은 복사라 느렸다, 36.18)
        g = st.get if isinstance(st, dict) else (lambda k: getattr(st, k))
        light = {"status": g("status"), "seen_at": g("seen_at"), "limit_wait": g("limit_wait"), "error": g("error")}
        return run_activity(light, live=is_live, last_ts=(ev or {}).get("ts") or g("updated_at"), legacy=ev is None)

    def _base(self, rd: Path) -> dict[str, Any]:
        """목록·받은편지함·사용량이 쓰는 실행 요약. 전체 상태(호출 기록 등)는 기억하지 않는다."""
        def load(_p: Path) -> dict[str, Any]:
            st = RunState.load(rd)
            return {"run_id": st.run_id, "status": st.status, "phase": st.phase, "step": st.step, "round": st.round,
                    "task": st.task[:300], "created_at": st.created_at, "updated_at": st.updated_at,
                    "calls": len(st.calls), "total_sec": round(sum(c.sec for c in st.calls), 1),
                    "last_verdict": st.last_verdict, "config": st.config, "error": st.error,
                    "seen_at": st.seen_at, "limit_wait": st.limit_wait, "worktree": bool(st.worktree),
                    "usage": st.usage_totals(), "race": bool(st.race), "archived_at": st.archived_at}
        return self._bases.get(rd / "run.json", load)

    # ---------------------------------------------------------------- 읽기 캐시 (36.18)
    def _state(self, rd: Path) -> RunState:
        """run.json 이 그대로면 기억한 RunState. 읽기 전용으로 쓴다(고쳐서 저장할 때는 RunState.load)."""
        return self._states.get(rd / "run.json", lambda _p: RunState.load(rd))

    def _last_event(self, rd: Path) -> dict[str, Any] | None:
        return self._events.get(rd / "events.jsonl", lambda _p: last_event(rd))

    def _run_dirs(self, project: Path) -> list[Path]:
        """worktree.all_run_dirs 와 같은 결과를 폴더 목록 캐시로."""
        dirs = list(self._dirs.list(project))
        seen = {d.name for d in dirs}
        for t in self._trees(project):
            if t.run_id in seen:
                continue
            for d in self._dirs.list(Path(t.path)):
                if d.name == t.run_id:
                    dirs.append(d)
                    seen.add(d.name)
        return sorted(dirs, key=lambda d: d.name)

    def _trees(self, project: Path) -> list:
        return self._indexes.get(project_dir(project) / worktree.INDEX_FILE, lambda _p: worktree.load_index(project))

    def _tree(self, project: Path, run_id: str):
        return next((t for t in self._trees(project) if t.run_id == run_id), None)

    def _resume_hint(self, st: RunState, wd: Path) -> dict[str, Any]:
        cur = int(st.config.get("max_review") or 0)
        if st.stop_reason == "picked":           # 사람이 다른 초안을 고름 (36.15)
            from tenure.orchestrator import PICKED_NO_RESUME
            return {"possible": False, "needs_max_review": False, "current_max_review": cur, "reason": PICKED_NO_RESUME}
        if st.status == DONE_UNAPPROVED and st.stop_reason in ("stalled", "arbiter_final") and st.round < cur:
            live = self._live(wd)
            if live and live.get("run_id") == st.run_id:
                return {"possible": False, "needs_max_review": False, "current_max_review": cur, "reason": "지금 실행 중입니다."}
            return {"possible": True, "needs_max_review": False, "current_max_review": cur, "reason": ""}
        if st.status == DONE_UNAPPROVED:
            return {"possible": cur < MAX_REVIEW_ROUNDS, "needs_max_review": True, "current_max_review": cur,
                    "reason": "리뷰 상한이 최대치입니다." if cur >= MAX_REVIEW_ROUNDS else ""}
        if st.finished:
            return {"possible": False, "needs_max_review": False, "current_max_review": cur,
                    "reason": "이미 끝난 실행입니다."}
        live = self._live(wd)
        if live and live.get("run_id") == st.run_id:
            return {"possible": False, "needs_max_review": False, "current_max_review": cur,
                    "reason": "지금 실행 중입니다."}
        return {"possible": True, "needs_max_review": False, "current_max_review": cur, "reason": ""}

    def _defaults(self, wd: Path | None) -> dict[str, Any]:
        try:
            cfg = load_config(wd if wd is not None and wd.is_dir() else None)
        except ConfigError as e:
            return {"error": str(e)}
        return {"workflow": cfg.workflow, "implementer": cfg.implementer, "reviewer": cfg.reviewer,
                "synth": cfg.synth_name,
                "debate_rounds": cfg.debate_rounds, "max_review": cfg.max_review,
                # 단계별 강도가 있으면 effort 를 비워 둔다: 화면이 그 값을 다시 넘기면 단계별 설정이 지워진다
                # 단계별 모델이 있으면 model 도 비워 둔다(같은 이유, DESIGN.md 30.2)
                **{n: {"model": "" if getattr(cfg, n).step_model else getattr(cfg, n).model,
                       "model_base": getattr(cfg, n).model, "step_model": dict(getattr(cfg, n).step_model),
                       "effort": "" if getattr(cfg, n).step_effort else getattr(cfg, n).effort,
                       "effort_label": describe_effort(getattr(cfg, n)),
                       # 순서도용 실제 값 (effort 칸은 위처럼 비워 두지만 그림에는 단계별 강도를 보인다)
                       "effort_base": getattr(cfg, n).effort, "step_effort": dict(getattr(cfg, n).step_effort)}
                   for n in ("claude", "codex")},
                "test_command": cfg.test_command, "block_severity": cfg.block_severity,
                "stall_rounds": cfg.stall_rounds, "require_tests_pass": cfg.require_tests_pass,
                "review_diff": cfg.review_diff, "repro_tests": cfg.repro_tests,
                "limits": {"debate_rounds": MAX_DEBATE_ROUNDS, "max_review": MAX_REVIEW_ROUNDS}}

    # ================================================================ 작업공간 파일·git (36.22)
    def ws_dir(self, project: str | None, run_id: str | None) -> Path:
        """작업공간 폴더: 프로젝트 자체, 또는 그 실행의 코드 폴더(worktree)."""
        wd = self.project(project)
        return self.code_dir(self.run_dir(wd, run_id)) if run_id else wd

    def ws_call(self, fn: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except fileslib.FilesError as e:
            raise ApiError(400, str(e)) from e

    def limits(self) -> dict[str, Any]:
        """아래 상태 막대: 구독 한도 사용률과 동시 에이전트 자리 (무거운 사용량 합산은 하지 않는다)."""
        limit = 0
        for p in self.registry.list()[:1]:
            try:
                limit = load_config(Path(p)).max_agents
            except (ConfigError, OSError):
                pass
        return {"codex": codex_rate_limits(), "claude": load_claude_limits(),
                "slots": {"busy": slots.in_use(64), "max": slots.resolve(limit)}}

    # ================================================================ usage
    def usage_summary(self) -> dict[str, Any]:
        projects = []
        by_agent: dict[str, dict] = {}
        by_day: dict[str, dict[str, dict]] = {}
        for p in self.registry.list():
            wd = Path(p)
            if not wd.is_dir():
                continue
            proj_total: dict[str, dict] = {}
            for rd in self._run_dirs(wd):
                try:
                    b = self._base(rd)
                except (OSError, ValueError, TypeError):
                    continue
                day = (b["created_at"] or "")[:10]
                for agent, u in b["usage"].items():
                    for bucket in (proj_total.setdefault(agent, {}), by_agent.setdefault(agent, {}),
                                   by_day.setdefault(day, {}).setdefault(agent, {})):
                        for k, v in u.items():
                            bucket[k] = round(bucket.get(k, 0) + v, 4) if isinstance(v, float) else bucket.get(k, 0) + v
            if proj_total:
                projects.append({"path": p, "name": wd.name, "by_agent": proj_total})
        return {
            "codex_limits": codex_rate_limits(),
            "claude_limits": load_claude_limits(),     # tenure 가 Claude 를 부를 때 갱신 (DESIGN.md 28.6)
            "claude_local": claude_local_usage(),
            "tenure": {"by_agent": by_agent, "by_day": dict(sorted(by_day.items(), reverse=True)[:14]),
                     "projects": projects},
        }

    def preflight(self, project: str | None, workflow: str | None) -> dict[str, Any]:
        """실제 실행 전에 맡을 CLI 의 설치·로그인 상태와 최근 '로그인 필요' 멈춤 (DESIGN.md 36.37 M52).
        알려 주기만 하고 막지는 않는다. 로그인 확인은 프롬프트를 보내지 않는 status 명령뿐."""
        wd: Path | None = None
        if project:
            try:
                wd = _workflow_api().workdir_of(self, project)
            except ApiError:
                wd = None
        agents = list(AGENT_NAMES)
        if workflow and workflow not in ("auto", CLASSIC_WORKFLOW):
            try:
                wf = load_workflow(workflow, wd, load_config(wd))
                agents = [a for a in AGENT_NAMES if a in {wf.roles[r].agent for r in wf.used_roles()}]
            except (WorkflowError, ConfigError):
                pass
        last: dict[str, tuple[str, str | None]] = {}       # CLI → (실행, 마지막 호출의 이유)
        for rd in (list(reversed(self._run_dirs(wd)))[:5] if wd is not None else []):
            try:
                calls = self._state(rd).calls
            except (OSError, ValueError, TypeError):
                continue
            for c in reversed(calls):
                last.setdefault(c.agent, (rd.name, c.reason))
        out = []
        for a in agents:
            c = self._logins.get(a, lambda a=a: login_check(a))
            ok = c.status == "ok" and bool(c.extra.get("logged_in"))
            run_id, reason = last.get(a, ("", None))
            if c.status == "missing":
                warn = f"{c.name} 가 설치되어 있지 않습니다. 이대로 시작하면 {a} 차례에서 멈춥니다."
            elif not ok:
                warn = f"{c.name} 에 로그인되어 있지 않습니다. 이대로 시작하면 {a} 차례에서 멈춥니다."
            elif reason == "로그인 필요":
                warn = (f"최근 플로({run_id})에서 {c.name} 가 로그인이 필요하다며 멈췄습니다. "
                        "로그인이 만료됐을 수 있으니 다시 로그인한 뒤 시작하세요.")
            else:
                warn = None
            out.append({"agent": a, "logged_in": ok, "warning": warn})
        return {"agents": out, "warnings": [x["warning"] for x in out if x["warning"]]}

    def doctor(self) -> dict[str, Any]:
        checks = run_checks()
        return {"checks": checks, "ready": ready(checks)}

    def _verdict(self, path: Path) -> dict[str, Any]:
        key = (str(path), path.stat().st_mtime)
        if key not in self._verdict_cache:
            if len(self._verdict_cache) >= MAX_VERDICT_CACHE:     # 크기 상한 (36.18)
                self._verdict_cache.clear()
            v = parse_verdict(split_header(path.read_text(encoding="utf-8", errors="replace"))[1])
            self._verdict_cache[key] = {"status": v.status.value, "must_fix": v.must_fix, "source": v.source,
                                        "parse_failed": v.parse_failed, "contradiction": v.contradiction}
        return self._verdict_cache[key]


def _looks_like_tenure(pid: int) -> bool:
    return procs.looks_like_tenure(pid)


def bench_root_of(path: Path) -> Path | None:
    """벤치마크 실행의 작업 폴더(<결과>/<과제>/<워크플로-반복>)면 결과 폴더 (DESIGN.md 28.2)."""
    root = path.parent.parent
    return root if (root / "results.json").is_file() else None


def _display_names(items: list[dict[str, Any]]) -> None:
    """이름이 같으면 부모 폴더 이름을 붙여 구별한다(ledger / lean-1). Windows 경로도 Path 로 다룬다."""
    counts: dict[str, int] = {}
    for it in items:
        counts[it["name"]] = counts.get(it["name"], 0) + 1
    for it in items:
        parent = Path(it["path"]).parent.name
        it["display"] = f"{parent} / {it['name']}" if counts[it["name"]] > 1 and parent else it["name"]


_CALL_START_RE = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\] (\w+) ← (\w+)(?: \(재시도 (\d+)\))? 실행 중")
_CALL_END_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] (\w+) ← (\w+)(?: \(재시도 \d+\))? (완료|실패)")


def current_call(console: Path, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """console.log 끝부분에서 '지금 진행 중인 호출'을 찾는다."""
    try:
        with console.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16_384))
            tail = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        if _CALL_END_RE.match(line):
            return None
        m = _CALL_START_RE.match(line)
        if m:
            hh, mm, ss, agent, step, retry = m.groups()
            now = now or dt.datetime.now()
            started = now.replace(hour=int(hh), minute=int(mm), second=int(ss), microsecond=0)
            elapsed = (now - started).total_seconds()
            if elapsed < 0:  # 자정을 넘김
                elapsed += 86_400
            return {"agent": agent, "step": step, "retry": int(retry or 0),
                    "started": f"{hh}:{mm}:{ss}", "elapsed_sec": int(elapsed)}
    return None


# ====================================================================== 폴더 고르기 (DESIGN.md 28.9)
FS_MAX_ENTRIES = 500
_FILE_ATTRIBUTE_HIDDEN, _FILE_ATTRIBUTE_SYSTEM = 0x2, 0x4


def _fs_roots() -> list[dict[str, str]]:
    roots = [{"name": "홈", "path": str(Path.home())}]
    if os.name == "nt":
        import string

        roots += [{"name": f"{c}:", "path": f"{c}:\\"} for c in string.ascii_uppercase if os.path.isdir(f"{c}:\\")]
    else:
        roots.append({"name": "/", "path": "/"})
    return roots


def _visible_dir(entry: os.DirEntry) -> bool:
    if entry.name.startswith(".") or entry.name.startswith("$"):
        return False
    try:
        if not entry.is_dir(follow_symlinks=True):
            return False
        attrs = getattr(entry.stat(follow_symlinks=True), "st_file_attributes", 0)
    except OSError:
        return False
    return not attrs & (_FILE_ATTRIBUTE_HIDDEN | _FILE_ATTRIBUTE_SYSTEM)


def list_dirs(path: Any) -> dict[str, Any]:
    """폴더 고르기 창용: 하위 폴더 이름만 (파일 내용은 읽지 않는다)."""
    raw = str(path or "").strip()
    base = {"roots": _fs_roots(), "sep": os.sep, "home": str(Path.home())}
    if not raw:
        return {**base, "path": None, "parent": None, "entries": [], "truncated": False}
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ApiError(400, f"절대 경로여야 합니다: {raw}")
    try:
        p = p.resolve()
    except OSError:
        pass
    if not p.is_dir():
        raise ApiError(404, f"없는 폴더입니다: {p}")
    try:
        with os.scandir(p) as it:
            found = [e for e in it if _visible_dir(e)]
    except OSError as e:
        raise ApiError(403, f"열 수 없는 폴더입니다: {p} ({e.strerror or e})") from None
    found.sort(key=lambda e: e.name.lower())
    entries = []
    for e in found[:FS_MAX_ENTRIES]:
        full = Path(e.path)
        entries.append({"name": e.name, "path": str(full), "git": (full / ".git").exists(),
                        "tenure": (full / TENURE_DIR).is_dir() or (full / LEGACY_DIR).is_dir()})
    parent = str(p.parent) if p.parent != p else None
    return {**base, "path": str(p), "parent": parent, "entries": entries, "truncated": len(found) > FS_MAX_ENTRIES}


# ====================================================================== API 메서드 표 (로컬 HTTP 와 원격 _api 가 같이 씀)
def _req(args: dict[str, Any], key: str) -> Any:
    return args.get(key)


API_METHODS: dict[str, Any] = {
    "projects": lambda d, a: {"projects": d.projects(), "jobs": d.jobs_list()},
    "add_project": lambda d, a: d.add_project(a.get("path")),
    "remove_project": lambda d, a: d.remove_project(a.get("project")),
    "prune_projects": lambda d, a: d.prune_projects(),
    "bench_list": lambda d, a: d.bench_list(),
    "bench_result": lambda d, a: d.bench_result(a.get("dir")),
    "runs": lambda d, a: d.runs(a.get("project")),
    "run": lambda d, a: d.run(a.get("project"), a.get("run")),
    "entry": lambda d, a: d.entry(a.get("project"), a.get("run"), a.get("file")),
    "console": lambda d, a: d.console(a.get("project"), a.get("run"), int(a.get("offset") or 0)),
    "design": lambda d, a: d.design(a.get("project"), a.get("run"), a.get("which") or "current"),
    "commits": lambda d, a: d.commits(a.get("project"), a.get("run")),
    "diff": lambda d, a: d.diff(a.get("project"), a.get("run"), a.get("commit") or None, a.get("draft") or None),
    "jobs": lambda d, a: {"jobs": d.jobs_list()},
    "start_run": lambda d, a: d.start_run(a),
    "resume": lambda d, a: d.resume(a),
    "make_report": lambda d, a: d.make_report(a),
    "stop": lambda d, a: d.stop(a),
    "usage": lambda d, a: d.usage_summary(),
    "activity": lambda d, a: d.activity(a.get("project"), a.get("run")),
    "doctor": lambda d, a: d.doctor(),
    "preflight": lambda d, a: d.preflight(a.get("project"), a.get("workflow")),
    "defaults": lambda d, a: d._defaults(None),
    "workflows": lambda d, a: d.workflows(a.get("project")),
    "list_dirs": lambda d, a: list_dirs(a.get("path")),
    "events": lambda d, a: d.events(a.get("project"), a.get("run"), int(a.get("since") or 0)),
    "mark_seen": lambda d, a: d.mark_seen(a.get("project"), a.get("run")),
    "archive_run": lambda d, a: d.archive_run(a),
    "delete_run": lambda d, a: d.delete_run(a),
    "inbox": lambda d, a: d.inbox(),
    "works": lambda d, a: d.works(),
    "merge_check": lambda d, a: d.merge_check(a.get("project"), a.get("run")),
    "merge": lambda d, a: d.merge(a),
    "discard": lambda d, a: d.discard(a),
    "resolve": lambda d, a: d.followup(a, "resolve"),
    "notes": lambda d, a: d.notes(a.get("project"), a.get("run")),
    "note_add": lambda d, a: d.note_add(a),
    "note_delete": lambda d, a: d.note_delete(a),
    "notes_send": lambda d, a: d.followup(a, "notes"),
    "pick": lambda d, a: d.pick(a),
    "flow_notes": lambda d, a: d.flow_notes(a),
    "ws_tree": lambda d, a: d.ws_call(fileslib.tree, d.ws_dir(a.get("project"), a.get("run")), a.get("path")),
    "ws_file": lambda d, a: d.ws_call(fileslib.read_file, d.ws_dir(a.get("project"), a.get("run")), a.get("path") or ""),
    "ws_status": lambda d, a: d.ws_call(fileslib.status, d.ws_dir(a.get("project"), a.get("run"))),
    "ws_diff": lambda d, a: d.ws_call(fileslib.file_diff, d.ws_dir(a.get("project"), a.get("run")), a.get("path") or ""),
    "limits": lambda d, a: d.limits(),
    # 워크플로 노드 편집기 (DESIGN.md 36.29 M44). workflow_api 가 이 모듈의 ApiError 를 쓰므로 부를 때 읽는다.
    "workflow_get": lambda d, a: _workflow_api().workflow_get(d, a.get("project"), a.get("name")),
    "workflow_check": lambda d, a: _workflow_api().workflow_check(d, a),
    "workflow_save": lambda d, a: _workflow_api().workflow_save(d, a),
}


def _workflow_api():
    from tenure.ui import workflow_api

    return workflow_api


def dispatch(d: Dashboard, method: str, args: dict[str, Any]) -> Any:
    fn = API_METHODS.get(method)
    if fn is None:
        raise ApiError(404, f"없는 API 입니다: {method}")
    return fn(d, args or {})
