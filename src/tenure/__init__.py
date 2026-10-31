"""tenure — 설계 토론 + 교차 리뷰 에이전트 팀."""

__version__ = "0.2.0"

from tenure.legacy import apply_env_aliases as _apply_env_aliases

_apply_env_aliases()   # 옛 DUET_* 환경 변수도 읽는다 (DESIGN.md 36.11)
