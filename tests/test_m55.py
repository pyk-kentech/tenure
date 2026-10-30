"""실제 실행에서 찾은 표시 오류 (DESIGN.md 36.40): --max-review 가 워크플로 요약 줄과 run.json 에도 보인다."""

from __future__ import annotations

from tenure.cli import main
from tenure.state import RunState, list_run_dirs


def test_cli_review_cap_shows_in_workflow_summary(tmp_path, capsys):
    wd = tmp_path / "calc"
    assert main(["run", "계산기", "--workdir", str(wd), "--mock", "--workflow", "quick", "--max-review", "3"]) == 0
    out = capsys.readouterr().out
    line = next(x for x in out.splitlines() if "워크플로: quick" in x)
    assert "최대 3회" in line and "최대 2회" not in line
    assert "리뷰 최대 3회" in out
    assert "토론" not in out                                    # quick 에는 토론 단계가 없다
    st = RunState.load(list_run_dirs(wd)[0])
    assert [s.get("max_review") for s in st.workflow["steps"] if s["kind"] == "build"] == [3]


def test_template_cap_kept_without_cli_value(tmp_path, capsys):
    wd = tmp_path / "calc"
    assert main(["run", "계산기", "--workdir", str(wd), "--mock", "--workflow", "quick"]) == 0
    out = capsys.readouterr().out
    assert "최대 2회" in next(x for x in out.splitlines() if "워크플로: quick" in x)
    assert "리뷰 최대 2회" in out
