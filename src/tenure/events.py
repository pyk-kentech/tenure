"""실행별 이벤트 기록 (DESIGN.md 36.3).

`.tenure/runs/<실행>/events.jsonl` 에 한 줄에 하나씩, 순번(seq)을 붙여 덧붙인다. 상태 전환·단계 시작/끝·로그가
모두 여기에 남아, 화면은 '마지막으로 본 seq 다음'만 받아 가면 된다(SSE 또는 폴링). 순번이 끊기면(파일을 다시
만들었거나 놓쳤으면) 받는 쪽은 실행 상세를 다시 읽는다.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from typing import Any

EVENTS_FILE = "events.jsonl"
MAX_READ = 1000
_TAIL = 65536
_SEQ_RE = re.compile(rb'^\{"seq": (\d+)')

# kind 값
STATE = "state"            # status·phase·step 이 바뀜
STEP_START = "step_start"  # 에이전트 호출 시작
STEP_END = "step_end"      # 에이전트 호출 끝 (outcome·reason)
LOG = "log"                # 콘솔에 찍힌 줄
ATTENTION = "attention"    # 사람이 볼 일 (한도 대기, 실패, 미승인 …)


def _last_record(path: Path) -> dict[str, Any] | None:
    """파일 끝 64KB 에서 마지막으로 읽히는 줄."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _TAIL))
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            return rec
    return None


def _last_seq(path: Path) -> int:
    rec = _last_record(path)
    try:
        return int((rec or {})["seq"])
    except (KeyError, TypeError, ValueError):
        return 0


class EventLog:
    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / EVENTS_FILE
        self._lock = threading.Lock()
        self._seq = _last_seq(self.path)

    def emit(self, kind: str, **data: Any) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, **data}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            except OSError:
                pass                   # 기록 실패가 실행을 멈추게 하지 않는다
            return rec


_SHARED: dict[str, EventLog] = {}
_SHARED_LOCK = threading.Lock()


def for_run(run_dir: Path) -> EventLog:
    """같은 실행 폴더는 같은 EventLog 를 쓴다(한 프로세스 안에서 seq 가 겹치지 않게)."""
    key = str(Path(run_dir).resolve())
    with _SHARED_LOCK:
        log = _SHARED.get(key)
        if log is None:
            log = _SHARED[key] = EventLog(Path(run_dir))
        return log


def _seq_of(line: bytes) -> int | None:
    """줄 머리의 seq 를 JSON 을 풀지 않고 읽는다(emit 이 쓰는 형식). 못 읽으면 None."""
    m = _SEQ_RE.match(line)
    return int(m.group(1)) if m else None


def _read_from(path: Path, since: int) -> bytes:
    """seq 가 since 이하인 줄부터 끝까지의 바이트. 끝에서부터 창을 넓혀 가며 찾는다:
    실시간 폴링은 끝의 몇 줄만 읽으면 된다 (DESIGN.md 36.38)."""
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        window = _TAIL
        while True:
            start = max(0, size - window)
            f.seek(start)
            data = f.read(size - start)
            if start == 0:
                return data
            nl = data.find(b"\n")
            if nl >= 0:
                seq = _seq_of(data[nl + 1:])
                if seq is not None and seq <= since:
                    return data[nl + 1:]
            window *= 4


def read_events(run_dir: Path, since: int = 0, limit: int = MAX_READ) -> list[dict[str, Any]]:
    """seq > since 인 이벤트를 순서대로 (최대 limit 개)."""
    path = Path(run_dir) / EVENTS_FILE
    out: list[dict[str, Any]] = []
    try:
        if since <= 0:                     # 처음부터: 필요한 만큼만 앞에서 읽는다
            with path.open("rb") as f:
                lines = [line for _, line in zip(range(limit), f)]
        else:
            lines = _read_from(path, since).splitlines()
    except OSError:
        return []
    for line in lines:
        seq = _seq_of(line)
        if seq is not None and seq <= since:
            continue                       # 이미 본 줄은 풀지 않는다
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and int(rec.get("seq", 0)) > since:
            out.append(rec)
            if len(out) >= limit:
                break
    return out


def last_event(run_dir: Path) -> dict[str, Any] | None:
    """마지막 이벤트. 파일 끝만 읽는다(플로 목록이 실행마다 부른다, 36.38)."""
    rec = _last_record(Path(run_dir) / EVENTS_FILE)
    return rec if rec and rec.get("seq") else None
