"""Meeting transcripts -> events + time-aligned segments.

Accepted formats (auto-detected):
* WebVTT (Zoom, Teams, Google Meet): cues with optional `<v Speaker>` voice tags or
  "Speaker: text" prefixes;
* SRT;
* Whisper / faster-whisper / WhisperX JSON: {"segments": [{"start", "end", "text", "speaker"?}]}
  (also a bare list of segments);
* plain text lines: "[HH:MM:SS] Speaker: text" or "HH:MM:SS Speaker: text".

Transcripts carry *relative* times. The absolute start comes from, in order: the explicit
`start` argument, a calendar/meeting event overlapping the file's timestamp hint, or the file's
modification time minus its duration (flagged as `start_source='mtime'` so the analysis knows the
alignment is approximate).
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pulso import store
from pulso.store import Event

_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?"
_CUE = re.compile(rf"^\s*{_TS}\s*-->\s*{_TS}")
_VOICE = re.compile(r"^<v(?:\.[^ >]+)?\s+([^>]+)>(.*?)(?:</v>)?$")
_PREFIX = re.compile(r"^([A-ZÀ-ÚA-Za-zà-ú0-9][\w .'\-À-ú]{0,40}?):\s+(.+)$")
_LINE = re.compile(rf"^\s*\[?{_TS}\]?\s*(?:-\s*)?(.*)$")


@dataclass
class Segment:
    start_s: float
    end_s: float
    text: str
    speaker: str | None = None


@dataclass
class Transcript:
    segments: list[Segment] = field(default_factory=list)
    fmt: str = ""

    @property
    def duration_s(self) -> float:
        return max((s.end_s for s in self.segments), default=0.0)


def _secs(h, m, s, frac) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + (int(frac.ljust(3, "0")) / 1000 if frac else 0.0)


def _split_speaker(text: str) -> tuple[str | None, str]:
    text = text.strip()
    v = _VOICE.match(text)
    if v:
        return v.group(1).strip(), re.sub(r"</?[^>]+>", "", v.group(2)).strip()
    text = re.sub(r"</?[^>]+>", "", text).strip()
    p = _PREFIX.match(text)
    if p and len(p.group(1).split()) <= 4:
        return p.group(1).strip(), p.group(2).strip()
    return None, text


def parse_cues(raw: str, fmt: str) -> Transcript:
    out = Transcript(fmt=fmt)
    lines = raw.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        m = _CUE.match(lines[i])
        if not m:
            i += 1
            continue
        g = m.groups()
        a, b = _secs(*g[0:4]), _secs(*g[4:8])
        i += 1
        body = []
        while i < len(lines) and lines[i].strip() and not _CUE.match(lines[i]):
            body.append(lines[i])
            i += 1
        if body and fmt == "srt" and body[-1].strip().isdigit():
            body = body[:-1]
        spk, text = _split_speaker(" ".join(body))
        if text:
            out.segments.append(Segment(a, b, text, spk))
    return out


def parse_json(raw: str) -> Transcript:
    data = json.loads(raw)
    segs = data.get("segments", data) if isinstance(data, dict) else data
    out = Transcript(fmt="json")
    for s in segs:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        spk = s.get("speaker")
        if spk is None:
            spk, text = _split_speaker(text)
        out.segments.append(Segment(float(s["start"]), float(s["end"]), text, spk))
    return out


def parse_plain(raw: str) -> Transcript:
    out = Transcript(fmt="text")
    pending: list[tuple[float, str | None, str]] = []
    for line in raw.splitlines():
        m = _LINE.match(line)
        if not m or not m.group(5).strip():
            continue
        g = m.groups()
        t = _secs(g[0], g[1], g[2], g[3])
        spk, text = _split_speaker(g[4])
        pending.append((t, spk, text))
    for k, (t, spk, text) in enumerate(pending):
        end = pending[k + 1][0] if k + 1 < len(pending) else t + max(5.0, len(text.split()) / 2.5)
        out.segments.append(Segment(t, max(end, t + 1.0), text, spk))
    return out


def parse(raw: str, name: str = "") -> Transcript:
    head = raw.lstrip()[:200]
    if head.startswith("WEBVTT") or name.endswith(".vtt"):
        return parse_cues(raw, "vtt")
    if name.endswith(".srt") or re.match(r"^\d+\s*\n\s*\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}\s*-->", head):
        return parse_cues(raw, "srt")
    if head.startswith("{") or head.startswith("["):
        try:
            return parse_json(raw)
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass  # "[00:00:05] Ana: ..." also starts with a bracket
    return parse_plain(raw)


def import_file(conn: sqlite3.Connection, path: str | Path, start: datetime | None = None,
                title: str | None = None, attendees: list[str] | None = None) -> str:
    """Parse, anchor in absolute time and store. Returns the meeting event id."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8", errors="replace")
    tr = parse(raw, p.name.lower())
    if not tr.segments:
        raise ValueError(f"{p}: no transcript segments recognised")

    start_source = "explicit"
    if start is not None:
        start_ms = int(start.timestamp() * 1000)
    else:
        guess_end = int(p.stat().st_mtime * 1000)
        guess_start = guess_end - int(tr.duration_s * 1000)
        cal = [e for e in store.events(conn, guess_start - 3 * 3600_000, guess_end + 3600_000, kinds=["calendar", "meeting"])
               if abs(e.start_ms - guess_start) < 3 * 3600_000]
        if title:
            cal = [e for e in cal if (e.title or "").lower() == title.lower()] or cal
        if cal:
            ev = min(cal, key=lambda e: abs(e.start_ms - guess_start))
            start_ms, start_source = ev.start_ms, f"calendar:{ev.id}"
            title = title or ev.title
            attendees = attendees or ev.attrs.get("attendees")
        else:
            start_ms, start_source = guess_start, "mtime"

    digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
    eid = f"meeting:{digest}"
    speakers = sorted({s.speaker for s in tr.segments if s.speaker})
    with store.transaction(conn):
        store.upsert_event(conn, Event(
            id=eid, kind="meeting", start_ms=start_ms, end_ms=start_ms + int(tr.duration_s * 1000),
            title=title or p.stem, source=f"transcript:{tr.fmt}",
            attrs={"file": p.name, "start_source": start_source, "speakers": speakers,
                   "attendees": attendees or speakers, "n_segments": len(tr.segments)},
        ))
        conn.execute("DELETE FROM transcript_segments WHERE event_id = ?", (eid,))
        conn.executemany(
            "INSERT INTO transcript_segments (event_id, idx, start_ms, end_ms, speaker, text, tags) VALUES (?, ?, ?, ?, ?, ?, NULL)",
            [(eid, i, start_ms + int(s.start_s * 1000), start_ms + int(s.end_s * 1000), s.speaker, s.text)
             for i, s in enumerate(tr.segments)],
        )
    return eid


def segments_for(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT event_id, idx, start_ms, end_ms, speaker, text, tags FROM transcript_segments WHERE event_id = ? ORDER BY idx",
        (event_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
        out.append(d)
    return out
