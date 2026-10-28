"""그래프 형식 워크플로 실행 (DESIGN.md 36.30 M45).

시작 단계부터 선을 따라간다. 블록(explore·debate·build·audit)은 기존 Phase 를 그대로 부르고,
갈림(branch)은 상태를 보고 길을 고르며, 이어서 고치기(continue)는 끝난 구현+리뷰에 라운드를 더하고,
나란히(parallel)는 읽기 전용 가지를 스레드로 동시에 돌린다.

같은 단계를 두 번째로 돌면 id 에 `_2` 를 붙여 기록·완료 표시를 따로 둔다. 갈림 결과와 이어서 고치기의
상한은 state.graph 에 남겨 resume 이 같은 길을 다시 걷는다.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace

from tenure.phases.build import BuildPhase
from tenure.phases.common import PhaseContext, PhaseStop
from tenure.phases.debate import DebatePhase
from tenure.phases.extra import run_audit, run_explore
from tenure.state import FAILED, RUNNING
from tenure.workflow import END, Step, Workflow
from tenure.workflow_graph import default_next, start_id


@dataclass
class GraphResult:
    approved: bool | None        # 구현+리뷰가 돌았으면 마지막 리뷰 승인 여부, 아니면 None
    design_stop: bool = False    # 설계만(design_only)이라 토론 뒤에 멈춤


class GraphRunner:
    def __init__(self, ctx: PhaseContext, wf: Workflow) -> None:
        self.ctx = ctx
        self.wf = wf
        self.st = ctx.state
        self.by_id = {s.id: s for s in wf.steps}
        self.g = self.st.graph
        self.g.setdefault("decisions", {})
        self.g.setdefault("continue", {})

    # ---------------------------------------------------------------- walk
    def run(self, *, design_only: bool = False) -> GraphResult:
        visits: dict[str, int] = {}
        cur = start_id(self.wf.steps)
        while cur != END:
            step = self.by_id[cur]
            n = visits.get(cur, 0) + 1
            limit = step.max_visits or 1
            if n > limit:
                self.ctx.log(f"반복 상한: {cur} 단계를 {limit}번 돌아 여기서 끝냅니다.")
                self.g["stopped_at"] = cur
                break
            visits[cur] = n
            inst = step if n == 1 else replace(step, id=f"{step.id}_{n}")
            if step.kind == "debate":
                if not self.st.is_done("debate"):
                    DebatePhase(self.ctx, step).run()
                    self.st.mark_done("debate")
                    self.ctx.save()
                if design_only:
                    return GraphResult(approved=None, design_stop=True)
            cur = self._step(step, inst, n)
        return GraphResult(approved=self._approved())

    def _next(self, step: Step) -> str:
        return step.next or default_next(self.wf.steps, step)

    def _step(self, step: Step, inst: Step, n: int) -> str:
        if step.kind == "explore":
            run_explore(self.ctx, inst)
        elif step.kind == "build":
            if not self.st.is_done("build"):
                BuildPhase(self.ctx, step=step).run()
                self.st.mark_done("build")
                self.ctx.save()
        elif step.kind == "audit":
            self._need_build(inst)
            run_audit(self.ctx, inst)
            self.g["last_audits"] = [inst.id]
            self.ctx.save()
        elif step.kind == "branch":
            return self._branch(step, n)
        elif step.kind == "continue":
            self._continue(inst)
        elif step.kind == "parallel":
            self._parallel(step, n)
        return self._next(step)

    # ---------------------------------------------------------------- branch
    def _branch(self, step: Step, n: int) -> str:
        key = f"{step.id}#{n}"
        decided = self.g["decisions"].get(key)
        if decided is None:
            decided = "yes" if self._check(step.check or "") else "no"
            self.g["decisions"][key] = decided
            self.ctx.log(f"갈림 {step.id}({step.check}): {'예' if decided == 'yes' else '아니오'}")
            self.ctx.save()
        target = step.yes if decided == "yes" else step.no
        return target or END

    def _check(self, check: str) -> bool:
        st = self.st
        if check == "approved":
            return self._approved() is True
        if check == "tests_pass":
            if not st.tests:
                return False
            return st.tests[max(st.tests, key=int)].get("status") == "passed"
        if check == "audit_clean":
            ran = [r for sid in self.g.get("last_audits") or [] for r in st.audits.get(sid, [])]
            return bool(ran) and all(r.get("approved") for r in ran)
        return False

    def _approved(self) -> bool | None:
        if not self.st.is_done("build") and not self.st.review_history:
            return None
        return bool(self.st.review_history and self.st.review_history[-1].get("approved"))

    # ---------------------------------------------------------------- continue
    def _continue(self, inst: Step) -> None:
        key = f"continue:{inst.id}"
        if self.st.is_done(key):
            return
        self._need_build(inst)
        done_rounds = [int(k.split(":")[1]) for k in self.st.done
                       if k.startswith("review:") and k.split(":")[1].isdigit()]
        target = self.g["continue"].get(inst.id)
        if target is None:
            target = max(done_rounds, default=0) + (inst.rounds or 1)
            self.g["continue"][inst.id] = target
        st = self.st
        st.status, st.stop_reason, st.error = RUNNING, None, None
        st.stall_baseline_round = st.round        # 정체 판단은 여기서 다시 센다 (resume 과 같음)
        st.config["max_review"] = target
        self.ctx.log(f"이어서 고치기 {inst.id}: {inst.implementer} 가 리뷰 {', '.join(inst.reviewers)} 와 "
                     f"{target}회까지 더 돕니다.")
        self.ctx.save()
        build = Step(kind="build", id="build", implementer=inst.implementer, reviewers=list(inst.reviewers))
        phase = BuildPhase(self.ctx, step=build)
        phase.max_review = target
        phase.run()
        st.mark_done(key)
        self.ctx.save()

    # ---------------------------------------------------------------- parallel
    def _parallel(self, step: Step, n: int) -> None:
        branches = [self.by_id[b] if n == 1 else replace(self.by_id[b], id=f"{b}_{n}") for b in step.branches]
        for b in branches:
            if b.kind == "audit":
                self._need_build(b)
        self.ctx.log(f"나란히 {step.id}: {', '.join(b.id for b in branches)} 를 동시에 돌립니다.")
        errors: list[BaseException] = []

        def work(b: Step) -> None:
            try:
                (run_audit if b.kind == "audit" else run_explore)(self.ctx, b)
            except BaseException as e:   # 스레드 밖에서 다시 올린다
                errors.append(e)

        threads = [threading.Thread(target=work, args=(b,), daemon=True) for b in branches]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            raise errors[0]
        audits = [b.id for b in branches if b.kind == "audit"]
        if audits:
            self.g["last_audits"] = audits
        self.ctx.save()

    def _need_build(self, inst: Step) -> None:
        if self.st.base_commit is None:
            self.st.status = FAILED
            self.st.error = f"{inst.id}: 구현+리뷰(build)가 아직 돌지 않아 이 단계를 할 수 없습니다. 워크플로의 선을 확인하세요."
            self.ctx.save()
            raise PhaseStop(self.st.error, FAILED)
