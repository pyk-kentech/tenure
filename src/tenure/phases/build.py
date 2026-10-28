"""BuildPhase — 구현 + 교차 리뷰.

workspace.ensure_repo(); base = commit("tenure: design")
loop i in 1..max_review:
  implementer.ask(implement 또는 fix)          # WRITE
  commit("tenure: impl round i")
  (리뷰 전 남은 변경이 있으면 커밋해서 보존)
  diff = 변경 파일 요약 + workspace.diff(base, HEAD, exclude=[DESIGN.md, 잠금 파일])
  reviewer.ask(review)                          # READ
  if workspace.dirty(): 경고 + 되돌림
  verdict → APPROVED면 종료

resume 키:
  build:base        기준 커밋 완료
  impl:i:called     구현/수정 호출 성공(유료) — 커밋 전에 죽어도 다시 부르지 않는다
  impl:i            커밋까지 완료
  test:i            tenure 가 테스트 실행 (결과는 state.tests[i], 기록 NN_test{i}.md)
  guard:i           테스트 변조 감지 (DESIGN.md 24.1, 결과는 state.test_guard[i])
  revert:i          되돌림 감지 (DESIGN.md 26.3, 결과는 state.reverts[i])
  review:i          리뷰 + 판정 완료 (판정은 policy.decide: 심각도 기준·테스트 결과 반영)
  review:i:<역할>   리뷰어가 여럿일 때 그 리뷰어의 리뷰 완료 (판정은 state.round_reviews[i][역할])

리뷰어가 여럿이면(DESIGN.md 19.3) 모두 승인해야 승인, 막는 지적 수는 합.

멈춤: 승인 → done_approved / 상한 도달 → done_unapproved(stop_reason=max_review)
      막는 지적 수가 stall_rounds 라운드 연속 최저치를 못 갱신 → done_unapproved(stop_reason=stalled)
"""

from __future__ import annotations

from contextlib import contextmanager

from dataclasses import replace
from typing import TYPE_CHECKING

from tenure.agents.base import Mode
from tenure.phases.common import PhaseContext
from tenure.phases.debate import DESIGN_FILENAME
from tenure import arbiter as arb
from tenure import browser as browserlib
from tenure import dedup, gates, revert, testguard
from tenure.dedup import DEDUP_ROLE
from tenure.packet import READ_INSTRUCTION_PACKET, READ_INSTRUCTION_PLAIN, call_sites, changed_symbols
from tenure.policy import Decision, decide, stalled
from tenure.prompts import TemplateError, load_template
from tenure.review_json import REVIEW_SCHEMA, output_instruction, render_review
from tenure.agents.base import parse_structured
from tenure.repro import parse_repros, write_repros
from tenure.runner import env_drops
from tenure.testing import STATUS_KO, describe_for_prompt, resolve_command, run_tests
from tenure.state import DONE_APPROVED, DONE_UNAPPROVED, PHASE_BUILD, PHASE_DONE
from tenure.verdict import parse_verdict
from tenure.workspace import GitError, Workspace

if TYPE_CHECKING:
    from tenure.verdict import Verdict
    from tenure.workflow import Step

DIFF_EXCLUDES = (DESIGN_FILENAME,)
TRUNCATED_NOTE = "\n... (diff가 길어 잘림. 위의 변경 파일 요약을 보고 나머지는 파일을 직접 열어 확인)"


def truncate_diff(diff: str, max_chars: int) -> str:
    if not diff.strip():
        return "(변경 없음)"
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + TRUNCATED_NOTE


def diff_body(ws: Workspace, frm: str, exclude, excludes_glob, budget: int, context: bool, head: str = "HEAD") -> str:
    """본문 diff. context 면 함수 단위 문맥(리뷰 패킷, DESIGN.md 21.1)을 쓰되 budget 을 넘으면 예전 diff."""
    if context:
        body = ws.diff(frm, head, exclude=exclude, exclude_globs=excludes_glob, context=True)
        if body.strip() and len(body) <= budget:
            return body
    return ws.diff(frm, head, exclude=exclude, exclude_globs=excludes_glob)


def build_review_diff(ws: Workspace, base: str, excludes_glob: list[str], max_chars: int,
                      exclude: tuple[str, ...] = DIFF_EXCLUDES, context: bool = False, head: str = "HEAD") -> str:
    """리뷰어에게 줄 diff. 맨 앞의 파일 요약(stat)은 잘리지 않게 두고, 본문 diff 만 자른다.

    exclude: 빼는 파일(설계 문서). 변경 설계면 DESIGN.md 대신 docs/changes/<run>.md (DESIGN.md 19.5).
    head: 화면이 병합·버린 worktree 실행을 볼 때는 남겨 둔 브랜치 (36.14).
    """
    stat = ws.diff_stat(base, head, exclude=exclude)
    budget0 = max(1000, max_chars - len(stat) - 200)
    body = diff_body(ws, base, exclude, excludes_glob, budget0, context, head)
    if not stat.strip() and not body.strip():
        return "(변경 없음)"
    header = "[변경 파일 요약]\n" + stat.rstrip() + "\n"
    if excludes_glob:
        header += f"(잠금 파일 등은 아래 diff 에서 뺐음: {', '.join(excludes_glob)})\n"
    budget = max(1000, max_chars - len(header))
    return header + "\n" + truncate_diff(body, budget)


