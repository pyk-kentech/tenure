"""그래프 형식 워크플로의 길 찾기와 검사 (DESIGN.md 36.30 M45).

블록(explore·debate·build·audit) 사이를 next·갈림(branch)·이어서 고치기(continue)·나란히(parallel)로 잇는다.
반복은 앞쪽 단계를 다시 가리키는 선이고, 각 단계는 max_visits 번까지만 돈다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tenure.workflow import Step

READ_ONLY_KINDS = ("explore", "audit")
ONCE_KINDS = ("debate", "build")


def owned_branches(steps: list["Step"]) -> set[str]:
    """나란히 단계가 가진 가지(이들은 목록 순서로 흘러 들어가지 않는다)."""
    return {b for s in steps if s.kind == "parallel" for b in s.branches}


def start_id(steps: list["Step"]) -> str:
    owned = owned_branches(steps)
    return next(s.id for s in steps if s.id not in owned)


def default_next(steps: list["Step"], step: "Step") -> str:
    """next 가 없을 때: 목록에서 다음 단계(나란히의 가지는 건너뜀), 없으면 end."""
    from tenure.workflow import END

    owned = owned_branches(steps)
    i = steps.index(step)
    return next((s.id for s in steps[i + 1:] if s.id not in owned), END)


def targets(steps: list["Step"], step: "Step") -> list[str]:
    """이 단계에서 나가는 선의 대상 (가지 포함)."""
    if step.kind == "branch":
        return [t for t in (step.yes, step.no) if t]
    out = [step.next or default_next(steps, step)]
    if step.kind == "parallel":
        out += step.branches
    return out


def check_graph(steps: list["Step"], err) -> None:
    from tenure.workflow import END

    ids = [s.id for s in steps]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        raise err(f"그래프 형식에서는 단계 id 가 겹치면 안 됩니다: {', '.join(dup)} (id 를 다르게 주세요)")
    by_id = {s.id: s for s in steps}
    kinds = [s.kind for s in steps]
    for k in ONCE_KINDS:
        if kinds.count(k) > 1:
            raise err(f"{k} 단계는 하나만 둘 수 있습니다")
    for s in steps:
        if s.kind in ONCE_KINDS and s.max_visits not in (None, 1):
            raise err(f"{s.id}: {s.kind} 단계는 한 번만 돕니다 (max_visits 를 쓸 수 없음)")
        if s.kind == "branch" and (s.yes is None or s.no is None):
            raise err(f"{s.id}: 갈림은 yes 와 no 를 모두 정해야 합니다")
        for t in targets(steps, s):
            if t != END and t not in by_id:
                raise err(f"{s.id}: 없는 단계를 가리킵니다: {t}")
    owned = owned_branches(steps)
    for s in steps:
        if s.kind != "parallel":
            continue
        for b in s.branches:
            t = by_id.get(b)
            if t is None:
                raise err(f"{s.id}: 없는 단계를 가지로 둡니다: {b}")
            if t.kind not in READ_ONLY_KINDS:
                raise err(f"{s.id}: 나란히 가지는 읽기만 하는 단계(살펴보기·감사)여야 합니다: {b} ({t.kind})")
            if t.next is not None:
                raise err(f"{s.id}: 나란히 가지({b})에는 next 를 쓰지 않습니다 (모두 끝나면 나란히의 next 로 갑니다)")
        if sum(1 for p in steps if p.kind == "parallel" and set(p.branches) & set(s.branches)) > 1:
            raise err(f"{s.id}: 한 단계가 두 나란히의 가지가 될 수 없습니다")
    for s in steps:
        for t in targets(steps, s):
            if t in owned and not (s.kind == "parallel" and t in s.branches):
                raise err(f"{s.id}: 나란히 가지({t})로 바로 갈 수 없습니다 (그 나란히 단계로 가세요)")
    if "build" not in kinds:
        for s in steps:
            if s.kind in ("continue", "audit") or (s.kind == "branch" and s.check in ("approved", "tests_pass")):
                raise err(f"{s.id}: 구현+리뷰(build) 단계가 있어야 쓸 수 있습니다")
    if any(s.kind == "branch" and s.check == "audit_clean" for s in steps) and "audit" not in kinds:
        raise err("감사 결과로 갈리는 갈림(audit_clean)에는 감사 단계가 있어야 합니다")
    seen: set[str] = set()
    todo = [start_id(steps)]
    while todo:
        cur = todo.pop()
        if cur == END or cur in seen:
            continue
        seen.add(cur)
        todo += targets(steps, by_id[cur])
    lost = [i for i in ids if i not in seen]
    if lost:
        raise err(f"시작에서 닿지 않는 단계가 있습니다: {', '.join(lost)}")
