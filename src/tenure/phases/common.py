"""Phase 공통부: 에이전트 호출 + 기록 + 상태 갱신 + 실패 정책.

실패 정책(DESIGN 5.4):
- 한도 도달(rate_limited): 즉시 상태 저장 후 RateLimitStop.
- 비정상 종료/타임아웃: 1회 재시도. 또 실패하면 상태 저장 후 AgentFailed.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Mapping

from tenure import events, slots
from tenure.outcome import step_outcome
from tenure.attachments import ATTACH_STEPS, is_image, prompt_note as attachments_note
from tenure.agents.base import AgentAdapter, AgentRequest, AgentResult, Mode
from tenure.config import Config, dedup_agent_config
from tenure.prompts import render
from tenure.runner import RunnerError
from tenure import limits, stopflag
from tenure.dedup import DEDUP_ROLE
from tenure.state import FAILED, STOPPED_RATE_LIMIT, RunState
from tenure.transcript import Transcript

if TYPE_CHECKING:
    from tenure.workflow import Workflow

Logger = Callable[[str], None]
EXPLORE_NOTE_MAX = 20_000   # 사전 조사 결과를 프롬프트에 붙일 때 최대 길이
DESIGN_DOC_DEFAULT = "DESIGN.md"


# 지금 실행 중인 워크플로 단계 (DESIGN.md 36.35). 병렬 갈래는 스레드마다 따로 담는다.
_WF_STEP: contextvars.ContextVar[tuple[str, int]] = contextvars.ContextVar("tenure_wf_step", default=("", 0))


@contextmanager
def in_wf_step(step_id: str, visit: int = 1) -> Iterator[None]:
    """이 블록 안의 에이전트 호출·로그에 워크플로 단계 id 를 붙인다."""
    token = _WF_STEP.set((step_id, visit))
    try:
        yield
    finally:
        _WF_STEP.reset(token)


def wf_step_meta() -> dict[str, Any]:
    """이벤트·호출 기록에 붙일 단계 표시 (단계 밖이면 빈 dict)."""
    step_id, visit = _WF_STEP.get()
    if not step_id:
        return {}
    return {"wf_step": step_id, **({"visit": visit} if visit > 1 else {})}


class PhaseStop(Exception):
    """Phase를 중단해야 하는 상황. status는 이미 상태에 기록됨."""

    def __init__(self, message: str, status: str):
        super().__init__(message)
        self.status = status


class RateLimitStop(PhaseStop):
    def __init__(self, agent: str, step: str):
        super().__init__(f"{agent}의 구독 한도에 도달했습니다 ({step}).", STOPPED_RATE_LIMIT)
        self.agent = agent


class AgentFailed(PhaseStop):
    def __init__(self, agent: str, step: str, reason: str):
        super().__init__(f"{agent} 호출이 반복 실패했습니다 ({step}): {reason}", FAILED)
        self.agent = agent


@dataclass
class CallOutcome:
    text: str
    file: str            # transcript 파일 이름
    result: AgentResult


@dataclass
class PhaseContext:
    task: str
    workdir: Path
    cfg: Config
    agents: Mapping[str, AgentAdapter]
    state: RunState
    transcript: Transcript
    run_dir: Path
    log: Logger = field(default=lambda msg: None)
    retries: int = 1
    # 워크플로 템플릿 (DESIGN.md 19장). None 이면 agents 의 키가 곧 CLI 이름(classic)
    workflow: "Workflow | None" = None
    # 병렬 실행 단계(DESIGN.md 36.30)가 기록 번호 매기기·상태 저장을 줄 세우는 잠금
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False, compare=False)

    # ---------------------------------------------------------------- roles
    def cli_of(self, role: str) -> str:
        """역할 → CLI 이름(claude/codex). 기록·사용량 합계는 CLI 기준."""
        if self.workflow is not None and role in self.workflow.roles:
            return self.workflow.roles[role].agent
        if role == DEDUP_ROLE:                    # 재제기 확인자 (DESIGN.md 26.2)
            return self.cfg.dedup_agent
        if role == "report":                      # 결과 요약자 (DESIGN.md 32장)
            return self.cfg.report_agent
        return role

    @property
    def design_doc(self) -> str:
        """설계 문서 경로(작업 폴더 기준). 변경 설계면 docs/changes/<run>.md (DESIGN.md 19.5)."""
        return self.state.design_doc or DESIGN_DOC_DEFAULT

    @property
    def change_mode(self) -> bool:
        return bool(self.state.design_doc) and self.state.design_doc != DESIGN_DOC_DEFAULT

    def design_note(self) -> str:
        if not self.change_mode:
            return ""
        return (f"이번 작업은 기존 프로젝트를 바꾸는 것이다. 이번 변경의 설계는 {self.design_doc} 에 있다. "
                "프로젝트 전체 설계(DESIGN.md, 있다면)와 기존 코드의 관례를 함께 따르고, "
                "과제에 필요하지 않은 기존 동작은 바꾸지 마라.")

    def agent_config(self, role: str):
        """역할이 쓰는 CLI 설정(모델·강도 포함)."""
        if self.workflow is not None and role in self.workflow.roles:
            return self.workflow.roles[role].agent_config(self.cfg)
        if role == DEDUP_ROLE:
            return dedup_agent_config(self.cfg)
        if role == "report":
            from tenure.config import report_agent_config

            return report_agent_config(self.cfg)
        from tenure.config import apply_step_overrides

        return apply_step_overrides(self.cfg.agent(self.cli_of(role)), self.cfg, role)

    def can_run_commands(self, role: str) -> bool:
        """읽기 모드에서 이 역할이 명령을 실행할 수 있는가 (DESIGN.md 19.6).

        Claude 는 read_args 에 Bash 가 있을 때만(기본 Read,Glob,Grep 이라 불가), Codex read-only 샌드박스는 가능.
        """
        cli = self.cli_of(role)
        if cli != "claude":
            return True
        if self.workflow is not None and role in self.workflow.roles:
            ac = self.workflow.roles[role].agent_config(self.cfg)
        else:
            ac = self.cfg.agent(cli)
        from tenure.tools import can_run_commands

        return can_run_commands(self.cfg, ac.read_args)

    def prompt(self, role: str, step: str, **values: object) -> str:
        """step 의 프롬프트. 역할이 템플릿을 바꿨으면 그것을 쓰고, 사전 조사·변경 설계 안내·역할 지시를 붙인다."""
        r = self.workflow.roles.get(role) if self.workflow is not None else None
        name = r.templates.get(step, step) if r is not None else step
        values.setdefault("design_doc", self.design_doc)
        values.setdefault("design_note", self.design_note())
        text = render(name, self.workdir, **values)
        if self.change_mode and step in ("propose", "critique", "synthesize"):
            text += ("\n\n## 기존 프로젝트의 변경\n\n이 과제는 기존 프로젝트를 바꾸는 작업이다. 프로젝트 전체를 다시 "
                     "설계하지 말고 이번 변경만 설계해라: 바꿀 파일·함수, 지켜야 할 기존 동작, 테스트, 문서 갱신. "
                     f"결과는 {self.design_doc} 에 저장되고 DESIGN.md 는 덮어쓰지 않는다.")
        if step in ("propose", "implement"):
            notes = self._explore_notes()
            if notes:
                text += "\n\n## 사전 조사 (tenure 가 먼저 기존 코드를 조사한 결과)\n\n" + notes
        text += self._tools_note(step)
        if step in ATTACH_STEPS and self.state.attachments:
            text += "\n\n" + attachments_note([self._rel(self.run_dir / a) for a in self.state.attachments])
        if r is not None and r.instructions.strip():
            text += "\n\n## 이 역할에 대한 추가 지시\n\n" + r.instructions.strip()
        return text

    def _rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.workdir).as_posix()
        except ValueError:
            return str(path)

    def _tools_note(self, step: str) -> str:
        """최신 정보 안내 (DESIGN.md 33.1·33.2): 웹 검색·버전 조회를 쓸 수 있는 단계에만."""
        cfg = self.cfg
        design = step in ("propose", "critique", "synthesize", "explore")
        coding = step in ("implement", "fix")
        web = cfg.web_search == "on" and (design or step in ("review", "arbitrate", "audit")
                                          or (coding and cfg.access == "full"))
        out = ""
        if web:
            out += ("\n\n## 최신 정보\n\n라이브러리 버전·API·도구 사용법처럼 바뀌는 정보는 기억에 기대지 말고 웹에서 확인해라"
                    "(공식 문서 우선). 근거로 쓴 출처는 링크로 적어라.")
        if design and step != "critique" and (cfg.access == "full" or cfg.package_lookup or cfg.web_search == "on"):
            out += ("\n\n패키지 최신 버전은 `npm view <이름> version`, `pip index versions <이름>` 같은 명령이나 웹 검색으로 "
                    "확인하고, 고정할 버전을 설계에 적어라.")
        return out

    def images_for(self, step: str) -> list[str]:
        """Codex 에 -i 로 붙일 첨부 이미지 (DESIGN.md 33.3)."""
        if step not in ATTACH_STEPS:
            return []
        return [str(self.run_dir / a) for a in self.state.attachments if is_image(a)]

    def _explore_notes(self) -> str:
        parts = []
        for sid, file in self.state.explores.items():
            try:
                body = self.transcript.read(file).strip()
            except OSError:
                continue
            if len(body) > EXPLORE_NOTE_MAX:
                body = body[:EXPLORE_NOTE_MAX] + "\n... (길어서 잘림)"
            parts.append(f"### {sid}\n{body}" if len(self.state.explores) > 1 else body)
        return "\n\n".join(parts)

    # ---------------------------------------------------------------- state
    def save(self) -> None:
        with self.lock:
            self._save()

    def _save(self) -> None:
        self.state.save(self.run_dir)
        st = self.state
        key = (st.status, st.phase, st.step, st.round)
        if key != getattr(self, "_last_state_event", None):   # 상태 전환만 이벤트로 (36.3)
            object.__setattr__(self, "_last_state_event", key)
            events.for_run(self.run_dir).emit(events.STATE, status=st.status, phase=st.phase, step=st.step,
                                              round=st.round, error=st.error)

    def set_position(self, phase: str, step: str, round_: int) -> None:
        self.state.phase, self.state.step, self.state.round = phase, step, round_

    # ---------------------------------------------------------------- call
    def call(
        self,
        agent_name: str,
        step: str,
        prompt: str,
        mode: Mode,
        *,
        label: str,
        round_: int = 0,
        extra_meta: Mapping[str, object] | None = None,
        output_schema: dict | None = None,
        resume_session: str | None = None,
        fallback_prompt: str | None = None,
        transform: Callable[[AgentResult], str] | None = None,
        effort: str | None = None,
        optional: bool = False,
        browser: object | None = None,
    ) -> CallOutcome | None:
        """에이전트를 호출하고 기록·상태를 갱신한다. 실패 정책을 적용한다.

        transform: 성공한 결과를 기록·반환할 본문으로 바꾼다(예: JSON 판정 → 마크다운 리뷰, DESIGN.md 21.2).
        optional: 보조 호출(재제기 확인 등). 한도가 아닌 실패면 실행을 실패로 만들지 않고 None 을 돌려준다.
        """
        agent = self.agents[agent_name]
        req = AgentRequest(prompt=prompt, cwd=self.workdir, mode=mode,
                           timeout_sec=self.cfg.timeout_sec, step=step, round=round_,
                           output_schema=output_schema, resume_session=resume_session,
                           fallback_prompt=fallback_prompt, effort=effort,
                           images=self.images_for(step), browser=browser)
        attempt = 0
        limit_hits = 0
        while True:
            stopflag.check()                      # 단계 사이의 중지 요청 (DESIGN.md 27.3)
            attempt += 1
            started = time.monotonic()
            self.log(f"{agent_name} ← {step}" + (f" (재시도 {attempt - 1})" if attempt > 1 else "") + " 실행 중...")
            events.for_run(self.run_dir).emit(events.STEP_START, agent=self.cli_of(agent_name), role=agent_name,
                                              step=step, round=round_, attempt=attempt, **wf_step_meta())
            try:
                if getattr(agent, "uses_slot", True):     # 동시 에이전트 상한 (DESIGN.md 36.18)
                    with slots.agent_slot(self.cfg.max_agents, log=self.log, what=f"{agent_name} {step}"):
                        result = agent.ask(req)
                else:
                    result = agent.ask(req)
            except (OSError, RunnerError) as e:
                # 실행 자체가 안 됨(인자 길이 초과, 실행 파일 사라짐 등). 재시도해도 같으므로 바로 멈춘다.
                self.state.status = FAILED
                self.state.error = f"{agent_name} {step} 실행 불가: {e}"
                self.save()
                raise AgentFailed(agent_name, step, f"실행 불가: {e}") from e
            elapsed = result.duration_sec or (time.monotonic() - started)

            cli = self.cli_of(agent_name)
            outcome, why = step_outcome(result, expect_structured=output_schema is not None)
            meta = {
                "agent": cli, **({"role": agent_name} if cli != agent_name else {}),
                "adapter": agent.name, "step": step, "mode": mode.value,
                "round": round_, "attempt": attempt,
                "duration_sec": elapsed, "exit_code": result.exit_code,
                "rate_limited": result.rate_limited, "timed_out": result.timed_out,
                "ok": result.ok, "outcome": outcome, **({"reason": why} if why else {}),
                **_usage_meta(result.usage),
                **({"resumed": True} if result.resumed else {}),
                **({"session_id": result.session_id} if result.session_id else {}),
                **({"structured": True} if result.structured is not None else {}),
                **(extra_meta or {}),
                **wf_step_meta(),
            }
            text = (transform(result) if transform is not None else result.text) if result.ok else result.text
            body = text if result.ok else _failure_body(result)
            with self.lock:
                path = self.transcript.save(label if result.ok else f"{label}_failed", body, meta)
                self.state.record_call(cli, step, elapsed, result.exit_code,
                                       rate_limited=result.rate_limited, timed_out=result.timed_out,
                                       file=path.name, usage=result.usage,
                                       role=agent_name if cli != agent_name else None, outcome=outcome, reason=why)
            events.for_run(self.run_dir).emit(events.STEP_END, agent=cli, role=agent_name, step=step, round=round_,
                                              attempt=attempt, outcome=outcome, reason=why, sec=round(elapsed, 1),
                                              file=path.name, **wf_step_meta())

            if result.ok:
                self.log(f"{agent_name} ← {step} 완료 ({elapsed:.0f}초)"
                         + (" [세션 이어감]" if result.resumed else "") + f" → {path.name}")
                return CallOutcome(text=text, file=path.name, result=result)

            reason = result.failure_reason()
            self.log(f"{agent_name} ← {step} 실패: {reason} → {path.name}")
            if result.rate_limited:
                limit_hits += 1
                if self._wait_for_limit(agent_name, step, result, limit_hits):
                    attempt -= 1          # 한도 재시도는 일반 실패 재시도(retries)에 세지 않는다
                    continue
                self.state.status = STOPPED_RATE_LIMIT
                self.state.error = f"{agent_name} 한도 도달 ({step})"
                self.save()
                raise RateLimitStop(agent_name, step)
            if attempt > self.retries:
                if optional:
                    self.log(f"{agent_name} ← {step} 실패라 건너뜁니다 (보조 호출)")
                    return None
                self.state.status = FAILED
                self.state.error = f"{agent_name} {step} 실패: {reason}"
                self.save()
                raise AgentFailed(agent_name, step, reason)


    def _wait_for_limit(self, agent_name: str, step: str, result: AgentResult, hits: int) -> bool:
        """한도에 걸렸을 때 재개 시각까지 기다린다 (DESIGN.md 25.1). 기다렸으면 True(같은 호출을 다시 보냄)."""
        if not self.cfg.wait_on_limit:
            return False
        budget = self.cfg.limit_wait_max_min * 60 - self.state.limit_waited_sec
        if budget <= 0:
            self.log(f"한도 대기 예산({self.cfg.limit_wait_max_min}분)을 다 써서 멈춥니다.")
            return False
        now = limits.now()
        wait, why = limits.plan_wait((result.text, result.raw_stderr, result.raw_stdout[-4000:]),
                                     result.usage, hits, now)
        wait = min(wait, budget)
        until = dt.datetime.fromtimestamp(now + wait)
        self.state.limit_wait = {"agent": self.cli_of(agent_name), "role": agent_name, "step": step,
                                 "until": until.isoformat(timespec="seconds"), "reason": why}
        events.for_run(self.run_dir).emit(events.ATTENTION, reason="구독 한도 대기", agent=self.cli_of(agent_name),
                                          step=step, until=self.state.limit_wait["until"])
        self.save()
        self.log(f"⏳ {agent_name} 한도 도달 ({step}). {until:%m-%d %H:%M} 까지 기다렸다가 다시 시도합니다 "
                 f"({why}, 남은 대기 예산 {budget / 60:.0f}분)")
        try:
            limits.sleep(wait)
        finally:
            self.state.limit_wait = None
            self.state.limit_waited_sec += wait
        self.save()
        return True


def _failure_body(result: AgentResult) -> str:
    parts = [f"# 호출 실패: {result.failure_reason()}", ""]
    if result.text:
        parts += ["## 응답 본문", result.text, ""]
    if result.raw_stderr.strip():
        parts += ["## stderr (마지막 4000자)", "```", result.raw_stderr[-4000:], "```", ""]
    if result.raw_stdout.strip() and result.raw_stdout.strip() != result.text.strip():
        parts += ["## stdout (마지막 4000자)", "```", result.raw_stdout[-4000:], "```", ""]
    return "\n".join(parts)


def _usage_meta(u: dict | None) -> dict[str, object]:
    if not u:
        return {}
    meta: dict[str, object] = {}
    for src, dst in (("model", "model"), ("effort", "effort"), ("input_tokens", "tokens_in"),
                     ("output_tokens", "tokens_out"), ("cost_usd", "cost_usd")):
        if u.get(src) not in (None, ""):
            meta[dst] = u[src]
    return meta