class BuildPhase:
    def __init__(self, ctx: PhaseContext, workspace: Workspace | None = None, step: "Step | None" = None):
        self.ctx = ctx
        self.state = ctx.state
        self.cfg = ctx.cfg
        self.ws = workspace or Workspace(ctx.workdir)
        self.step = step
        self.implementer = step.implementer if step is not None and step.implementer else ctx.cfg.implementer
        self.reviewers: list[str] = list(step.reviewers) if step is not None else [ctx.cfg.reviewer]
        self.arbiter_role = self._pick_arbiter(step)
        self.reviewer = self.reviewers[0]
        self.multi = len(self.reviewers) > 1
        self.max_review = ctx.cfg.max_review
        self._single: "Verdict | None" = None
        self._downgraded: dict[str, dict] = {}     # 역할 → 이번 라운드 관문별 강등 수

    # ---------------------------------------------------------------- run
    def run(self) -> bool:
        ctx, st = self.ctx, self.state

        if not st.is_done("build:base"):
            ctx.set_position(PHASE_BUILD, "base", 0)
            self.ws.ensure_repo()
            st.base_commit = self.ws.commit_all("tenure: design")
            st.mark_done("build:base")
            ctx.save()
            ctx.log(f"기준 커밋: {st.base_commit}")
        base = st.base_commit
        assert base

        if self.step is not None and self.step.rivals:     # 경쟁 초안 (DESIGN.md 36.15)
            if self._race(self.step.rivals[0]):
                return True

        for i in range(1, self.max_review + 1):
            impl_key, review_key = f"impl:{i}", f"review:{i}"

            if not st.is_done(impl_key):
                if not st.is_done(f"{impl_key}:called"):
                    ctx.set_position(PHASE_BUILD, "implement", i)
                    ctx.save()
                    if i == 1 or not st.last_review:
                        step = "implement"
                        ctx.log(f"[{i}/{self.max_review}] 구현: {self.implementer}")
                        prompt = ctx.prompt(self.implementer, "implement", task=ctx.task,
                                            test_hint=self._test_hint())
                    else:
                        step = "fix"
                        ctx.log(f"[{i}/{self.max_review}] 리뷰 반영: {self.implementer}")
                        prompt = ctx.prompt(self.implementer, "fix", task=ctx.task, review=self._review_texts(i),
                                            reviewer_name=", ".join(self.reviewers),
                                            tests=self._tests_text(i - 1), repro=self._repro_text(i - 1))
                    out = ctx.call(self.implementer, step, prompt, Mode.WRITE,
                                   label=f"impl{i}_{self.implementer}", round_=i)
                    st.last_impl = out.file
                    st.mark_done(f"{impl_key}:called")
                    ctx.save()
                commit = self.ws.commit_all(f"tenure: impl round {i}")
                ctx.log(f"[{i}/{self.max_review}] 커밋: {commit}")
                st.mark_done(impl_key)
                ctx.save()

            test_key = f"test:{i}"
            # 테스트는 그 라운드 리뷰 전에만 의미가 있다. 리뷰가 끝난 라운드(예: 테스트 단계가 없던
            # 예전 실행을 이어갈 때)는 지금 코드로 돌려 봐야 그 라운드 기록이 틀려진다.
            if not st.is_done(test_key) and not st.is_done(review_key):
                ctx.set_position(PHASE_BUILD, "test", i)
                ctx.save()
                self._run_tests(i)
                st.mark_done(test_key)
                ctx.save()

            if not st.is_done(review_key):
                ctx.set_position(PHASE_BUILD, "review", i)
                ctx.save()
                self._preserve_pending_changes(i)
                self._guard_tests(i, base)
                self._detect_reverts(i)
                diff_title, diff = self._review_diff(i, base)
                decision, files = self._review_round(i, diff_title, diff)
                test = st.tests.get(str(i))
                st.last_verdict = "APPROVED" if decision.approved else "CHANGES_REQUESTED"
                st.review_history.append(self._history_entry(i, decision, files, test))
                st.mark_done(review_key)
                ctx.log(f"[{i}/{self.max_review}] 판정: {decision.describe()}")
                if decision.approved:
                    st.status = DONE_APPROVED
                    st.stop_reason = None
                    ctx.set_position(PHASE_DONE, "approved", i)
                    ctx.save()
                    return True
                ctx.save()
                if self.cfg.repro_tests and not st.is_done(f"repro:{i}"):
                    self._handle_repros(i, "\n\n".join(ctx.transcript.read(f) for f in files))
                    st.mark_done(f"repro:{i}")
                    ctx.save()
                if self._focus_round(i):
                    # 중재 뒤 마지막 라운드도 승인되지 않음 (DESIGN.md 23.4)
                    st.status = DONE_UNAPPROVED
                    st.stop_reason = "arbiter_final"
                    st.arbiter["upheld"] = arb.blocking_ids(st, self.cfg.block_severity)
                    ctx.set_position(PHASE_DONE, "unapproved", i)
                    ctx.save()
                    return False
                if self._is_stalled():
                    outcome = self._maybe_arbitrate(i, "stalled")
                    if outcome == "approved":
                        return True
                    if outcome == "continue":
                        ctx.save()
                        continue
                    if outcome == "stop":
                        st.status = DONE_UNAPPROVED
                        st.stop_reason = "arbiter_upheld"
                        ctx.set_position(PHASE_DONE, "unapproved", i)
                        ctx.save()
                        return False
                    st.status = DONE_UNAPPROVED
                    st.stop_reason = "stalled"
                    ctx.set_position(PHASE_DONE, "stalled", i)
                    ctx.save()
                    ctx.log(f"주의: 수렴하지 않음: 막는 지적 수가 {self.cfg.stall_rounds}라운드 연속 줄지 않아 멈춥니다 "
                            f"({self._blocking_series()}). 마지막 리뷰를 보고 직접 판단하세요.")
                    return False
                ctx.save()

        if st.review_history and not st.review_history[-1].get("approved"):
            outcome = self._maybe_arbitrate(self.max_review, "max_review")
            if outcome == "approved":
                return True
            if outcome in ("stop", "continue"):     # 상한이라 추가 라운드 없음
                st.status = DONE_UNAPPROVED
                st.stop_reason = "arbiter_upheld"
                ctx.set_position(PHASE_DONE, "unapproved", self.max_review)
                ctx.save()
                return False
        st.status = DONE_UNAPPROVED
        st.stop_reason = "max_review"
        ctx.set_position(PHASE_DONE, "unapproved", self.max_review)
        ctx.save()
        return False

    # ---------------------------------------------------------------- race (DESIGN.md 36.15)
    def _race(self, rival: str) -> bool:
        """초안 둘을 돌려 승자를 고르고, 이후 라운드를 승자=구현·패자=리뷰로 잇게 한다. 승자 초안이 승인이면 True."""
        from tenure.phases.race import Race

        ctx, st = self.ctx, self.state
        if not st.is_done("race"):
            ctx.set_position(PHASE_BUILD, "race", 1)
            ctx.save()
            Race(self, rival).run()
        winner, loser = st.race["winner"], st.race["loser"]
        self.implementer, self.reviewers, self.reviewer, self.multi = winner, [loser], loser, False
        if len(st.review_history) == 1 and st.review_history[0].get("approved"):
            st.status = DONE_APPROVED
            st.stop_reason = None
            ctx.set_position(PHASE_DONE, "approved", 1)
            ctx.save()
            return True
        return False

    # ---------------------------------------------------------------- arbiter (DESIGN.md 23장)
    def _pick_arbiter(self, step) -> str | None:
        if step is not None and step.arbiter:
            return step.arbiter
        wf = self.ctx.workflow
        if wf is not None:
            debate = wf.first("debate")
            if debate is not None and debate.synth and debate.synth != self.implementer:
                return debate.synth
            return self.reviewers[0]
        return self.cfg.synth_name

    def _focus_round(self, i: int) -> bool:
        """이 라운드가 중재 뒤 마지막 라운드인가."""
        a = self.state.arbiter
        return bool(a and a.get("result") == "continue" and a.get("round") == i - 1)

    def _focus_ids(self, i: int) -> set[str] | None:
        return set(self.state.arbiter.get("upheld") or []) if self._focus_round(i) else None

    def _maybe_arbitrate(self, i: int, reason: str) -> str | None:
        """None: 중재 안 함(예전처럼 멈춤). approved / continue / stop."""
        ctx, st = self.ctx, self.state
        if not self.cfg.arbiter or st.arbiter is not None or self.arbiter_role is None:
            return None
        if self.arbiter_role not in ctx.agents:
            return None
        items = [x for x in st.issues if x.get("status") == "open"]
        if not items:
            return None
        ctx.set_position(PHASE_BUILD, "arbitrate", i)
        ctx.save()
        ctx.log(f"[{i}/{self.max_review}] 중재: {self.arbiter_role} (열린 지적 {len(items)}개, "
                f"{'수렴 실패' if reason == 'stalled' else '리뷰 상한'})")
        role = self.arbiter_role
        repro = st.repros.get(str(i)) or {}
        prompt = ctx.prompt(role, "arbitrate", task=ctx.task, rounds=i,
                            reason="막는 지적 수가 줄지 않음" if reason == "stalled" else "리뷰 상한에 닿음",
                            issues=arb.format_issues(items), tests=self._tests_for_review(i),
                            repros=(f"{', '.join(repro.get('files') or [])}: {repro.get('status')}"
                                    if repro.get("files") else "(없음)"),
                            stat=self.ws.diff_stat(st.base_commit, "HEAD", exclude=(ctx.design_doc,)).strip() or "(없음)",
                            run_instruction=("전체 테스트를 다시 돌리지는 마라." if ctx.can_run_commands(role)
                                             else "너는 명령을 실행할 수 없다(읽기 전용)."),
                            output_instruction=arb.OUTPUT_INSTRUCTION)
        holder: dict = {}

        def transform(r):
            decisions = arb.normalize(r.structured, items) if r.structured is not None else None
            if decisions is None:
                return r.text
            holder["d"] = decisions
            return arb.render(decisions, items, str((r.structured or {}).get("summary") or ""))

        out = ctx.call(role, "arbitrate", prompt, Mode.READ, label=f"arbiter{i}_{role}", round_=i,
                       output_schema=arb.ARBITER_SCHEMA, transform=transform)
        self._undo_reviewer_changes()
        if "d" not in holder:
            ctx.log(f"[{i}/{self.max_review}] 중재 결과를 읽지 못해 예전처럼 멈춥니다 → {out.file}")
            st.arbiter = {"round": i, "file": out.file, "result": "unreadable", "reason": reason}
            ctx.save()
            return None
        applied = arb.apply(st, holder["d"], i)
        blocking = arb.blocking_ids(st, self.cfg.block_severity)
        test = st.tests.get(str(i))
        tests_fail = bool(test and test.get("status") in ("failed", "timeout")) and self.cfg.require_tests_pass
        st.arbiter = {"round": i, "file": out.file, "reason": reason, "upheld": blocking,
                      "downgraded": applied["downgrade"], "rejected": applied["reject"]}
        ctx.log(f"[{i}/{self.max_review}] 중재 결과: 유지 {len(applied['uphold'])}, 완화 {len(applied['downgrade'])}, "
                f"기각 {len(applied['reject'])}" + (" (테스트 실패)" if tests_fail else "") + f" → {out.file}")
        if not blocking and not tests_fail:
            st.arbiter["result"] = "approved"
            st.status = DONE_APPROVED
            st.stop_reason = "arbiter"
            st.last_verdict = "APPROVED"
            ctx.set_position(PHASE_DONE, "approved", i)
            ctx.save()
            return "approved"
        if reason == "stalled" and i < self.max_review:
            st.arbiter["result"] = "continue"
            st.stall_baseline_round = i
            ctx.log(f"[{i}/{self.max_review}] 중재 뒤 마지막 라운드: 막는 기준은 {', '.join(blocking) or '테스트 실패'} 뿐")
            ctx.save()
            return "continue"
        st.arbiter["result"] = "stop"
        ctx.save()
        return "stop"

    # ---------------------------------------------------------------- review
    def _review_round(self, i: int, diff_title: str, diff: str) -> tuple[Decision, list[str]]:
        """이 라운드의 리뷰어들을 차례로 부르고 판정을 합친다. (합친 판정, 리뷰 파일들)

        웹 화면이 있는 결과물이면 리뷰 동안 앱을 띄워 두고 리뷰어에게 브라우저 도구를 붙인다 (DESIGN.md 33.4).
        """
        with self._browser_app(i) as browser:
            return self._run_reviews(i, diff_title, diff, browser)

    def _browser_ok(self) -> bool:
        return self.cfg.browser_check == "auto" and not self.state.config.get("mock")

    @contextmanager
    def _browser_app(self, i: int):
        ctx, st = self.ctx, self.state
        parts = st.round_reviews.get(str(i)) or {}
        pending = [r for r in self.reviewers if not (self.multi and st.is_done(f"review:{i}:{r}") and r in parts)]
        app = browserlib.detect(ctx.workdir) if self._browser_ok() and pending else None
        if app is None:
            yield None
            return
        server = browserlib.AppServer(ctx.workdir, app, ctx.run_dir / f"browser{i}.log")
        url = server.start()
        tool = browserlib.playwright_tool(url) if url else None
        if tool is None:
            why = server.error or "npx 가 없어 브라우저 도구를 붙일 수 없음"
            server.stop()
            st.browser[str(i)] = {"error": why}
            ctx.log(f"[{i}/{self.max_review}] 브라우저 확인 건너뜀: {why}")
            ctx.save()
            yield None
            return
        st.browser[str(i)] = {"url": url, "kind": app.kind}
        ctx.log(f"[{i}/{self.max_review}] 브라우저 확인: 앱을 {url} 에 띄움 (리뷰어가 필요하면 열어 봄)")
        ctx.save()
        try:
            yield tool
        finally:
            server.stop()

    def _run_reviews(self, i: int, diff_title: str, diff: str, browser) -> tuple[Decision, list[str]]:
        ctx, st = self.ctx, self.state
        test = st.tests.get(str(i))
        tests_block = bool(test and test.get("status") in ("failed", "timeout"))
        parts = st.round_reviews.setdefault(str(i), {}) if self.multi else {}
        decisions: list[tuple[str, Decision]] = []
        files: list[str] = []
        sites_full: str | None = None
        self._downgraded = {}
        for role in self.reviewers:
            sub_key = f"review:{i}:{role}"
            if self.multi and st.is_done(sub_key) and role in parts:
                p = parts[role]
                decisions.append((role, Decision(p["approved"], p["blocking"], list(p["reasons"]))))
                files.append(p["file"])
                continue
            json_mode = self.cfg.review_json and self._template_has(role, "review", "{output_instruction}")
            use_gates = json_mode and self.cfg.review_gates
            spec_text = self._spec_text() if use_gates and self.cfg.spec_gate else None
            repro_text = REPRO_INSTRUCTION if self.cfg.repro_tests else ""
            open_now = gates.open_issues(st, role) if use_gates else []
            focus = self._focus_ids(i) if use_gates else None
            if sites_full is None:
                sites_full = self._call_sites(diff)
            open_text = (gates.format_open(open_now) if focus is None else
                         "(중재 결과: 아래 중 " + (", ".join(sorted(focus)) or "없음")
                         + " 만 막는 기준이다. 그 밖의 지적은 P3 로 처리된다)\n" + gates.format_open(open_now))
            closed_text = dedup.closed_block(st) if use_gates and self.cfg.dedup else ""
            if closed_text:
                open_text += "\n\n" + closed_text
            common = dict(task=ctx.task, implementer_name=self.implementer, tests=self._tests_for_review(i),
                          test_instruction=self._test_instruction(i, role),
                          read_instruction=READ_INSTRUCTION_PACKET if self.cfg.review_packet else READ_INSTRUCTION_PLAIN,
                          repro_instruction=repro_text,
                          open_issues=open_text,
                          output_instruction=output_instruction(json_mode, self.cfg.repro_tests, repro_text,
                                                                gates=use_gates, spec=spec_text is not None))
            full_prompt = ctx.prompt(role, "review", diff=diff, diff_title=diff_title, call_sites=sites_full,
                                     previous_review=self._previous_must_fix(role), **common)
            if browser is not None:
                full_prompt += "\n\n" + browser.prompt_note()
            resume_id = self._resumable_session(role, i, json_mode)
            if resume_id:
                # 이어간 리뷰도 diff 는 review_diff 설정을 따른다 (기본 incremental: 지난 리뷰 이후만)
                prompt = ctx.prompt(role, "rereview", diff=diff, diff_title=diff_title, call_sites=sites_full,
                                    previous_review=self._previous_must_fix(role), **common)
                if browser is not None:
                    prompt += "\n\n" + browser.prompt_note()
                ctx.log(f"[{i}/{self.max_review}] 리뷰: {role} (지난 세션 이어서, diff {len(diff):,}자)")
            else:
                prompt = full_prompt
                ctx.log(f"[{i}/{self.max_review}] 리뷰: {role} (diff {len(diff):,}자"
                        f"{', 지난 리뷰 이후만' if '이후' in diff_title else ''})")
            gated: dict = {}
            if use_gates:
                changed = self._changed_since_review(i)
                ids = {x["id"] for x in open_now}
                first = gates.next_number(st)

                def transform(r, changed=changed, ids=ids, first=first, focus=focus, spec_text=spec_text):
                    if r.structured is None:
                        return r.text
                    res = gates.apply_gates(r.structured, round_=i, open_ids=ids, changed_files=changed,
                                            first_number=first, only_ids=focus, spec_text=spec_text)
                    rendered = render_review(res.data)
                    if rendered is None:
                        return r.text
                    gated["res"] = res
                    return rendered
            else:
                transform = _render_structured if json_mode else None
            st.reviewed_commits[str(i)] = self.ws.head()
            # 이어갈 때는 세션을 연 리뷰의 추론 강도를 그대로 쓴다. 강도가 바뀌면 Codex 대화 캐시가 깨진다(DESIGN.md 21.3)
            session_effort = (st.review_sessions.get(role) or {}).get("effort") if resume_id else None
            out = ctx.call(role, "review", prompt, Mode.READ, label=f"review{i}_{role}", round_=i,
                           output_schema=REVIEW_SCHEMA if json_mode else None,
                           resume_session=resume_id, fallback_prompt=full_prompt if resume_id else None,
                           transform=transform, effort=session_effort, browser=browser)
            self._remember_session(role, i, out.result,
                                   session_effort if out.result.resumed else ctx.agent_config(role).effort_for("review", i))
            if "res" in gated:
                out = self._check_repeats(i, role, gated["res"], out)
                gates.update_ledger(st, role, i, gated["res"].data["findings"])
                self._downgraded[role] = gated["res"].downgraded
            self._undo_reviewer_changes()
            verdict = parse_verdict(out.text)
            decision = decide(verdict, block_severity=self.cfg.block_severity, tests_block=tests_block,
                              require_tests_pass=self.cfg.require_tests_pass)
            st.last_review = out.file
            st.last_reviews[role] = out.file
            decisions.append((role, decision))
            files.append(out.file)
            if self.multi:
                parts[role] = {"file": out.file, "reviewer_status": verdict.status.value,
                               "downgraded": self._downgraded.get(role),
                               "approved": decision.approved, "blocking": decision.blocking,
                               "must_fix": verdict.must_fix, "severity": verdict.severity,
                               "parse_failed": verdict.parse_failed, "reasons": decision.reasons}
                st.mark_done(sub_key)
                ctx.log(f"[{i}/{self.max_review}] 판정({role}): {decision.describe()}")
                ctx.save()
            else:
                self._single = verdict
        if not self.multi:
            return decisions[0][1], files
        counts = [d.blocking for _, d in decisions if d.blocking is not None]
        reasons = []
        for role, d in decisions:
            reasons.append(f"{role}: {'승인' if d.approved else '반려'}"
                           + (f" (막는 지적 {d.blocking})" if d.blocking else ""))
        merged = Decision(all(d.approved for _, d in decisions), sum(counts) if counts else None, reasons)
        return merged, files

    # ---------------------------------------------------------------- review cost (DESIGN.md 21장)
    def _template_has(self, role: str, step: str, marker: str) -> bool:
        wf = self.ctx.workflow
        r = wf.roles.get(role) if wf is not None else None
        name = r.templates.get(step, step) if r is not None else step
        try:
            return marker in load_template(name, self.ctx.workdir)
        except TemplateError:
            return False

    def _spec_text(self) -> str:
        """요구 근거 관문이 인용을 찾는 곳: 과제 + 설계 문서 (DESIGN.md 25.2)."""
        try:
            design = (self.ctx.workdir / self.ctx.design_doc).read_text(encoding="utf-8", errors="replace")
        except OSError:
            design = ""
        return f"{self.ctx.task}\n\n{design}"

    def _changed_since_review(self, i: int) -> set[str] | None:
        """지난 리뷰 시점 이후 바뀐 파일 (범위 관문, DESIGN.md 22.2). 1라운드거나 기준을 모르면 None."""
        if i < 2:
            return None
        prev = self._reviewed_commit(i - 1)
        if not prev:
            return None
        try:
            out = self.ws.git("diff", "--name-only", prev, "HEAD")
        except GitError:
            return None
        return {ln.strip() for ln in out.splitlines() if ln.strip()}

    def _call_sites(self, diff: str) -> str:
        if not self.cfg.review_packet:
            return "(리뷰 패킷을 끈 실행)"
        sites = call_sites(self.ws, changed_symbols(diff), exclude=(self.ctx.design_doc,),
                           exclude_globs=self.cfg.diff_excludes)
        return sites or "(찾지 못함)"

    def _resumable_session(self, role: str, i: int, json_mode: bool) -> str | None:
        """2라운드부터, 직전 라운드에 쓴 자기 세션이 있고 상한 전이면 그 세션 id."""
        if self.cfg.review_session != "reuse" or i < 2:
            return None
        if not self._template_has(role, "rereview", "{diff}"):
            return None
        s = self.state.review_sessions.get(role) or {}
        if not s.get("id") or s.get("round") != i - 1 or int(s.get("rounds") or 0) >= self.cfg.review_session_rounds:
            return None
        if (s.get("model") or "") != self.ctx.agent_config(role).model_for("review", i):
            return None      # 1회차와 2회차부터의 모델이 다르면 새 세션 (DESIGN.md 30.4)
        return str(s["id"])

    def _remember_session(self, role: str, i: int, result, effort: str | None = None) -> None:
        prev = self.state.review_sessions.get(role) or {}
        if not result.session_id:
            self.state.review_sessions.pop(role, None)
            return
        rounds = int(prev.get("rounds") or 0) + 1 if result.resumed and prev.get("id") == result.session_id else 1
        self.state.review_sessions[role] = {"id": result.session_id, "round": i, "rounds": rounds, "effort": effort or "",
                                            "model": self.ctx.agent_config(role).model_for("review", i)}

    def _history_entry(self, i: int, decision: Decision, files: list[str], test: dict | None) -> dict:
        if not self.multi:
            v = self._single
            assert v is not None
            entry = {"round": i, "file": files[0], "reviewer_status": v.status.value,
                     "approved": decision.approved, "blocking": decision.blocking,
                     "must_fix": v.must_fix, "severity": v.severity,
                     "parse_failed": v.parse_failed, "reasons": decision.reasons,
                     "tests": test.get("status") if test else None}
            if self._downgraded.get(self.reviewer):
                entry["downgraded"] = self._downgraded[self.reviewer]
            if self.state.issues:
                entry["open_issues"] = sum(1 for x in self.state.issues if x["status"] == "open")
            return entry
        parts = self.state.round_reviews.get(str(i), {})
        rows = [{"role": r, **parts[r]} for r in self.reviewers if r in parts]
        severity: dict[str, int] | None = None
        if any(row.get("severity") for row in rows):
            severity = {}
            for row in rows:
                for k, n in (row.get("severity") or {}).items():
                    severity[k] = severity.get(k, 0) + n
        must = [row["must_fix"] for row in rows if row.get("must_fix") is not None]
        all_ok = all(row["reviewer_status"] == "APPROVED" for row in rows)
        return {"round": i, "file": files[0], "files": files,
                "reviewer_status": "APPROVED" if all_ok else "CHANGES_REQUESTED",
                "approved": decision.approved, "blocking": decision.blocking,
                "must_fix": sum(must) if must else None, "severity": severity,
                "parse_failed": any(row.get("parse_failed") for row in rows), "reasons": decision.reasons,
                "tests": test.get("status") if test else None, "reviewers": rows}

    # ---------------------------------------------------------------- repeats (DESIGN.md 26장)
    def _check_repeats(self, i: int, role: str, res, out):
        """새 막는 지적이 닫힌 지적의 반복인지 작은 모델에게 묻고 판정에 적용한다 (26.2). 바뀌면 리뷰 기록을 다시 쓴다."""
        ctx, st = self.ctx, self.state
        if not self.cfg.dedup or DEDUP_ROLE not in ctx.agents:
            return out
        findings = res.data["findings"]
        new = dedup.new_blocking(findings)
        pending = dedup.pending_issues(st, role, findings)
        closed = pending + [x for x in dedup.closed_issues(st) if x["id"] not in {p["id"] for p in pending}]
        if not new or not closed:
            return out
        ctx.log(f"[{i}/{self.max_review}] 중복 확인: 새 막는 지적 {len(new)}개 × 닫힌 지적 {len(closed)}개")
        prompt = ctx.prompt(DEDUP_ROLE, "dedup", new=dedup.format_new(new), closed=dedup.format_closed(closed),
                            output_instruction=dedup.OUTPUT_INSTRUCTION)
        call = ctx.call(DEDUP_ROLE, "dedup", prompt, Mode.READ, label=f"dedup{i}_{role}", round_=i,
                        output_schema=dedup.DEDUP_SCHEMA, optional=True)
        self._undo_reviewer_changes()
        key = f"{i}:{role}"
        if call is None:
            st.dedup[key] = {"result": "failed"}
            return out
        matches = dedup.normalize(call.result.structured, {f["id"] for f in new}, {x["id"] for x in closed})
        if matches is None:
            st.dedup[key] = {"file": call.file, "result": "unreadable"}
            ctx.log(f"[{i}/{self.max_review}] 중복 확인 결과를 읽지 못해 리뷰를 그대로 씁니다 → {call.file}")
            return out
        counts = dedup.apply(findings, matches, {x["id"]: x for x in closed})
        st.dedup[key] = {"file": call.file, "matches": matches, **counts}
        if not matches:
            ctx.log(f"[{i}/{self.max_review}] 중복 확인: 반복 없음")
            return out
        if counts["repeat"]:
            res.downgraded["repeat"] = res.downgraded.get("repeat", 0) + counts["repeat"]
        text = render_review(res.data) or out.text
        ctx.transcript.rewrite(out.file, text, {"dedup": f"재제기 {counts['repeat']} 재발 {counts['recurred']} "
                                                         f"합침 {counts['same']}"})
        ctx.log(f"[{i}/{self.max_review}] 중복 확인: "
                + ", ".join(f"{n}→{m['old_id']}" for n, m in matches.items())
                + f" (재제기 P3 강등 {counts['repeat']}, 재발 표시 {counts['recurred']}, 같은 지적 합침 {counts['same']})")
        return replace(out, text=text)

    def _detect_reverts(self, i: int) -> None:
        """지난 리뷰 이후 변경이 해결된 지적을 가로질러 예전 코드를 되살렸는지 (26.3)."""
        ctx, st = self.ctx, self.state
        key = f"revert:{i}"
        if not self.cfg.dedup or i < 2 or st.is_done(key):
            return
        prev = self._reviewed_commit(i - 1)
        if prev and st.base_commit and st.issues:
            points = [(0, st.base_commit)] + [(k, c) for k in range(1, i - 1) if (c := self._reviewed_commit(k))]
            found = revert.find_reverts(self.ws, prev, points, st.issues, exclude=(ctx.design_doc,))
            if found:
                st.reverts[str(i)] = {"since": prev, "reverts": [r.to_dict() for r in found]}
                ctx.log(f"[{i}/{self.max_review}] 주의: 되돌림 감지: "
                        + ", ".join(f"{r.file}({'/'.join(r.issues)})" for r in found))
        st.mark_done(key)
        ctx.save()

    def _review_texts(self, i: int = 0) -> str:
        """수정 프롬프트에 넣을 지난 리뷰. 리뷰어가 여럿이면 역할별로 이어 붙인다. 중재 뒤 마지막 라운드면 중재 결과를 앞에."""
        st = self.state
        head = ""
        if i and self._focus_round(i):
            head = ("### 중재 결과 (이번이 마지막 라운드)\n\n"
                    + self.ctx.transcript.read(st.arbiter["file"]).strip()
                    + "\n\n**이번 라운드는 유지된 지적("
                    + (", ".join(st.arbiter.get("upheld") or []) or "없음")
                    + ")만 고쳐라. 완화·기각된 지적은 고치지 않아도 된다.**\n\n")
        closed = dedup.closed_block(st, for_fix=True) if self.cfg.dedup else ""
        tail = f"\n\n{closed}" if closed else ""
        if not self.multi or not st.last_reviews:
            return head + self.ctx.transcript.read(st.last_review).rstrip() + tail  # type: ignore[arg-type]
        chunks = []
        for role in self.reviewers:
            f = st.last_reviews.get(role)
            if f:
                chunks.append(f"### {role} 의 리뷰\n\n{self.ctx.transcript.read(f).strip()}")
        return head + "\n\n".join(chunks) + tail

    # ---------------------------------------------------------------- tests
    def _env_drops(self) -> list[str]:
        return env_drops(self.cfg.env_strip, self.cfg.env_keep)

    def _run_tests(self, i: int) -> None:
        ctx, st = self.ctx, self.state
        resolved = resolve_command(ctx.workdir, self.cfg.test_command)
        if resolved is None:
            ctx.log(f"[{i}/{self.max_review}] 테스트: 명령 없음 (건너뜀)")
            return
        command, source = resolved
        ctx.log(f"[{i}/{self.max_review}] 테스트: {command} ({source})")
        result = run_tests(ctx.workdir, command, source, self.cfg.test_timeout_sec, drop_env=self._env_drops())
        # 테스트가 만든 부산물(캐시·임시 파일)은 커밋하지 않는다. 구현은 이미 커밋돼 있다.
        if self.ws.dirty():
            self.ws.restore_clean()
        body = describe_for_prompt(result)
        path = ctx.transcript.save(f"test{i}", body, {
            "agent": "tenure", "adapter": "tenure", "step": "test", "mode": "run", "round": i,
            "duration_sec": result.duration_sec, "exit_code": result.exit_code,
            "ok": result.status == "passed", "test_status": result.status, "command": command,
            "rate_limited": False, "timed_out": result.status == "timeout"})
        st.tests[str(i)] = {**result.to_dict(), "file": path.name}
        st.tests[str(i)].pop("output_tail", None)
        ctx.log(f"[{i}/{self.max_review}] 테스트 {STATUS_KO[result.status]}: {result.summary} → {path.name}")

    def _tests_text(self, i: int) -> str:
        t = self.state.tests.get(str(i))
        if not t:
            return describe_for_prompt(None)
        try:
            return self.ctx.transcript.read(t["file"]).strip()
        except (OSError, KeyError):
            return f"명령: {t.get('command')}\n결과: {STATUS_KO.get(t.get('status'), t.get('status'))}"

    def _tests_for_review(self, i: int) -> str:
        """리뷰·중재 프롬프트의 테스트 결과. 테스트 변경 경고가 있으면 아래에 붙인다 (DESIGN.md 24.1)."""
        warnings = [w for w in (self._guard_text(i), self._revert_text(i)) if w]
        return self._tests_text(i) + "".join(f"\n\n{w}" for w in warnings)

    def _revert_text(self, i: int) -> str:
        g = self.state.reverts.get(str(i))
        return revert.format_reverts(g["reverts"]) if g and g.get("reverts") else ""

    # ---------------------------------------------------------------- test guard (DESIGN.md 24.1)
    def _guard_tests(self, i: int, base: str) -> None:
        ctx, st = self.ctx, self.state
        key = f"guard:{i}"
        if not self.cfg.test_guard or st.is_done(key):
            return
        prev = self._reviewed_commit(i - 1) if i > 1 else None
        frm = prev or base
        signals = [s.to_dict() for s in testguard.scan(self.ws, frm, "HEAD", testguard.repro_files(st.repros))]
        if signals:
            text = testguard.format_signals(signals, "지난 리뷰 이후" if prev else "기준 커밋 이후")
            path = ctx.transcript.save(f"testguard{i}", text, {
                "agent": "tenure", "adapter": "tenure", "step": "testguard", "mode": "run", "round": i,
                "exit_code": 0, "ok": True, "guard_signals": len(signals),
                "rate_limited": False, "timed_out": False})
            st.test_guard[str(i)] = {"since": frm, "signals": signals, "file": path.name}
            kinds = sorted({testguard.KIND_KO.get(s["kind"], s["kind"]) for s in signals})
            ctx.log(f"[{i}/{self.max_review}] 주의: 테스트 변경 감지 {len(signals)}건 ({', '.join(kinds)}) → {path.name}")
        st.mark_done(key)
        ctx.save()

    def _guard_text(self, i: int) -> str:
        g = self.state.test_guard.get(str(i))
        if not g or not g.get("signals"):
            return ""
        try:
            return self.ctx.transcript.read(g["file"]).strip()
        except (OSError, KeyError):
            return testguard.format_signals(g["signals"], "이번 라운드에")

    def _test_instruction(self, i: int, role: str | None = None) -> str:
        ran = bool(self.state.tests.get(str(i)))
        if role is not None and not self.ctx.can_run_commands(role):
            # Claude 읽기 모드는 명령을 못 돌린다 (DESIGN.md 19.6)
            head = "위 테스트는 tenure 가 방금 직접 실행했다. " if ran else "tenure 가 테스트를 돌리지 않았다. "
            tail = ("재현 테스트로 내라." if self.cfg.repro_tests else "P3 로 적어라.")
            return (head + "너는 이 호출에서 명령을 실행할 수 없다(읽기 전용). 코드와 위 테스트 결과로 판단하고, "
                    "실행해 봐야 알 수 있는 의심은 추측으로 반려하지 말고 " + tail)
        if ran:
            return ("위 테스트는 tenure 가 방금 직접 실행했다. 전체 테스트를 다시 돌리지 마라. "
                    "특정 동작을 확인해야 할 때만 그 부분만 좁게 실행해라.")
        return "tenure 가 테스트를 돌리지 않았다. 필요하면 직접 실행해도 된다."

    # ---------------------------------------------------------------- diff
    def _reviewed_commit(self, i: int) -> str | None:
        """i 라운드 리뷰 시점의 HEAD. 기록이 없으면(예전 실행) 그 라운드 구현 커밋을 메시지로 찾는다."""
        c = self.state.reviewed_commits.get(str(i))
        if c:
            return c
        try:
            out = self.ws.git("log", "-1", "--format=%h", f"--grep=^tenure: impl round {i}$", "HEAD")
        except GitError:
            return None
        return out.strip() or None

    def _review_diff(self, i: int, base: str) -> tuple[str, str]:
        full_title = "구현 전체 diff"
        exclude = (self.ctx.design_doc,)
        if self.cfg.review_diff != "incremental" or i <= 1:
            return full_title, build_review_diff(self.ws, base, self.cfg.diff_excludes, self.cfg.max_diff_chars,
                                                 exclude, context=self.cfg.review_packet)
        return self._incremental_diff(i, base)

    def _incremental_diff(self, i: int, base: str) -> tuple[str, str]:
        """지난 리뷰 이후 바뀐 부분. 기준을 못 찾으면 전체 diff."""
        exclude = (self.ctx.design_doc,)
        prev = self._reviewed_commit(i - 1) if i > 1 else None
        if not prev:
            return "구현 전체 diff", build_review_diff(self.ws, base, self.cfg.diff_excludes, self.cfg.max_diff_chars,
                                                     exclude, context=self.cfg.review_packet)
        stat = self.ws.diff_stat(base, "HEAD", exclude=exclude)
        body = diff_body(self.ws, prev, exclude, self.cfg.diff_excludes, self.cfg.max_diff_chars,
                         self.cfg.review_packet)
        note = (f"아래 diff 는 지난 리뷰(커밋 {prev}) 이후 바뀐 부분만이다. 처음부터의 전체 변경은 "
                f"`git diff {base} HEAD` 로 볼 수 있다. 바뀐 부분과 그 영향을 받는 코드 위주로 리뷰해라.\n\n"
                f"[처음부터의 변경 파일 요약]\n{stat.rstrip() or '(없음)'}\n")
        budget = max(1000, self.cfg.max_diff_chars - len(note))
        text = note + "\n" + (truncate_diff(body, budget) if body.strip() else "(지난 리뷰 이후 바뀐 코드 없음)")
        return f"지난 리뷰 이후 바뀐 부분 (diff)", text

    # ---------------------------------------------------------------- repro
    def _handle_repros(self, i: int, review_text: str) -> None:
        ctx, st = self.ctx, self.state
        parsed = parse_repros(review_text, ctx.workdir)
        if not parsed.files and not parsed.rejected:
            return
        record: dict = {"files": [f.path for f in parsed.files], "rejected": parsed.rejected}
        lines = [f"# 리뷰어가 낸 재현 테스트 ({i}회차 리뷰)", ""]
        if parsed.files:
            write_repros(parsed.files, ctx.workdir)
            commit = self.ws.commit_all(f"tenure: repro tests from review {i}")
            record["commit"] = commit
            lines += ["저장한 파일:", *[f"- `{f}`" for f in record["files"]], ""]
            resolved = resolve_command(ctx.workdir, self.cfg.test_command)
            if resolved is None:
                record["status"] = "not_run"
                lines.append("테스트 명령이 없어 실행하지 못했다.")
            else:
                result = run_tests(ctx.workdir, resolved[0], resolved[1], self.cfg.test_timeout_sec,
                                   drop_env=self._env_drops())
                if self.ws.dirty():
                    self.ws.restore_clean()
                record.update(status=result.status, summary=result.summary, command=result.command)
                verdict = {"failed": "재현됨: 지금 코드에서 실패한다. 리뷰어의 지적이 맞다.",
                           "passed": "재현 안 됨: 지금 코드에서도 통과한다. 지적이 틀렸거나 테스트가 문제를 못 짚었다.",
                           "timeout": "시간 초과", "error": "실행 불가(환경 문제일 수 있음)"}[result.status]
                lines += [f"결과: {verdict}", "", describe_for_prompt(result)]
            ctx.log(f"[{i}/{self.max_review}] 재현 테스트 {len(record['files'])}개 저장"
                    + (f" → {STATUS_KO.get(record.get('status'), '실행 안 함')}" if record.get("status") else ""))
        if parsed.rejected:
            lines += ["", "받지 않은 블록:", *[f"- {r}" for r in parsed.rejected]]
            ctx.log(f"[{i}/{self.max_review}] 재현 테스트 거절 {len(parsed.rejected)}개: {parsed.rejected[0]}")
        path = ctx.transcript.save(f"repro{i}", "\n".join(lines), {
            "agent": "tenure", "adapter": "tenure", "step": "repro", "mode": "run", "round": i,
            "exit_code": 0, "ok": True, "repro_status": record.get("status", "none"),
            "repro_files": len(record["files"]), "rate_limited": False, "timed_out": False})
        record["file"] = path.name
        st.repros[str(i)] = record

    def _repro_text(self, i: int) -> str:
        r = self.state.repros.get(str(i))
        if not r or not r.get("files"):
            return "(없음)"
        status = {"failed": "지금 코드에서 실패함 — 재현됨", "passed": "지금 코드에서도 통과함 — 재현 안 됨",
                  "timeout": "시간 초과", "error": "실행 불가", "not_run": "실행 안 함"}.get(r.get("status"), "?")
        files = "\n".join(f"- {f}" for f in r["files"])
        return (f"리뷰어가 아래 테스트를 냈고 tenure 가 작업 폴더에 저장했다 ({status}).\n{files}\n"
                "이 테스트가 통과하도록 코드를 고쳐라. 테스트 자체가 틀렸다고 판단되면 테스트를 고치거나 지우되, "
                "이유를 IMPLEMENTATION_NOTES.md 에 적어라.")

    def _previous_must_fix(self, role: str | None = None) -> str:
        prev = self.state.last_reviews.get(role) if self.multi and role else self.state.last_review
        if not prev:
            return "(첫 리뷰)"
        try:
            text = self.ctx.transcript.read(prev)
        except OSError:
            return "(이전 리뷰를 읽을 수 없음)"
        section = extract_section(text, "반드시 고칠 것")
        return section or "(이전 리뷰에서 '반드시 고칠 것' 섹션을 찾지 못함)"

    # ---------------------------------------------------------------- stall
    def _blocking_series(self) -> list[int | None]:
        base = self.state.stall_baseline_round
        return [h.get("blocking") for h in self.state.review_history if h["round"] >= base]

    def _is_stalled(self) -> bool:
        return stalled(self._blocking_series(), self.cfg.stall_rounds)

    def _test_hint(self) -> str:
        c = (self.cfg.test_command or "auto").strip()
        if c.lower() == "none":
            return "(이번 실행은 tenure 테스트를 끄도록 설정되어 있다. 그래도 테스트는 작성해라.)"
        if c.lower() != "auto":
            return f"(이번 실행은 설정에 따라 `{c}` 로 테스트한다. tenure.toml 의 값보다 이것이 우선한다.)"
        return ""

    # ---------------------------------------------------------------- helpers
    def _preserve_pending_changes(self, i: int) -> None:
        """리뷰 직전에 커밋 안 된 변경이 있으면 커밋해 둔다.

        resume 전에 사용자가 고친 파일 등. 그대로 두면 리뷰 후 '리뷰어가 바꾼 것'으로 오인되어 지워진다.
        """
        if not self.ws.dirty():
            return
        files = self.ws.dirty_files()
        commit = self.ws.commit_all(f"tenure: uncommitted changes before review {i}")
        self.ctx.log(f"리뷰 전에 커밋되지 않은 변경 {len(files)}개를 보존 커밋함 ({commit}): "
                     f"{', '.join(files[:5])}{' ...' if len(files) > 5 else ''}")

    def _undo_reviewer_changes(self) -> None:
        if not self.ws.dirty():
            return
        files = self.ws.dirty_files()
        self.ctx.log(f"경고: 리뷰어가 파일을 변경함 → 되돌림 ({', '.join(files[:5])}{' ...' if len(files) > 5 else ''})")
        self.ws.restore_clean()


