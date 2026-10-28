"""워크플로 템플릿: 역할군 + 단계 순서 (DESIGN.md 19장).

템플릿은 정해진 단계 종류(explore, debate, build, audit)를 순서대로 나열하고 각 단계에 역할을 붙인다.
흐름 제어·resume·종료 판단은 Orchestrator 와 각 Phase 가 맡는다.

- classic: 파일이 아니라 설정(implementer/reviewer/synth/debate_rounds/max_review)에서 만든다.
- 찾는 순서: <workdir>/.tenure/workflows/<이름>.toml → $TENURE_HOME/workflows/<이름>.toml → 내장 tenure/workflows/
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field, replace
from importlib import resources
from pathlib import Path
from typing import Any, Mapping

from tenure.config import (
    AGENT_NAMES,
    CLASSIC_WORKFLOW,
    EFFORT_STEPS,
    MAX_DEBATE_ROUNDS,
    MAX_REVIEW_ROUNDS,
    WORKFLOW_NAME_RE,
    AgentConfig,
    Config,
    _TOKEN_RE,
    apply_step_overrides,
)

ROLE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
STEP_KINDS = ("explore", "debate", "build", "audit", "branch", "continue", "parallel")
# 그래프 형식에서만 쓰는 단계 (DESIGN.md 36.30)
GRAPH_KINDS = ("branch", "continue", "parallel")
BRANCH_CHECKS = ("approved", "tests_pass", "audit_clean")
END = "end"
MAX_VISITS = 5
MAX_CONTINUE_ROUNDS = 5
MAX_PARALLEL = 4
# 역할이 프롬프트 템플릿을 바꿀 수 있는 단계
PROMPT_STEPS = ("propose", "critique", "synthesize", "implement", "fix", "review", "rereview", "explore", "audit",
                "arbitrate", "dedup", "report")
MAX_INSTRUCTIONS = 4000
MAX_REVIEWERS = 3
MAX_AUDITORS = 3
PROJECT_DIR = Path(".tenure") / "workflows"

SOURCE_LABELS = {"config": "설정", "builtin": "내장", "user": "내 템플릿", "project": "프로젝트", "file": "파일"}

_ROLE_KEYS = {"agent", "model", "effort", "step_effort", "step_model", "instructions", "templates"}
_STEP_KEYS = {
    "explore": {"kind", "id", "role", "next", "max_visits"},
    "debate": {"kind", "id", "proposers", "rounds", "synth", "output", "next"},
    "build": {"kind", "id", "implementer", "reviewers", "max_review", "arbiter", "design", "rivals", "next"},
    "audit": {"kind", "id", "roles", "next", "max_visits"},
    "branch": {"kind", "id", "check", "yes", "no", "max_visits"},
    "continue": {"kind", "id", "implementer", "reviewers", "rounds", "next", "max_visits"},
    "parallel": {"kind", "id", "branches", "next", "max_visits"},
}
_TOP_KEYS = {"name", "description", "roles", "steps"}


class WorkflowError(Exception):
    """템플릿 오류. 메시지는 사용자에게 그대로 보여준다."""


@dataclass
class Role:
    name: str
    agent: str
    model: str | None = None              # None 이면 [claude]/[codex] 설정을 따른다
    effort: str | None = None
    step_effort: dict[str, str] | None = None
    step_model: dict[str, str] | None = None
    instructions: str = ""
    templates: dict[str, str] = field(default_factory=dict)

    def agent_config(self, cfg: Config) -> AgentConfig:
        """이 역할로 CLI 를 부를 때의 설정. bin·args 는 공유하고 모델·강도만 바꾼다(18.1 층 규칙)."""
        base = cfg.agent(self.agent)
        changes: dict[str, Any] = {}
        if self.model is not None:
            changes["model"] = self.model
            changes["step_model"] = dict(self.step_model or {})   # 모델을 정한 역할은 에이전트의 단계별 모델을 안 물려받음 (30.2)
        elif self.step_model is not None:
            changes["step_model"] = dict(self.step_model)
        if self.effort is not None:
            changes["effort"] = self.effort
            changes["step_effort"] = dict(self.step_effort or {})
        elif self.step_effort is not None:
            changes["step_effort"] = dict(self.step_effort)
        return apply_step_overrides(replace(base, **changes), cfg, self.name)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"agent": self.agent}
        for key in ("model", "effort", "step_effort", "step_model"):
            if getattr(self, key) is not None:
                d[key] = getattr(self, key)
        if self.instructions:
            d["instructions"] = self.instructions
        if self.templates:
            d["templates"] = dict(self.templates)
        return d


@dataclass
class Step:
    kind: str
    id: str
    role: str | None = None                                  # explore
    proposers: list[str] = field(default_factory=list)       # debate
    synth: str | None = None
    rounds: int | None = None
    output: str | None = None                                # None/"design": DESIGN.md, "change": 변경 설계 (19.5)
    implementer: str | None = None                           # build
    reviewers: list[str] = field(default_factory=list)
    max_review: int | None = None
    arbiter: str | None = None                               # 분쟁 중재자 역할 (DESIGN.md 23.2)
    design: str | None = None                                # build: None/"doc" = DESIGN.md, "task" = 과제 원문을 설계로 (36.5)
    rivals: list[str] = field(default_factory=list)          # build: 경쟁 초안의 맞수 (36.15)
    roles: list[str] = field(default_factory=list)           # audit
    # 그래프 형식 (DESIGN.md 36.30): 다음 단계, 갈림 조건·가지, 나란히 가지, 최대 반복
    next: str | None = None
    check: str | None = None
    yes: str | None = None
    no: str | None = None
    branches: list[str] = field(default_factory=list)
    max_visits: int | None = None

    def role_names(self) -> list[str]:
        names = [*([self.role] if self.role else []), *self.proposers, *([self.synth] if self.synth else []),
                 *([self.implementer] if self.implementer else []), *self.rivals, *self.reviewers,
                 *([self.arbiter] if self.arbiter else []), *self.roles]
        return list(dict.fromkeys(names))

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"kind": self.kind, "id": self.id}
        for key in sorted(_STEP_KEYS[self.kind] - {"kind", "id"}):
            v = getattr(self, key)
            if v not in (None, []):
                d[key] = v
        return d

    def describe(self) -> str:
        if self.kind == "explore":
            return f"explore({self.role})"
        if self.kind == "debate":
            parts = ", ".join(self.proposers)
            if self.synth:
                parts += f" → {self.synth}"
            if self.rounds is not None:
                parts += f", {self.rounds}R"
            if self.output == "change":
                parts += ", 변경 설계"
            return f"debate({parts})"
        if self.kind == "build":
            extra = f", 최대 {self.max_review}회" if self.max_review is not None else ""
            if self.arbiter:
                extra += f", 중재 {self.arbiter}"
            if self.rivals:
                return f"race({self.implementer} 대 {', '.join(self.rivals)}; 서로 리뷰 → 판정 → 승자가 이어서{extra})"
            return f"build({self.implementer}; 리뷰 {', '.join(self.reviewers)}{extra})"
        if self.kind == "audit":
            return f"audit({', '.join(self.roles)})"
        if self.kind == "branch":
            return f"branch({self.check}? {self.yes or END} : {self.no or END})"
        if self.kind == "continue":
            return f"continue({self.implementer}; 리뷰 {', '.join(self.reviewers)}, {self.rounds}회)"
        return f"parallel({', '.join(self.branches)})"


@dataclass
class Workflow:
    name: str
    description: str
    roles: dict[str, Role]
    steps: list[Step]
    source: str = "builtin"
    path: str = ""

    @property
    def is_classic(self) -> bool:
        return self.source == "config"

    @property
    def is_graph(self) -> bool:
        return is_graph_steps(self.steps)

    def first(self, kind: str) -> Step | None:
        return next((s for s in self.steps if s.kind == kind), None)

    def role_agents(self) -> dict[str, str]:
        return {name: r.agent for name, r in self.roles.items()}

    def used_roles(self) -> list[str]:
        out: list[str] = []
        for s in self.steps:
            out += [n for n in s.role_names() if n not in out]
        return out

    def summary(self) -> str:
        return " → ".join(s.describe() for s in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "source": self.source,
                "roles": {n: r.to_dict() for n, r in self.roles.items()},
                "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Workflow":
        """run.json 에 저장해 둔 정의를 다시 읽는다."""
        data = dict(data)
        source = str(data.pop("source", "file"))
        return parse_workflow(data, name_hint=str(data.get("name") or "workflow"), source=source,
                              where="저장된 워크플로")


# ====================================================================== classic
def classic(cfg: Config) -> Workflow:
    """설정에서 만든 기본 워크플로. 기록 이름·resume 키가 M12 와 같다."""
    roles = {name: Role(name=name, agent=name) for name in AGENT_NAMES}
    steps = [
        Step(kind="debate", id="debate", proposers=list(AGENT_NAMES), synth=cfg.synth_name),
        Step(kind="build", id="build", implementer=cfg.implementer, reviewers=[cfg.reviewer]),
    ]
    return Workflow(CLASSIC_WORKFLOW, "설계 토론 → 구현 → 교차 리뷰 · 큰 설계 결정이 있을 때",
                    roles, steps, source="config")


# ====================================================================== parse
def parse_workflow(data: Mapping[str, Any], *, name_hint: str, source: str, where: str) -> Workflow:
    def err(msg: str) -> WorkflowError:
        return WorkflowError(f"{where}: {msg}")

    unknown = set(data) - _TOP_KEYS
    if unknown:
        raise err(f"알 수 없는 키: {', '.join(sorted(unknown))}")
    name = data.get("name", name_hint)
    description = data.get("description", "")
    if not isinstance(name, str) or not name.strip() or len(name) > 80:
        raise err("name 은 80자 이하 문자열이어야 합니다")
    if not isinstance(description, str) or len(description) > 500:
        raise err("description 은 500자 이하 문자열이어야 합니다")

    raw_roles = data.get("roles")
    if not isinstance(raw_roles, Mapping) or not raw_roles:
        raise err("[roles.<이름>] 이 하나 이상 있어야 합니다")
    roles = {rname: _parse_role(rname, rdata, err) for rname, rdata in raw_roles.items()}

    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise err("[[steps]] 가 하나 이상 있어야 합니다")
    steps = [_parse_step(i, sdata, roles, err) for i, sdata in enumerate(raw_steps, 1)]
    if is_graph_steps(steps):
        from tenure.workflow_graph import check_graph

        check_graph(steps, err)
    else:
        _check_order(steps, err)
    return Workflow(name=name.strip(), description=description.strip(), roles=roles, steps=steps, source=source)


def _parse_role(name: Any, data: Any, err) -> Role:
    if not isinstance(name, str) or not ROLE_NAME_RE.match(name):
        raise err(f"역할 이름이 잘못되었습니다: {name!r} (영문 소문자로 시작, 소문자·숫자·_ 24자 이하)")
    if not isinstance(data, Mapping):
        raise err(f"[roles.{name}] 는 테이블이어야 합니다")
    unknown = set(data) - _ROLE_KEYS
    if unknown:
        raise err(f"[roles.{name}] 에 알 수 없는 키: {', '.join(sorted(unknown))}")
    agent = data.get("agent")
    if agent not in AGENT_NAMES:
        raise err(f"[roles.{name}] agent 는 {' 또는 '.join(AGENT_NAMES)} 여야 합니다: {agent!r}")
    for key in ("model", "effort"):
        v = data.get(key)
        if v is not None and (not isinstance(v, str) or not _TOKEN_RE.match(v)):
            raise err(f"[roles.{name}] {key} 값이 잘못되었습니다: {v!r}")
    maps: dict[str, dict[str, str] | None] = {}
    for key, what in (("step_effort", "강도"), ("step_model", "모델")):
        m = data.get(key)
        if m is not None:
            if not isinstance(m, Mapping):
                raise err(f"[roles.{name}] {key} 는 {{단계 = {what}}} 테이블이어야 합니다")
            for k, v in m.items():
                if k not in EFFORT_STEPS:
                    raise err(f"[roles.{name}] {key} 의 단계가 잘못되었습니다: {k!r} (가능: {', '.join(EFFORT_STEPS)})")
                if not isinstance(v, str) or not v or not _TOKEN_RE.match(v):
                    raise err(f"[roles.{name}] {key}.{k} 값이 잘못되었습니다: {v!r}")
            m = dict(m)
        maps[key] = m
    step_effort = maps["step_effort"]
    instructions = data.get("instructions", "")
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        raise err(f"[roles.{name}] instructions 는 {MAX_INSTRUCTIONS}자 이하 문자열이어야 합니다")
    templates = data.get("templates", {})
    if not isinstance(templates, Mapping):
        raise err(f"[roles.{name}] templates 는 {{단계 = 템플릿 이름}} 테이블이어야 합니다")
    from tenure.prompts import TEMPLATE_NAME_RE

    for k, v in templates.items():
        if k not in PROMPT_STEPS:
            raise err(f"[roles.{name}] templates 의 단계가 잘못되었습니다: {k!r} (가능: {', '.join(PROMPT_STEPS)})")
        if not isinstance(v, str) or not TEMPLATE_NAME_RE.match(v):
            raise err(f"[roles.{name}] templates.{k} 이름이 잘못되었습니다: {v!r}")
    return Role(name=name, agent=agent, model=data.get("model"), effort=data.get("effort"),
                step_effort=step_effort, step_model=maps["step_model"], instructions=instructions, templates=dict(templates))


def _role_list(sdata: Mapping, key: str, label: str, roles: Mapping, err, *, lo: int, hi: int) -> list[str]:
    v = sdata.get(key)
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or not lo <= len(v) <= hi:
        raise err(f"{label}: {key} 는 역할 이름 {lo}~{hi}개의 목록이어야 합니다")
    if len(set(v)) != len(v):
        raise err(f"{label}: {key} 에 같은 역할이 두 번 있습니다")
    for x in v:
        _need_role(x, label, roles, err)
    return list(v)


def _need_role(name: Any, label: str, roles: Mapping, err) -> str:
    if not isinstance(name, str) or name not in roles:
        raise err(f"{label}: 정의되지 않은 역할: {name!r} (정의된 역할: {', '.join(roles)})")
    return name


def _int_in(sdata: Mapping, key: str, label: str, lo: int, hi: int, err) -> int | None:
    v = sdata.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise err(f"{label}: {key} 는 {lo}~{hi} 사이 정수여야 합니다: {v!r}")
    return v


def _parse_step(i: int, sdata: Any, roles: Mapping[str, Role], err) -> Step:
    if not isinstance(sdata, Mapping):
        raise err(f"{i}번째 단계는 테이블이어야 합니다")
    kind = sdata.get("kind")
    if kind not in STEP_KINDS:
        raise err(f"{i}번째 단계: kind 는 {', '.join(STEP_KINDS)} 중 하나여야 합니다: {kind!r}")
    label = f"{i}번째 단계({kind})"
    if kind == "build" and ("manager" in sdata or "workers" in sdata):
        raise err(f"{label}: 관리자·작업자 분업(manager·workers)은 없앤 기능입니다 (DESIGN.md 29장). implementer 를 쓰세요")
    unknown = set(sdata) - _STEP_KEYS[kind]
    if unknown:
        raise err(f"{label}: 알 수 없는 키: {', '.join(sorted(unknown))}")
    sid = sdata.get("id", kind)
    if not isinstance(sid, str) or not ROLE_NAME_RE.match(sid):
        raise err(f"{label}: id 가 잘못되었습니다: {sid!r} (영문 소문자로 시작, 소문자·숫자·_)")
    step = Step(kind=kind, id=sid)
    if kind == "explore":
        step.role = _need_role(sdata.get("role"), label, roles, err)
    elif kind == "debate":
        step.proposers = _role_list(sdata, "proposers", label, roles, err, lo=1, hi=2)
        step.rounds = _int_in(sdata, "rounds", label, 0, MAX_DEBATE_ROUNDS, err)
        synth = sdata.get("synth")
        if len(step.proposers) == 2:
            step.synth = _need_role(synth, label, roles, err)
        elif synth is not None:
            raise err(f"{label}: 제안자가 1명이면 synth 를 쓰지 않습니다 (그 제안이 그대로 DESIGN.md 가 됩니다)")
        elif step.rounds:
            raise err(f"{label}: 제안자가 1명이면 비판 라운드(rounds)를 쓸 수 없습니다")
        output = sdata.get("output")
        if output not in (None, "design", "change"):
            raise err(f"{label}: output 은 design 또는 change 여야 합니다: {output!r}")
        step.output = None if output in (None, "design") else output
    elif kind == "build":
        step.implementer = _need_role(sdata.get("implementer"), label, roles, err)
        lead = step.implementer
        step.reviewers = _role_list(sdata, "reviewers", label, roles, err, lo=1, hi=MAX_REVIEWERS)
        if lead in step.reviewers:
            raise err(f"{label}: 구현자({lead})는 리뷰어가 될 수 없습니다")
        step.max_review = _int_in(sdata, "max_review", label, 1, MAX_REVIEW_ROUNDS, err)
        if sdata.get("arbiter") is not None:
            step.arbiter = _need_role(sdata.get("arbiter"), label, roles, err)
            if step.arbiter == lead:
                raise err(f"{label}: 구현자({lead})는 중재자가 될 수 없습니다")
        if sdata.get("rivals") is not None:
            step.rivals = _role_list(sdata, "rivals", label, roles, err, lo=1, hi=1)
            if lead in step.rivals:
                raise err(f"{label}: 구현자({lead})는 자기 맞수가 될 수 없습니다")
        if sdata.get("design") is not None:
            if sdata["design"] not in ("doc", "task"):
                raise err(f"{label}: design 은 doc 또는 task 여야 합니다: {sdata['design']!r}")
            step.design = sdata["design"]
    elif kind == "audit":
        step.roles = _role_list(sdata, "roles", label, roles, err, lo=1, hi=MAX_AUDITORS)
    elif kind == "branch":
        check = sdata.get("check")
        if check not in BRANCH_CHECKS:
            raise err(f"{label}: check 는 {', '.join(BRANCH_CHECKS)} 중 하나여야 합니다: {check!r}")
        step.check = check
        step.yes = _target(sdata, "yes", label, err)
        step.no = _target(sdata, "no", label, err)
    elif kind == "continue":
        step.implementer = _need_role(sdata.get("implementer"), label, roles, err)
        step.reviewers = _role_list(sdata, "reviewers", label, roles, err, lo=1, hi=MAX_REVIEWERS)
        if step.implementer in step.reviewers:
            raise err(f"{label}: 구현자({step.implementer})는 리뷰어가 될 수 없습니다")
        step.rounds = _int_in(sdata, "rounds", label, 1, MAX_CONTINUE_ROUNDS, err) or 1
    else:
        v = sdata.get("branches")
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or not 2 <= len(v) <= MAX_PARALLEL:
            raise err(f"{label}: branches 는 단계 id {2}~{MAX_PARALLEL}개의 목록이어야 합니다")
        if len(set(v)) != len(v):
            raise err(f"{label}: branches 에 같은 단계가 두 번 있습니다")
        step.branches = list(v)
    if "next" in sdata:
        step.next = _target(sdata, "next", label, err)
    if "max_visits" in sdata:
        step.max_visits = _int_in(sdata, "max_visits", label, 1, MAX_VISITS, err)
    return step


def _target(sdata: Mapping, key: str, label: str, err) -> str:
    v = sdata.get(key)
    if not isinstance(v, str) or not (v == END or ROLE_NAME_RE.match(v)):
        raise err(f"{label}: {key} 는 단계 id 또는 \"{END}\" 여야 합니다: {v!r}")
    return v


def is_graph_steps(steps: list[Step]) -> bool:
    """다음 단계·갈림·나란히·반복을 쓰면 그래프 형식 (DESIGN.md 36.30)."""
    return any(s.kind in GRAPH_KINDS or s.next is not None or s.max_visits is not None for s in steps)


def _check_order(steps: list[Step], err) -> None:
    kinds = [s.kind for s in steps]
    for k in ("debate", "build"):
        if kinds.count(k) > 1:
            raise err(f"{k} 단계는 하나만 둘 수 있습니다")
    seen: set[tuple[str, str]] = set()
    for s in steps:
        if (s.kind, s.id) in seen:
            raise err(f"{s.kind} 단계의 id 가 겹칩니다: {s.id} (id 를 다르게 주세요)")
        seen.add((s.kind, s.id))
    build_at = kinds.index("build") if "build" in kinds else None
    if "debate" in kinds and build_at is not None and kinds.index("debate") > build_at:
        raise err("debate 단계는 build 앞에 있어야 합니다")
    for i, k in enumerate(kinds):
        if k == "explore" and build_at is not None and i > build_at:
            raise err("explore 단계는 build 앞에 있어야 합니다")
        if k == "audit" and (build_at is None or i < build_at):
            raise err("audit 단계는 build 뒤에 있어야 합니다")


# ====================================================================== load
def _builtin_dir():
    return resources.files("tenure").joinpath("workflows")


def _search(name: str, workdir: Path | None) -> tuple[str, str, str] | None:
    """(내용, 출처, 경로)"""
    from tenure.registry import tenure_home

    candidates: list[tuple[Path, str]] = []
    if workdir is not None:
        candidates.append((Path(workdir) / PROJECT_DIR / f"{name}.toml", "project"))
    candidates.append((tenure_home() / "workflows" / f"{name}.toml", "user"))
    for path, source in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8"), source, str(path)
    ref = _builtin_dir().joinpath(f"{name}.toml")
    if ref.is_file():
        return ref.read_text(encoding="utf-8"), "builtin", f"내장:{name}"
    return None


def _parse_text(text: str, *, name_hint: str, source: str, where: str) -> Workflow:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise WorkflowError(f"{where}: TOML 파싱 실패: {e}") from e
    return parse_workflow(data, name_hint=name_hint, source=source, where=where)


def load_workflow(spec: str, workdir: Path | None, cfg: Config) -> Workflow:
    """이름 또는 @파일경로 로 워크플로를 찾는다. classic 은 설정에서 만든다."""
    spec = (spec or CLASSIC_WORKFLOW).strip()
    if spec == CLASSIC_WORKFLOW:
        return classic(cfg)
    if spec.startswith("@"):
        path = Path(spec[1:]).expanduser()
        if not path.is_file():
            raise WorkflowError(f"워크플로 파일을 찾을 수 없습니다: {path}")
        wf = _parse_text(path.read_text(encoding="utf-8"), name_hint=path.stem, source="file", where=str(path))
        wf.path = str(path.resolve())
    else:
        if not WORKFLOW_NAME_RE.match(spec):
            raise WorkflowError(f"워크플로 이름이 잘못되었습니다: {spec!r}")
        found = _search(spec, workdir)
        if found is None:
            names = ", ".join(w["id"] for w in list_workflows(workdir))
            raise WorkflowError(f"워크플로를 찾을 수 없습니다: {spec} (쓸 수 있는 것: {names})")
        text, source, path_s = found
        wf = _parse_text(text, name_hint=spec, source=source, where=path_s)
        wf.path = path_s
    check_templates(wf, workdir)
    return wf


def check_templates(wf: Workflow, workdir: Path | None) -> None:
    from tenure.prompts import template_exists

    for role in wf.roles.values():
        for step, name in role.templates.items():
            if not template_exists(name, workdir):
                raise WorkflowError(f"역할 {role.name} 의 {step} 템플릿을 찾을 수 없습니다: {name}")


def list_workflows(workdir: Path | None, cfg: Config | None = None) -> list[dict[str, Any]]:
    """쓸 수 있는 워크플로. 같은 이름이면 앞(프로젝트 > 내 템플릿 > 내장)이 가린다. 잘못된 파일은 error 로 표시."""
    from tenure.registry import tenure_home

    names: dict[str, None] = {CLASSIC_WORKFLOW: None}
    dirs: list[Path] = []
    if workdir is not None:
        dirs.append(Path(workdir) / PROJECT_DIR)
    dirs.append(tenure_home() / "workflows")
    for d in dirs:
        if d.is_dir():
            for p in sorted(d.glob("*.toml")):
                if WORKFLOW_NAME_RE.match(p.stem):
                    names.setdefault(p.stem, None)
    for ref in sorted(_builtin_dir().iterdir(), key=lambda r: r.name):
        if ref.name.endswith(".toml") and WORKFLOW_NAME_RE.match(ref.name[:-5]):
            names.setdefault(ref.name[:-5], None)
    out = []
    for name in names:
        try:
            wf = load_workflow(name, workdir, cfg or Config())
        except WorkflowError as e:
            out.append({"id": name, "name": name, "source": "error", "error": str(e)})
            continue
        out.append(describe(wf, name))
    return out


def describe(wf: Workflow, wid: str | None = None) -> dict[str, Any]:
    return {
        "id": wid or wf.name, "name": wf.name, "description": wf.description, "source": wf.source,
        "source_label": SOURCE_LABELS.get(wf.source, wf.source), "path": wf.path,
        "summary": wf.summary(),
        "roles": [{"name": r.name, "agent": r.agent, "model": r.model or "", "effort": r.effort or "",
                   # None(설정을 따름)과 {}(단계별 없음)를 구별한다: 새 실행 창의 순서도가 강도를 계산한다
                   "step_effort": dict(r.step_effort) if r.step_effort is not None else None,
                   "step_model": dict(r.step_model) if r.step_model is not None else None,
                   "templates": dict(r.templates), "has_instructions": bool(r.instructions)}
                  for r in wf.roles.values() if r.name in wf.used_roles()],
        "steps": [s.to_dict() for s in wf.steps],
    }


def role_agent_map(saved: Mapping[str, Any] | None) -> dict[str, str]:
    """run.json 의 workflow(저장본) → {역할: CLI}. 없으면(classic) 빈 dict."""
    if not isinstance(saved, Mapping):
        return {}
    roles = saved.get("roles")
    if not isinstance(roles, Mapping):
        return {}
    return {str(n): str(r.get("agent")) for n, r in roles.items() if isinstance(r, Mapping) and r.get("agent")}
