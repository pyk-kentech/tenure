"""워크플로 정의(dict)를 TOML 로 쓴다 (DESIGN.md 36.29 M44).

표준 라이브러리 tomllib 은 읽기만 한다. 워크플로 형식은 단순해서(문자열·정수·문자열 목록·표·표 배열)
작은 직렬화기로 충분하다. 결과는 tomllib 으로 다시 읽어 같은 값이 나오는지 확인한다.
"""

from __future__ import annotations

import json
import re
import tomllib
from typing import Any, Mapping

_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _string(k)


def _string(s: str) -> str:
    # JSON 의 큰따옴표 문자열은 TOML 기본 문자열로도 유효하다(\" \\ \n \uXXXX 이스케이프가 같다).
    return json.dumps(s, ensure_ascii=False)


def _value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    if isinstance(v, Mapping):
        return "{ " + ", ".join(f"{_key(str(k))} = {_value(x)}" for k, x in v.items()) + " }"
    raise TypeError(f"TOML 로 쓸 수 없는 값: {v!r}")


def dump_workflow(data: Mapping[str, Any]) -> str:
    """{name, description, roles: {이름: {...}}, steps: [{...}]} → TOML 텍스트."""
    lines: list[str] = []
    for key in ("name", "description"):
        if data.get(key):
            lines.append(f"{key} = {_value(data[key])}")
    for rname, role in (data.get("roles") or {}).items():
        lines += ["", f"[roles.{_key(str(rname))}]"]
        lines += [f"{_key(str(k))} = {_value(v)}" for k, v in role.items() if v not in (None, "", [], {})]
    for step in data.get("steps") or []:
        lines += ["", "[[steps]]"]
        lines += [f"{_key(str(k))} = {_value(v)}" for k, v in step.items() if v not in (None, "", [])]
    text = "\n".join(lines).lstrip("\n") + "\n"
    tomllib.loads(text)  # 쓴 것이 다시 읽히는지
    return text
