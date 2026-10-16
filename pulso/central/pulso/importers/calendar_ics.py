"""Calendar (.ics export from Google Calendar / Outlook) -> 'calendar' events.

Recurring events are expanded within the requested window, minus EXDATEs and minus the instances
that a RECURRENCE-ID component moved or cancelled (the moved instance is imported from that
component). Re-importing removes what the file says is gone:

  - events the file carries (same UID) that it no longer has at that time: moved, cancelled or
    deleted instances, a rescheduled or cancelled invitation;
  - for a whole-calendar export (X-WR-CALNAME, METHOD:PUBLISH or none), that calendar's events the
    file no longer has, but only over the days its single events cover (and never outside the
    range Outlook declares in X-CLIPSTART/X-CLIPEND), so a date-range export does not erase the rest
    of the window. A recurring series says nothing about the range exported, and neither do its
    changed instances, which travel with it (they count only when the file has no single event);
    an invitation (METHOD:REQUEST/CANCEL) or an unnamed file speaks only about its own events.

A calendar is told apart by Outlook's X-WR-RELCALID, else by its name plus the file name: the
default calendar is "Calendário" in every Outlook account and two accounts must not erase each
other's meetings.

Attendee count and whether you organise the meeting are kept: both are candidate stress features.
"""

from __future__ import annotations

import json
import re
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


def _uid_of(event_id: str) -> str:
    return event_id[len("cal:"):event_id.rfind(":")]  # ids are cal:{uid}:{start_ms}


def _day_bounds(ms: int, zone: ZoneInfo) -> tuple[int, int]:
    d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(zone).date()
    return _to_ms(d, zone), _to_ms(d + timedelta(days=1), zone)


def _declared(cal, prop: str, zone: ZoneInfo, default: int) -> int:
    """Outlook's X-CLIPSTART/X-CLIPEND: the date range picked in 'Save Calendar' (MS-OXCICAL
    2.1.3.1.1.6-7; X-CALSTART/X-CALEND are only the first/last instance in the file), in ms."""
    from icalendar.prop import vDDDTypes

    try:
        return _to_ms(vDDDTypes.from_ical(str(cal[prop])), zone) if prop in cal else default
    except (ValueError, TypeError):
        return default


def _calendar_id(cal, name: str, path: str | Path) -> str:
    """Which calendar this export is, for the sweep of what it no longer has."""
    if str(cal.get("X-WR-RELCALID", "")).strip():
        return str(cal["X-WR-RELCALID"]).strip()
    # not folding a browser's "calendar (1).ics" into "calendar": Outlook web's published links all download
    # as calendar.ics and Exchange writes no X-WR-RELCALID, so that may well be another account
    stem = Path(path).stem.casefold()
    return f"{name}|{stem}" if name else stem


