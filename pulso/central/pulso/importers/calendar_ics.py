"""Calendar (.ics export from Google Calendar / Outlook) -> 'calendar' events.

Recurring events are expanded within the requested window. Attendee count and whether you
organise the meeting are kept: both are candidate stress features.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulso import store
from pulso.store import Event


def _to_ms(v, tz: ZoneInfo) -> int:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=tz)
        return int(v.timestamp() * 1000)
    if isinstance(v, date):
        return int(datetime.combine(v, time(0, 0), tzinfo=tz).timestamp() * 1000)
    raise TypeError(type(v))


def import_ics(conn: sqlite3.Connection, path: str | Path, tz: str, start: date, end: date,
               me: str | None = None) -> int:
    try:
        from icalendar import Calendar
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[calendar]' to import .ics files") from e
    zone = ZoneInfo(tz)
    cal = Calendar.from_ical(Path(path).read_bytes())
    lo = datetime.combine(start, time(0, 0), tzinfo=zone)
    hi = datetime.combine(end, time(0, 0), tzinfo=zone)
    n = 0
    with store.transaction(conn):
        for comp in cal.walk("VEVENT"):
            if str(comp.get("STATUS", "")).upper() == "CANCELLED" or str(comp.get("TRANSP", "")).upper() == "TRANSPARENT":
                continue
            dtstart = comp.decoded("DTSTART")
            if not isinstance(dtstart, datetime):
                continue  # all-day items are not meetings
            dtend = comp.decoded("DTEND") if comp.get("DTEND") else dtstart + comp.decoded("DURATION", timedelta(minutes=30))
            dur = dtend - dtstart
            occurrences = [dtstart]
            if comp.get("RRULE"):
                from dateutil.rrule import rrulestr

                rule = rrulestr(comp.get("RRULE").to_ical().decode(), dtstart=dtstart)
                occurrences = rule.between(lo.astimezone(dtstart.tzinfo or zone), hi.astimezone(dtstart.tzinfo or zone), inc=True)
                exdates = set()
                for ex in comp.get("EXDATE", []) if isinstance(comp.get("EXDATE"), list) else [comp.get("EXDATE")] if comp.get("EXDATE") else []:
                    for d in ex.dts:
                        exdates.add(_to_ms(d.dt, zone))
                occurrences = [o for o in occurrences if _to_ms(o, zone) not in exdates]
            attendees = comp.get("ATTENDEE", [])
            if not isinstance(attendees, list):
                attendees = [attendees]
            names = [str(a.params.get("CN", str(a).replace("mailto:", ""))) for a in attendees]
            organizer = str(comp.get("ORGANIZER", "")).replace("mailto:", "")
            uid = str(comp.get("UID", "")) or f"{comp.get('SUMMARY')}-{dtstart}"
            for occ in occurrences:
                s = _to_ms(occ, zone)
                if not (_to_ms(lo, zone) <= s < _to_ms(hi, zone)):
                    continue
                store.upsert_event(conn, Event(
                    id=f"cal:{uid}:{s}", kind="calendar", start_ms=s, end_ms=s + int(dur.total_seconds() * 1000),
                    title=str(comp.get("SUMMARY", "")) or None, source="ics",
                    attrs={"attendees": names, "n_attendees": len(names),
                           "organizer_is_me": bool(me and me.lower() in organizer.lower()),
                           "recurring": bool(comp.get("RRULE"))},
                ))
                n += 1
    return n


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
