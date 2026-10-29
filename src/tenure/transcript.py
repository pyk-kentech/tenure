"""발언 기록. <workdir>/.tenure/runs/<run_id>/NN_label.md 로 저장한다.

각 파일 상단에는 메타데이터를 HTML 주석으로 넣는다:
    <!-- tenure
    agent: claude
    step: propose
    ...
    -->
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

HEADER_START = "<!-- tenure"
LEGACY_HEADER_START = "<!-- duet"      # 이름 바꾸기 전 기록 (DESIGN.md 36.11)
HEADER_END = "-->"
_FILE_RE = re.compile(r"^(\d{2,})_(.+)\.md$")


@dataclass
class Entry:
    path: Path
    seq: int
    label: str
    meta: dict[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    def body(self) -> str:
        return split_header(self.path.read_text(encoding="utf-8"))[1]


class Transcript:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.n = max((e.seq for e in self.entries()), default=0)

    # ---------------------------------------------------------------- write
    def save(self, label: str, text: str, meta: Mapping[str, Any] | None = None) -> Path:
        self.n += 1
        safe = re.sub(r"[^\w.\-]+", "_", label).strip("_") or "entry"
        path = self.dir / f"{self.n:02d}_{safe}.md"
        full_meta = {"timestamp": dt.datetime.now().isoformat(timespec="seconds"), **(meta or {})}
        path.write_text(render_header(full_meta) + (text or "").rstrip("\n") + "\n", encoding="utf-8")
        return path

    def rewrite(self, name: str, text: str, extra_meta: Mapping[str, Any] | None = None) -> None:
        """기록의 본문을 바꾼다. 헤더(메타)는 유지하고 extra_meta 를 더한다 (재제기 확인 뒤 리뷰, DESIGN.md 26.2)."""
        path = self.dir / name
        content = path.read_text(encoding="utf-8")
        meta, _ = split_header(content)
        if not meta:
            path.write_text((text or "").rstrip("\n") + "\n", encoding="utf-8")
            return
        path.write_text(render_header({**meta, **(extra_meta or {})}) + (text or "").rstrip("\n") + "\n",
                        encoding="utf-8")

    # ---------------------------------------------------------------- read
    def entries(self) -> list[Entry]:
        out: list[Entry] = []
        if not self.dir.is_dir():
            return out
        with os.scandir(self.dir) as it:
            found = sorted((e for e in it if _FILE_RE.match(e.name)), key=lambda e: e.name)
        for e in found:
            if not e.is_file():
                continue
            m = _FILE_RE.match(e.name)
            out.append(Entry(path=Path(e.path), seq=int(m.group(1)), label=m.group(2), meta=_cached_header(e)))
        out.sort(key=lambda e: e.seq)
        return out

    def read(self, name: str) -> str:
        """파일 이름(예: 01_propose_claude.md)으로 본문(헤더 제외)을 읽는다."""
        return split_header((self.dir / name).read_text(encoding="utf-8"))[1]

    def __iter__(self) -> Iterator[Entry]:
        return iter(self.entries())


# -------------------------------------------------------------------- header
_HEADERS: dict[str, tuple[tuple[int, int], dict]] = {}
_MAX_HEADERS = 4096


def _cached_header(e: os.DirEntry) -> dict:
    """기록 파일은 한 번 쓰면 바뀌지 않는다. 폴더 목록의 (수정 시각, 크기)가 같으면 머리말을 다시 읽지 않는다:
    진행 중인 실행의 상세는 2초마다 불린다 (DESIGN.md 36.38)."""
    st = e.stat()
    key = (st.st_mtime_ns, st.st_size)
    hit = _HEADERS.get(e.path)
    if hit is not None and hit[0] == key:
        return dict(hit[1])
    meta = read_header(Path(e.path))
    if len(_HEADERS) >= _MAX_HEADERS:
        _HEADERS.clear()
    _HEADERS[e.path] = (key, meta)
    return dict(meta)


def render_header(meta: Mapping[str, Any]) -> str:
    lines = [HEADER_START]
    for k, v in meta.items():
        if isinstance(v, bool):
            v = "true" if v else "false"
        elif isinstance(v, float):
            v = f"{v:.1f}"
        s = str(v).replace("\n", " ")
        lines.append(f"{k}: {s}")
    lines.append(HEADER_END)
    return "\n".join(lines) + "\n\n"


def split_header(content: str) -> tuple[dict[str, str], str]:
    """(메타, 본문). 헤더가 없으면 ({}, 원문)."""
    start = HEADER_START if content.startswith(HEADER_START) else LEGACY_HEADER_START if content.startswith(LEGACY_HEADER_START) else None
    if start is None:
        return {}, content
    end = content.find(HEADER_END)
    if end < 0:
        return {}, content
    head = content[len(start):end]
    meta: dict[str, str] = {}
    for line in head.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    body = content[end + len(HEADER_END):]
    return meta, body.lstrip("\n")


def read_header(path: Path) -> dict[str, str]:
    try:
        with path.open(encoding="utf-8") as f:
            head = f.read(4096)
    except OSError:
        return {}
    return split_header(head)[0]