def _address(v) -> str:
    return re.sub(r"(?i)^mailto:", "", str(v))  # Outlook/Exchange write MAILTO:


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
    lo_ms, hi_ms = _to_ms(lo, zone), _to_ms(hi, zone)
    name = str(cal.get("X-WR-CALNAME", ""))
    calendar = name or Path(path).stem
    calendar_id = _calendar_id(cal, name, path)
    whole = bool(name) and str(cal.get("METHOD", "PUBLISH")).upper() == "PUBLISH"
    comps = list(cal.walk("VEVENT"))
    # UIDs the file defines whole (a single event or a series' master), as opposed to UIDs it only
    # carries single changed instances of (an invitation for one moved occurrence)
    masters = {str(c.get("UID", "")) for c in comps if c.get("RECURRENCE-ID") is None} - {""}
    # instances replaced by a RECURRENCE-ID component (moved or cancelled): not expanded from the master
    overridden: dict[str, set[int]] = {}
    dated: list[int] = []  # when the single items happen (one-offs, lone overrides): the days the file covers
    exceptions: list[int] = []  # changed instances of a series the file carries: written with the series, any date
    for comp in comps:
        cuid = str(comp.get("UID", ""))
        if comp.get("RECURRENCE-ID") is not None:
            rid = _to_ms(comp.decoded("RECURRENCE-ID"), zone)
            overridden.setdefault(cuid, set()).add(rid)
            into = exceptions if cuid in masters else dated
            into.append(rid)
            if comp.get("DTSTART") is not None:
                into.append(_to_ms(comp.decoded("DTSTART"), zone))
        elif comp.get("DTSTART") is not None and not comp.get("RRULE"):
            dated.append(_to_ms(comp.decoded("DTSTART"), zone))
    written: set[str] = set()
    n = 0
    with store.transaction(conn):
        for comp in comps:
            if str(comp.get("STATUS", "")).upper() == "CANCELLED" or str(comp.get("TRANSP", "")).upper() == "TRANSPARENT":
                continue
            dtstart = comp.decoded("DTSTART")
            if not isinstance(dtstart, datetime):
                continue  # all-day items are not meetings
            dtend = comp.decoded("DTEND") if comp.get("DTEND") else dtstart + comp.decoded("DURATION", timedelta(minutes=30))
            dur = dtend - dtstart
            uid = str(comp.get("UID", "")) or f"{comp.get('SUMMARY')}-{dtstart}"
            rid = _to_ms(comp.decoded("RECURRENCE-ID"), zone) if comp.get("RECURRENCE-ID") is not None else None
            occurrences = [dtstart]
            if comp.get("RRULE") and comp.get("RECURRENCE-ID") is None:
                from dateutil.rrule import rrulestr

                rule = rrulestr(comp.get("RRULE").to_ical().decode(), dtstart=dtstart)
                occurrences = rule.between(lo.astimezone(dtstart.tzinfo or zone), hi.astimezone(dtstart.tzinfo or zone), inc=True)
                exdates = set(overridden.get(uid, ()))
                for ex in comp.get("EXDATE", []) if isinstance(comp.get("EXDATE"), list) else [comp.get("EXDATE")] if comp.get("EXDATE") else []:
                    for d in ex.dts:
                        exdates.add(_to_ms(d.dt, zone))
                occurrences = [o for o in occurrences if _to_ms(o, zone) not in exdates]
            attendees = comp.get("ATTENDEE", [])
            if not isinstance(attendees, list):
                attendees = [attendees]
            names = [str(a.params.get("CN", _address(a))) for a in attendees]
            organizer = _address(comp.get("ORGANIZER", ""))
            for occ in occurrences:
                s = _to_ms(occ, zone)
                if not (lo_ms <= s < hi_ms):
                    continue
                eid = f"cal:{uid}:{s}"
                store.upsert_event(conn, Event(
                    id=eid, kind="calendar", start_ms=s, end_ms=s + int(dur.total_seconds() * 1000),
                    title=str(comp.get("SUMMARY", "")) or None, source="ics",
                    attrs={"attendees": names, "n_attendees": len(names),
                           "organizer_is_me": bool(me and me.lower() in organizer.lower()),
                           "recurring": bool(comp.get("RRULE") or comp.get("RECURRENCE-ID")),
                           "calendar": calendar, "calendar_id": calendar_id, "recurrence_id": rid},
                ))
                written.add(eid)
                n += 1
        # what the file says is gone (see the module docstring): never more than the file speaks for
        dated = [t for t in dated if lo_ms <= t < hi_ms] or [t for t in exceptions if lo_ms <= t < hi_ms]
        covered = (_day_bounds(min(dated), zone)[0], _day_bounds(max(dated), zone)[1]) if dated else (0, 0)
        covered = (max(covered[0], _declared(cal, "X-CLIPSTART", zone, covered[0])),
                   min(covered[1], _declared(cal, "X-CLIPEND", zone, covered[1])))
        stale = []
        for r in conn.execute("SELECT id, start_ms, attrs FROM events WHERE source = 'ics' AND kind = 'calendar'"
                              " AND start_ms >= ? AND start_ms < ?", (lo_ms, hi_ms)):
            if r["id"] in written:
                continue
            uid, attrs = _uid_of(r["id"]), json.loads(r["attrs"] or "{}")
            if uid in masters:
                gone = True
            elif uid in overridden:  # only the instances the file changes: their slot or an earlier move
                gone = r["start_ms"] in overridden[uid] or attrs.get("recurrence_id") in overridden[uid]
            else:  # rows written before calendar_id existed know only the calendar's name
                same = attrs.get("calendar_id", calendar_id if attrs.get("calendar") == calendar else None)
                gone = whole and same == calendar_id and covered[0] <= r["start_ms"] < covered[1]
            if gone:
                stale.append(r["id"])
        for eid in stale:
            conn.execute("DELETE FROM events WHERE id = ?", (eid,))
            conn.execute("DELETE FROM artefacts WHERE kind = 'meeting_response' AND key = ?", (eid,))
    return n


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
