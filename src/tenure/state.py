"""RunState — 실행 상태 직렬화. 각 스텝이 끝날 때마다 run.json 에 저장해 resume 에 쓴다."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from tenure.procs import replace_file

RUN_JSON = "run.json"
TENURE_DIR = ".tenure"
LEGACY_DIR = ".duet"           # 옛 이름 (DESIGN.md 36.11)
RUNS_DIR = "runs"


def project_dir(workdir) -> Path:
    """작업 폴더의 tenure 기록 폴더. 옛 .duet/ 만 있으면 .tenure/ 로 옮긴다."""
    from tenure.legacy import migrate_dir

    wd = Path(workdir)
    return migrate_dir(wd / TENURE_DIR, wd / LEGACY_DIR)

# status 값
RUNNING = "running"
DONE_APPROVED = "done_approved"
DONE_UNAPPROVED = "done_unapproved"
DONE_DESIGN = "done_design"          # --design-only 로 설계만 끝냄
STOPPED_RATE_LIMIT = "stopped_rate_limit"
STOPPED_USER = "stopped_user"        # Ctrl-C 또는 UI 의 중지(SIGTERM)
FAILED = "failed"
FINAL_STATUSES = frozenset({DONE_APPROVED, DONE_UNAPPROVED, DONE_DESIGN})

# phase 값
PHASE_INIT = "init"
PHASE_DEBATE = "debate"
PHASE_BUILD = "build"
PHASE_EXPLORE = "explore"
PHASE_AUDIT = "audit"
PHASE_DONE = "done"


@dataclass
class CallRecord:
    agent: str
    step: str
    sec: float
    exit: int
    rate_limited: bool = False
    timed_out: bool = False
    file: str | None = None
    usage: dict | None = None
    role: str | None = None   # 워크플로 역할 이름 (agent 와 다를 때만)
    outcome: str | None = None  # 단계 결과 done|blocked|failed|unconfirmed (DESIGN.md 36.3.2)
    reason: str | None = None


@dataclass
class RunState:
    run_id: str
    task: str
    config: dict[str, Any] = field(default_factory=dict)
    phase: str = PHASE_INIT
    step: str = ""
    round: int = 0
    status: str = RUNNING
    base_commit: str | None = None
    proposals: dict[str, str] = field(default_factory=dict)   # agent -> 최초 설계안 파일
    revisions: dict[str, str] = field(default_factory=dict)   # agent -> 최신 개정안 파일
    critiques: list[str] = field(default_factory=list)        # 비판 파일(순서대로)
    final_design: str | None = None
    last_impl: str | None = None
    last_review: str | None = None
    last_verdict: str | None = None
    done: list[str] = field(default_factory=list)             # 완료한 스텝 키
    tests: dict[str, dict] = field(default_factory=dict)      # round → tenure 가 돌린 테스트 결과
    review_history: list[dict] = field(default_factory=list)  # 라운드별 판정·막는 지적 수
    stop_reason: str | None = None                            # "max_review" | "stalled"
    stall_baseline_round: int = 0                             # resume 때 수렴 판단을 이 라운드부터 다시
    reviewed_commits: dict[str, str] = field(default_factory=dict)  # round → 리뷰한 시점의 HEAD (증분 diff 기준)
    repros: dict[str, dict] = field(default_factory=dict)     # round → 리뷰어가 낸 재현 테스트 처리 결과
    calls: list[CallRecord] = field(default_factory=list)
    # 워크플로 템플릿 (DESIGN.md 19장). None 이면 classic(설정에서 만듦)
    workflow: dict | None = None
    last_reviews: dict[str, str] = field(default_factory=dict)        # 역할 → 마지막 리뷰 파일 (리뷰어 여럿)
    round_reviews: dict[str, dict] = field(default_factory=dict)      # round → 역할 → 리뷰어별 판정 (리뷰어 여럿)
    explores: dict[str, str] = field(default_factory=dict)            # explore 단계 id → 결과 파일
    audits: dict[str, list] = field(default_factory=dict)             # audit 단계 id → 감사자별 결과
    issues: list[dict] = field(default_factory=list)                  # 지적 원장 (DESIGN.md 22.1)
    arbiter: dict | None = None                                       # 중재 결과 (DESIGN.md 23장)
    report: str | None = None                                         # 결과 요약 기록 파일 (DESIGN.md 32장)
    attachments: list[str] = field(default_factory=list)              # 첨부 파일 (실행 폴더 기준, DESIGN.md 33.3)
    browser: dict = field(default_factory=dict)                       # 라운드 → 브라우저 확인 기록 (DESIGN.md 33.4)
    test_guard: dict[str, dict] = field(default_factory=dict)         # round → 테스트 변경 경고 (DESIGN.md 24.1)
    route: dict | None = None                                         # auto 가 고른 워크플로와 이유 (36.5)
    seen_at: str | None = None                                        # 화면에서 끝난 실행을 연 시각 (36.3.1)
    archived_at: str | None = None                                    # 플로 목록에서 보관한 시각 (36.37)
    race: dict | None = None                                          # 경쟁 초안: 초안별 결과·승자·이유 (36.15)
    followups: list[dict] = field(default_factory=list)              # 끝난 뒤 덧붙인 라운드(충돌 해결·줄 메모, 36.14)
    worktree: dict | None = None                                      # worktree 에서 돌면 {project, branch, base_branch, base_commit} (36.14)
    limit_wait: dict | None = None                                    # 한도 대기 중이면 {agent, step, until, reason} (25.1)
    limit_waited_sec: float = 0.0                                     # 한도 대기에 쓴 시간 합
    dedup: dict[str, dict] = field(default_factory=dict)              # "round:역할" → 재제기 확인 결과 (DESIGN.md 26.2)
    reverts: dict[str, dict] = field(default_factory=dict)            # round → 되돌림 경고 (DESIGN.md 26.3)
    review_sessions: dict[str, dict] = field(default_factory=dict)    # 역할 → {id, round, rounds} (DESIGN.md 21.3)
    graph: dict = field(default_factory=dict)                         # 그래프 워크플로: 분기 결정·추가 수정 상한 (36.30)
    design_doc: str | None = None    # 설계 문서(작업 폴더 기준). None 이면 DESIGN.md, 변경 설계면 docs/changes/<run>.md
    error: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    updated_at: str = ""

    # ---------------------------------------------------------------- steps
    def is_done(self, key: str) -> bool:
        return key in self.done

    def mark_done(self, key: str) -> None:
        if key not in self.done:
            self.done.append(key)

    def record_call(self, agent: str, step: str, sec: float, exit_code: int, *,
                    rate_limited: bool = False, timed_out: bool = False,
                    file: str | None = None, usage: dict | None = None, role: str | None = None,
                    outcome: str | None = None, reason: str | None = None) -> None:
        self.calls.append(CallRecord(agent, step, round(sec, 1), exit_code,
                                     rate_limited=rate_limited, timed_out=timed_out, file=file, usage=usage,
                                     role=role, outcome=outcome, reason=reason or None))

    def usage_totals(self) -> dict[str, dict]:
        """에이전트별 사용량 합계."""
        from tenure.usage import add_usage

        out: dict[str, dict] = {}
        for c in self.calls:
            if c.usage:
                add_usage(out.setdefault(c.agent, {}), c.usage)
        return out

    @property
    def finished(self) -> bool:
        return self.status in FINAL_STATUSES

    @property
    def resumable(self) -> bool:
        """그대로 이어갈 수 있는 상태. done_unapproved 는 max_review 를 늘리면 이어갈 수 있다(Orchestrator 가 판단)."""
        return self.status in (RUNNING, STOPPED_RATE_LIMIT, STOPPED_USER, FAILED)

    # ---------------------------------------------------------------- io
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        data = dict(data)
        # 모르는 필드는 버린다(더 새 tenure 가 쓴 run.json 도 읽을 수 있게)
        call_keys = set(CallRecord.__dataclass_fields__)
        data["calls"] = [CallRecord(**{k: v for k, v in c.items() if k in call_keys}) for c in data.get("calls", [])]
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, run_dir: Path, *, touch: bool = True) -> Path:
        """touch=False: 화면의 표시만 바꿀 때(보관 등). 실행이 마지막으로 움직인 시각을 그대로 둔다."""
        if touch:
            self.updated_at = dt.datetime.now().isoformat(timespec="seconds")
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / RUN_JSON
        tmp = run_dir / (RUN_JSON + ".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        replace_file(tmp, path)
        return path

    @classmethod
    def load(cls, run_dir: Path) -> "RunState":
        path = Path(run_dir) / RUN_JSON
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


# -------------------------------------------------------------------- run dirs
def runs_root(workdir: Path) -> Path:
    return project_dir(workdir) / RUNS_DIR


def new_run_id(now: dt.datetime | None = None) -> str:
    return (now or dt.datetime.now()).strftime("%Y%m%d-%H%M%S")


def create_run_dir(workdir: Path, run_id: str | None = None) -> Path:
    root = runs_root(workdir)
    root.mkdir(parents=True, exist_ok=True)
    rid = run_id or new_run_id()
    path = root / rid
    # 같은 초에 두 번 만들면 접미사
    n = 1
    while path.exists():
        n += 1
        path = root / f"{rid}-{n}"
    path.mkdir()
    return path


def list_run_dirs(workdir: Path) -> list[Path]:
    root = runs_root(workdir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / RUN_JSON).is_file())


def latest_run_dir(workdir: Path) -> Path | None:
    dirs = list_run_dirs(workdir)
    return dirs[-1] if dirs else None
