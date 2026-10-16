import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from pulso import store
from pulso.importers import transcripts

VTT = """WEBVTT

1
00:00:01.000 --> 00:00:04.500
<v Ana Souza>Bom dia, vamos revisar o prazo da entrega.</v>

2
00:00:05.000 --> 00:00:09.000
<v Davi>Eu acho que sexta não fecha.

00:01:00.000 --> 00:01:03.000
Bruno: Precisa fechar, o cliente cobrou.
"""

SRT = """1
00:00:01,000 --> 00:00:03,000
Ana: Olá

2
00:00:04,000 --> 00:00:06,500
Tudo certo?
"""


def test_vtt_with_voice_tags_and_prefixes():
    tr = transcripts.parse(VTT, "x.vtt")
    assert [s.speaker for s in tr.segments] == ["Ana Souza", "Davi", "Bruno"]
    assert tr.segments[0].text.startswith("Bom dia")
    assert tr.segments[2].start_s == 60.0 and tr.duration_s == 63.0


def test_srt():
    tr = transcripts.parse(SRT, "x.srt")
    assert len(tr.segments) == 2
    assert tr.segments[0].speaker == "Ana" and tr.segments[1].speaker is None
    assert tr.segments[1].end_s == 6.5


def test_whisper_json_and_plain():
    js = json.dumps({"segments": [{"start": 0.0, "end": 2.0, "text": " oi", "speaker": "SPEAKER_00"},
                                  {"start": 2.0, "end": 4.0, "text": "tudo bem"}]})
    tr = transcripts.parse(js, "x.json")
    assert tr.segments[0].speaker == "SPEAKER_00" and tr.segments[1].text == "tudo bem"
    plain = "[00:00:05] Ana: começamos\n[00:00:20] Davi: ok\n"
    tp = transcripts.parse(plain, "x.txt")
    assert tp.segments[0].end_s == 20.0 and tp.segments[1].speaker == "Davi"


def test_import_with_explicit_start(tmp_path: Path):
    conn = store.connect(":memory:")
    f = tmp_path / "reuniao.vtt"
    f.write_text(VTT, encoding="utf-8")
    start = datetime(2026, 10, 16, 14, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))
    eid = transcripts.import_file(conn, f, start=start, title="Planning")
    ev = store.events(conn, 0, 2**62, kinds=["meeting"])[0]
    assert ev.id == eid and ev.title == "Planning" and ev.attrs["start_source"] == "explicit"
    segs = transcripts.segments_for(conn, eid)
    assert segs[0]["start_ms"] == int(start.timestamp() * 1000) + 1000
    # idempotent re-import
    assert transcripts.import_file(conn, f, start=start) == eid
    assert len(transcripts.segments_for(conn, eid)) == 3


def test_import_anchors_to_calendar(tmp_path: Path):
    conn = store.connect(":memory:")
    f = tmp_path / "call.vtt"
    f.write_text(VTT, encoding="utf-8")
    mtime = f.stat().st_mtime
    cal_start = int((mtime - 63 - 120) * 1000)  # calendar says it started 2 min before the guess
    store.upsert_event(conn, store.Event("cal:x", "calendar", cal_start, cal_start + 3600_000, "1:1 Ana", "ics",
                                         {"attendees": ["Ana Souza"]}))
    eid = transcripts.import_file(conn, f)
    ev = [e for e in store.events(conn, 0, 2**62, kinds=["meeting"]) if e.id == eid][0]
    assert ev.start_ms == cal_start and ev.attrs["start_source"] == "calendar:cal:x"
    assert ev.title == "1:1 Ana"


ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:daily-1
DTSTART;TZID=America/Sao_Paulo:20261012T093000
DTEND;TZID=America/Sao_Paulo:20261012T094500
RRULE:FREQ=DAILY;COUNT=5
SUMMARY:Daily
ORGANIZER:mailto:davi@example.com
ATTENDEE;CN=Ana:mailto:ana@example.com
ATTENDEE;CN=Bruno:mailto:bruno@example.com
END:VEVENT
BEGIN:VEVENT
UID:allday
DTSTART;VALUE=DATE:20261013
SUMMARY:Feriado
END:VEVENT
END:VCALENDAR
"""


def test_ics_recurring(tmp_path: Path):
    pytest.importorskip("icalendar")
    from pulso.importers import calendar_ics

    conn = store.connect(":memory:")
    f = tmp_path / "cal.ics"
    f.write_text(ICS)
    n = calendar_ics.import_ics(conn, f, "America/Sao_Paulo", date(2026, 10, 1), date(2026, 10, 31), me="davi")
    assert n == 5
    evs = store.events(conn, 0, 2**62, kinds=["calendar"])
    assert all(e.title == "Daily" and e.attrs["n_attendees"] == 2 and e.attrs["organizer_is_me"] for e in evs)
    first = datetime.fromtimestamp(evs[0].start_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo"))
    assert (first.hour, first.minute) == (9, 30)
    assert evs[0].end_ms - evs[0].start_ms == 15 * 60_000


def test_git_import(tmp_path: Path):
    from pulso.importers import gitlog

    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    run("init", "-q")
    run("config", "user.name", "Gabi Dev")
    run("config", "user.email", "g@example.com")
    (repo / "a.txt").write_text("1\n2\n")
    run("add", ".")
    run("commit", "-qm", "feat: primeira")
    conn = store.connect(":memory:")
    assert gitlog.import_repo(conn, repo, author="Gabi", since="10 years ago") == 1
    ev = store.events(conn, 0, 2**62, kinds=["commit"])[0]
    assert ev.title == "feat: primeira" and ev.attrs["insertions"] == 2
