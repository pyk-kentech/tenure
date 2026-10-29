"""워크플로 노드 편집기가 쓰는 API: 정의 읽기·검사·저장 (DESIGN.md 36.29 M44).

검사는 엔진의 parse_workflow 가 한다. 화면은 그래프를 워크플로 dict 로 바꿔 보내기만 한다.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Mapping

from tenure.config import CLASSIC_WORKFLOW, WORKFLOW_NAME_RE, Config, ConfigError, load_config
from tenure.registry import tenure_home
from tenure.workflow import (
    PROJECT_DIR,
    WorkflowError,
    Workflow,
    _search,
    check_templates,
    classic,
    describe,
    parse_workflow,
)
from tenure.ui.dashboard import ApiError
from tenure.workflow_toml import dump_workflow

SCOPES = ("project", "user")


def _workdir(d: Any, project: Any) -> Path | None:
    """프로젝트 폴더. 데스크톱 앱의 작업공간은 엔진에 아직 등록되지 않았을 수 있다(흐름을 처음 돌릴 때 등록됨)."""
    return workdir_of(d, project)


def workdir_of(d: Any, project: Any) -> Path | None:
    if not project:
        return None
    try:
        return d.project(project)
    except ApiError:
        p = Path(str(project)).expanduser()
        if p.is_dir():
            return p.resolve()
        raise


def _config(wd: Path | None) -> Config:
    try:
        return load_config(wd)
    except ConfigError:
        return Config()


def _definition(wf: Workflow) -> dict[str, Any]:
    """편집기가 다시 그릴 수 있는 정의(검사를 거친 정규형)."""
    return {"name": wf.name, "description": wf.description,
            "roles": {n: r.to_dict() for n, r in wf.roles.items()},
            "steps": [s.to_dict() for s in wf.steps],
            **({"editor": wf.editor} if wf.editor else {})}


def workflow_get(d: Any, project: Any, name: Any) -> dict[str, Any]:
    wd = _workdir(d, project)
    name = str(name or "").strip()
    if name == CLASSIC_WORKFLOW:
        wf = classic(_config(wd))
        return {"id": name, "source": "config", "path": "", "definition": _definition(wf)}
    if not WORKFLOW_NAME_RE.match(name):
        raise ApiError(400, f"워크플로 이름이 잘못되었습니다: {name!r}")
    found = _search(name, wd)
    if found is None:
        raise ApiError(404, f"워크플로를 찾을 수 없습니다: {name}")
    text, source, path = found
    try:
        data = tomllib.loads(text)
        wf = parse_workflow(data, name_hint=name, source=source, where=path)
    except (tomllib.TOMLDecodeError, WorkflowError) as e:
        raise ApiError(400, str(e)) from e
    return {"id": name, "source": source, "path": path, "definition": _definition(wf)}


def _parse(payload: Mapping[str, Any], wd: Path | None) -> Workflow:
    data = payload.get("workflow")
    if not isinstance(data, Mapping):
        raise ApiError(400, "워크플로 정의가 없습니다.")
    wf = parse_workflow(data, name_hint=str(payload.get("name") or "workflow"), source="file",
                        where="편집 중인 워크플로")
    check_templates(wf, wd)
    return wf


def workflow_check(d: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    wd = _workdir(d, payload.get("project"))
    try:
        wf = _parse(payload, wd)
    except WorkflowError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "summary": wf.summary(), "workflow": describe(wf)}


def workflow_save(d: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    wd = _workdir(d, payload.get("project"))
    name = str(payload.get("name") or "").strip()
    if not WORKFLOW_NAME_RE.match(name) or name == CLASSIC_WORKFLOW:
        raise ApiError(400, "저장 이름은 영문 소문자·숫자로 시작하고 소문자·숫자·_·- 40자 이하여야 합니다 (classic 은 쓸 수 없음).")
    scope = str(payload.get("scope") or "project")
    if scope not in SCOPES:
        raise ApiError(400, f"저장 위치가 잘못되었습니다: {scope!r}")
    if scope == "project" and wd is None:
        raise ApiError(400, "프로젝트에 저장하려면 프로젝트 폴더가 필요합니다.")
    try:
        wf = _parse(payload, wd)
    except WorkflowError as e:
        raise ApiError(400, str(e)) from e
    folder = (wd / PROJECT_DIR) if scope == "project" and wd is not None else tenure_home() / "workflows"
    path = folder / f"{name}.toml"
    if path.exists() and not payload.get("overwrite"):
        raise ApiError(409, f"같은 이름의 워크플로가 이미 있습니다: {path}")
    text = dump_workflow(_definition(wf))
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"id": name, "scope": scope, "path": str(path), "summary": wf.summary()}
