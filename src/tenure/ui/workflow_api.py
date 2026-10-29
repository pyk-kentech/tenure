"""워크플로 노드 편집기가 쓰는 API: 정의 읽기·검사·저장 (DESIGN.md 36.29 M44).

검사는 엔진의 parse_workflow 가 한다. 화면은 그래프를 워크플로 dict 로 바꿔 보내기만 한다.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any, Mapping

from tenure.config import AUTO_WORKFLOW, CLASSIC_WORKFLOW, WORKFLOW_NAME_RE, Config, ConfigError, load_config
from tenure.registry import tenure_home
from tenure.workflow import (
    PROJECT_DIR,
    WorkflowError,
    Workflow,
    _search,
    builtin_names,
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


def _slug(title: str) -> str:
    """보이는 이름에서 파일 이름 후보를 만든다. 한글처럼 못 쓰는 글자만 있으면 "workflow"."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    return s if s and WORKFLOW_NAME_RE.match(s) else "workflow"


def _free_id(base: str, folder: Path) -> str:
    """폴더에 없고 내장·classic·auto 를 가리지 않는 파일 이름 (base, base-2, base-3 …)."""
    taken = {CLASSIC_WORKFLOW, AUTO_WORKFLOW, *builtin_names()}
    for n in range(1, 1000):
        wid = base if n == 1 else f"{base[:36]}-{n}"
        if wid not in taken and not (folder / f"{wid}.toml").exists():
            return wid
    raise ApiError(409, "저장할 파일 이름을 정하지 못했습니다. 이름을 바꿔 보세요.")


def workflow_save(d: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    """저장. title(보이는 이름, 한글 가능)만 주면 파일 이름은 엔진이 정한다(DESIGN.md 36.37 M52).
    id 를 주면 그 파일에 저장한다(덮어쓰려면 overwrite). 옛 호출처럼 name 만 주면 name 이 파일 이름."""
    wd = _workdir(d, payload.get("project"))
    title = str(payload.get("title") or "").strip()
    wid = str(payload.get("id") or ("" if title else payload.get("name")) or "").strip()
    if len(title) > 80:
        raise ApiError(400, "이름은 80자까지 쓸 수 있습니다.")
    if wid and (not WORKFLOW_NAME_RE.match(wid) or wid == CLASSIC_WORKFLOW):
        raise ApiError(400, "파일 이름은 영문 소문자·숫자·_·- 로 40자까지 쓸 수 있습니다 (classic 은 쓸 수 없음).")
    if not wid and not title:
        raise ApiError(400, "이름을 입력하세요.")
    scope = str(payload.get("scope") or "project")
    if scope not in SCOPES:
        raise ApiError(400, f"저장 위치가 잘못되었습니다: {scope!r}")
    if scope == "project" and wd is None:
        raise ApiError(400, "프로젝트에 저장하려면 프로젝트 폴더가 필요합니다.")
    data = payload.get("workflow")
    if title and isinstance(data, Mapping):
        payload = {**payload, "workflow": {**data, "name": title}}
    try:
        wf = _parse(payload, wd)
    except WorkflowError as e:
        raise ApiError(400, str(e)) from e
    folder = (wd / PROJECT_DIR) if scope == "project" and wd is not None else tenure_home() / "workflows"
    wid = wid or _free_id(_slug(title), folder)
    path = folder / f"{wid}.toml"
    if path.exists() and not payload.get("overwrite"):
        raise ApiError(409, f"같은 이름의 워크플로가 이미 있습니다: {path}")
    text = dump_workflow(_definition(wf))
    folder.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"id": wid, "name": wf.name, "scope": scope, "path": str(path), "summary": wf.summary()}
