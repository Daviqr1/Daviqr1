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


WEEKLY = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google Inc//Google Calendar 70.9054//EN
X-WR-CALNAME:Trabalho
BEGIN:VEVENT
DTSTART;TZID=America/Sao_Paulo:20261005T100000
DTEND;TZID=America/Sao_Paulo:20261005T110000
RRULE:FREQ=WEEKLY;BYDAY=MO
UID:weekly123@google.com
SUMMARY:Sync semanal
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/Sao_Paulo:20261012T150000
DTEND;TZID=America/Sao_Paulo:20261012T160000
RECURRENCE-ID;TZID=America/Sao_Paulo:20261012T100000
UID:weekly123@google.com
SUMMARY:Sync semanal
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/Sao_Paulo:20261019T100000
DTEND;TZID=America/Sao_Paulo:20261019T110000
RECURRENCE-ID;TZID=America/Sao_Paulo:20261019T100000
STATUS:CANCELLED
UID:weekly123@google.com
SUMMARY:Sync semanal
END:VEVENT
BEGIN:VEVENT
DTSTART;TZID=America/Sao_Paulo:20261014T140000
DTEND;TZID=America/Sao_Paulo:20261014T143000
UID:once@google.com
SUMMARY:Entrevista
END:VEVENT
END:VCALENDAR
"""


def test_ics_moved_cancelled_and_deleted_instances(tmp_path: Path):
    """F10: a RECURRENCE-ID override replaces its slot (moved or cancelled), and re-importing the
    calendar drops what it no longer has, without touching another calendar's events."""
    pytest.importorskip("icalendar")
    from pulso.importers import calendar_ics

    tz = ZoneInfo("America/Sao_Paulo")

    def slots():
        return sorted(datetime.fromtimestamp(e.start_ms / 1000, tz=tz).strftime("%m-%d %H:%M")
                      for e in store.events(conn, 0, 2**62, kinds=["calendar"]))

    conn = store.connect(":memory:")
    other = tmp_path / "pessoal.ics"
    other.write_text(ICS)
    calendar_ics.import_ics(conn, other, "America/Sao_Paulo", date(2026, 10, 1), date(2026, 10, 31))
    personal = slots()
    f = tmp_path / "trabalho.ics"
    f.write_text(WEEKLY)
    calendar_ics.import_ics(conn, f, "America/Sao_Paulo", date(2026, 10, 1), date(2026, 10, 31))
    work = sorted(set(slots()) - set(personal))
    assert work == ["10-05 10:00", "10-12 15:00", "10-14 14:00", "10-26 10:00"]
    once = [e for e in store.events(conn, 0, 2**62, kinds=["calendar"]) if e.title == "Entrevista"][0]
    store.put_artefact(conn, "meeting_response", once.id, {"title": "Entrevista", "during": 1.0})
    # the interview is deleted from the calendar and the export downloaded again
    f.write_text(WEEKLY.replace(WEEKLY[WEEKLY.index("BEGIN:VEVENT\nDTSTART;TZID=America/Sao_Paulo:20261014"):
                                      WEEKLY.index("END:VCALENDAR")], ""))
    calendar_ics.import_ics(conn, f, "America/Sao_Paulo", date(2026, 10, 1), date(2026, 10, 31))
    assert sorted(set(slots()) - set(personal)) == ["10-05 10:00", "10-12 15:00", "10-26 10:00"]
    assert set(personal) <= set(slots())  # the other calendar is untouched
    assert store.get_artefact(conn, "meeting_response", once.id) is None


