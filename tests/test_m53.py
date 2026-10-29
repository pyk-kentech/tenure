"""M53 반응·자원 최적화 (DESIGN.md 36.38): 이벤트를 끝에서 읽기, 마지막 이벤트, 기록 머리말 기억."""

from __future__ import annotations

import json

from tenure import events
from tenure.transcript import Transcript, render_header


def _write(rd, n, *, text="줄 " * 40):
    with (rd / events.EVENTS_FILE).open("w", encoding="utf-8") as f:
        for s in range(1, n + 1):
            f.write(json.dumps({"seq": s, "ts": "t", "kind": "log", "text": text}, ensure_ascii=False) + "\n")


def test_read_from_the_tail_matches_a_full_scan(tmp_path):
    _write(tmp_path, 5000)                                     # 64KB 창보다 훨씬 크다
    for since in (0, 1, 999, 2500, 4990, 4999, 5000, 6000):
        got = [e["seq"] for e in events.read_events(tmp_path, since=since, limit=50)]
        assert got == list(range(since + 1, min(since + 50, 5000) + 1)), since


def test_read_skips_broken_lines_and_old_format(tmp_path):
    p = tmp_path / events.EVENTS_FILE
    p.write_text('{"seq": 1, "kind": "a"}\nnot json\n{"kind": "b", "seq": 2}\n{"seq": 3, "kind": "c"}\n{"seq": 4',
                 encoding="utf-8")
    assert [e["seq"] for e in events.read_events(tmp_path, since=0)] == [1, 2, 3]
    assert [e["seq"] for e in events.read_events(tmp_path, since=1)] == [2, 3]
    assert events.read_events(tmp_path / "없음", since=3) == []


def test_last_event_reads_only_the_end(tmp_path):
    _write(tmp_path, 3000)
    with (tmp_path / events.EVENTS_FILE).open("a", encoding="utf-8") as f:
        f.write('{"seq": 30')                                   # 쓰는 중인 줄
    assert events.last_event(tmp_path)["seq"] == 3000
    assert events.EventLog(tmp_path).emit("log", text="x")["seq"] == 3001
    assert events.last_event(tmp_path / "없음") is None


def test_transcript_headers_are_remembered_until_the_file_changes(tmp_path, monkeypatch):
    from tenure import transcript

    f = tmp_path / "01_review_codex.md"
    f.write_text(render_header({"step": "review"}) + "\n본문\n", encoding="utf-8")
    reads = []
    real = transcript.read_header
    monkeypatch.setattr(transcript, "read_header", lambda p: reads.append(p) or real(p))
    assert Transcript(tmp_path).entries()[0].meta["step"] == "review"
    assert Transcript(tmp_path).entries()[0].meta["step"] == "review"
    assert len(reads) == 1
    f.write_text(render_header({"step": "rereview"}) + "\n더 긴 본문\n", encoding="utf-8")
    assert Transcript(tmp_path).entries()[0].meta["step"] == "rereview"
    assert len(reads) == 2