def _render_structured(result) -> str:
    """JSON 판정을 기존 마크다운 리뷰로. 못 읽으면 본문 그대로 (DESIGN.md 21.2)."""
    rendered = render_review(result.structured) if result.structured is not None else None
    return rendered or result.text


def extract_section(text: str, title: str) -> str:
    """'## <title>' 섹션 본문 (다음 '## ' 전까지)."""
    lines = text.splitlines()
    out: list[str] = []
    inside = False
    for ln in lines:
        if ln.startswith("## "):
            if inside:
                break
            inside = title in ln
            continue
        if inside:
            out.append(ln)
    return "\n".join(out).strip()


REPRO_INSTRUCTION = """'반드시 고칠 것' 중 코드로 재현할 수 있는 것은, 지금 코드에서 실패하는 테스트를 아래 형식으로 함께 내라.
tenure 가 새 파일로 저장해 실제로 실패하는지 돌려 보고, 구현자는 이 테스트를 통과시켜야 한다.

<repro path="tests/test_repro_짧은이름.py">
(프로젝트의 테스트 방식대로 작성한, 지금은 실패하는 테스트 코드)
</repro>

- 프로젝트의 테스트 폴더와 프레임워크를 따라라. 이미 있는 파일 경로는 쓰지 마라(새 파일만).
- 한 번에 최대 5개. 코드로 재현하기 어려운 지적은 글로만 써도 된다."""