INVITE = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
METHOD:{method}
BEGIN:VEVENT
DTSTART:{s}
DTEND:{e}
UID:{uid}
SUMMARY:{t}
STATUS:{status}
END:VEVENT
END:VCALENDAR
"""


def _invite(folder: Path, s: str, uid: str, title: str, method="REQUEST", status="CONFIRMED") -> Path:
    folder.mkdir(exist_ok=True)
    f = folder / "invite.ics"  # Google names every invitation attachment invite.ics
    e = s[:9] + f"{int(s[9:11]) + 1:02d}" + s[11:]
    f.write_text(INVITE.format(method=method, s=s, e=e, uid=uid, t=title, status=status))
    return f


def test_ics_invitations_speak_only_for_their_own_events(tmp_path: Path):
    """verify N1(a): invitations (METHOD:REQUEST, no calendar name) do not erase each other; an
    updated invitation moves its event and a cancellation removes it."""
    pytest.importorskip("icalendar")
    from pulso.importers import calendar_ics

    conn = store.connect(":memory:")
    window = ("America/Sao_Paulo", date(2026, 9, 1), date(2026, 11, 1))

    def titles():
        return [(e.title, datetime.fromtimestamp(e.start_ms / 1000, tz=timezone.utc).strftime("%d %H"))
                for e in store.events(conn, 0, 2**62, kinds=["calendar"])]

    calendar_ics.import_ics(conn, _invite(tmp_path / "a", "20261012T130000Z", "one@google.com", "Entrevista"), *window)
    calendar_ics.import_ics(conn, _invite(tmp_path / "b", "20261013T130000Z", "two@google.com", "Review"), *window)
    assert titles() == [("Entrevista", "12 13"), ("Review", "13 13")]
    # the interview is rescheduled: the new invitation replaces the old slot
    calendar_ics.import_ics(conn, _invite(tmp_path / "c", "20261015T170000Z", "one@google.com", "Entrevista"), *window)
    assert titles() == [("Review", "13 13"), ("Entrevista", "15 17")]
    # and then cancelled
    calendar_ics.import_ics(conn, _invite(tmp_path / "d", "20261015T170000Z", "one@google.com", "Entrevista",
                                          method="CANCEL", status="CANCELLED"), *window)
    assert titles() == [("Review", "13 13")]


def _export(days, weekly: bool = False) -> str:
    body = "".join("BEGIN:VEVENT\nDTSTART:202610%02dT130000Z\nDTEND:202610%02dT140000Z\nUID:u%d@x\nSUMMARY:M%d\n"
                   "END:VEVENT\n" % (k, k, k, k) for k in days)
    if weekly:  # a series exported whole (its RRULE) even when the export covers a few days
        body += ("BEGIN:VEVENT\nDTSTART:20260907T110000Z\nDTEND:20260907T113000Z\nRRULE:FREQ=WEEKLY;BYDAY=MO\n"
                 "UID:weekly@x\nSUMMARY:1:1\nEND:VEVENT\n")
    return "BEGIN:VCALENDAR\nVERSION:2.0\nMETHOD:PUBLISH\nX-WR-CALNAME:Calendar\n" + body + "END:VCALENDAR\n"


@pytest.mark.parametrize("weekly", [False, True])
def test_ics_date_range_export_keeps_the_rest_of_the_window(tmp_path: Path, weekly: bool):
    """verify N1(b): an export of the same calendar for 7 days (Outlook 'Save Calendar' with a date
    range) removes only what is gone from those days, not every other event in the 90-day window."""
    pytest.importorskip("icalendar")
    from pulso.importers import calendar_ics

    conn = store.connect(":memory:")
    window = ("America/Sao_Paulo", date(2026, 9, 1), date(2026, 11, 1))
    f = tmp_path / "agenda.ics"
    f.write_text(_export(range(1, 31), weekly))
    calendar_ics.import_ics(conn, f, *window)
    full = {e.title for e in store.events(conn, 0, 2**62, kinds=["calendar"])}
    first = [e for e in store.events(conn, 0, 2**62, kinds=["calendar"]) if e.title == "M3"][0]
    store.put_artefact(conn, "meeting_response", first.id, {"title": "M3", "during": 1.0})
    f.write_text(_export([k for k in range(16, 23) if k != 20], weekly))  # M20 was deleted meanwhile
    calendar_ics.import_ics(conn, f, *window)
    left = [e.title for e in store.events(conn, 0, 2**62, kinds=["calendar"])]
    assert set(left) == full - {"M20"}
    assert len(left) == 29 + (8 if weekly else 0)  # 8 Mondays from 09-07 to 10-26
    assert store.get_artefact(conn, "meeting_response", first.id)


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
