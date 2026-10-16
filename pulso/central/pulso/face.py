"""What the watch face shows, computed on the central and shipped back in the ingest response.

Budget: the face's background service gets ~8 KB to hand to the foreground and the BLE link is
slow, so this stays well under 1 KB. Strings are pre-formatted here (the watch has no locale
tables to spare) and numbers pre-rounded.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso import metrics as M
from pulso import store

MIN = 60_000


def _hhmm(ms: int, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(tz).strftime("%H:%M")


def _short(s: str | None, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def predicted_meeting_load(conn: sqlite3.Connection, title: str | None, attendees: list[str] | None) -> int | None:
    """0..3 from past responses to meetings sharing title words or attendees (stored by the
    meeting analysis as artefacts 'meeting_response'). None when there is no history."""
    rows = conn.execute("SELECT key, body FROM artefacts WHERE kind = 'meeting_response'").fetchall()
    if not rows:
        return None
    import json

    words = {w.lower() for w in (title or "").split() if len(w) > 3}
    people = {a.lower() for a in (attendees or [])}
    vals = []
    for r in rows:
        b = json.loads(r["body"])
        bw = {w.lower() for w in (b.get("title") or "").split() if len(w) > 3}
        bp = {a.lower() for a in (b.get("attendees") or [])}
        if (words & bw) or (people & bp):
            v = b.get("during")
            if v is not None:
                vals.append(v)
    if len(vals) < 2:
        return None
    m = float(np.median(vals))
    return 0 if m < 0.25 else 1 if m < 0.6 else 2 if m < 1.0 else 3


def face_state(conn: sqlite3.Connection, now_ms: int, tz: str) -> dict:
    zone = ZoneInfo(tz)
    out: dict = {"t": now_ms // 1000}

    ts, z = store.series(conn, M.AROUSAL_Z, now_ms - 30 * MIN, now_ms + 1, source=M.DERIVED)
    if z.size:
        out["az"] = round(float(np.nanmean(z[-10:])), 1)
    ts_a, add = store.series(conn, M.ADDITIONAL_HR, now_ms - 15 * MIN, now_ms + 1, source=M.DERIVED)
    if add.size:
        out["ahr"] = int(round(float(np.nanmean(add[-5:]))))

    # hourly arousal of the last 12 h as small ints (-9..9) for the face sparkline
    ts12, z12 = store.series(conn, M.AROUSAL_Z, now_ms - 12 * 60 * MIN, now_ms + 1, source=M.DERIVED)
    if z12.size:
        idx = ((ts12 - (now_ms - 12 * 60 * MIN)) // (60 * MIN)).astype(int)
        trend = []
        for h in range(12):
            v = z12[idx == h]
            trend.append(None if v.size == 0 else int(np.clip(round(float(np.nanmean(v)) * 3), -9, 9)))
        out["tr"] = trend

    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(zone).date().isoformat()
    day = store.get_artefact(conn, "analysis:day", today)
    if day:
        eps = day.get("episodes") or []
        out["ep"] = len(eps)
        if eps:
            last = max(eps, key=lambda e: e["peak_ms"])
            out["pk"] = _hhmm(last["peak_ms"], zone)
            if last.get("overlaps"):
                ev = conn.execute("SELECT title FROM events WHERE id = ?", (last["overlaps"][0],)).fetchone()
                if ev and ev["title"]:
                    out["pl"] = _short(ev["title"], 18)

    night = store.get_artefact(conn, "sleep:last", "latest")
    if night:
        if night.get("total_sleep_min") is not None:
            out["slp"] = int(night["total_sleep_min"])
        if night.get("debt_min") is not None:
            out["sd"] = int(night["debt_min"])
        if night.get("hrv_ms") is not None:
            out["hrv"] = int(round(night["hrv_ms"]))
        if night.get("hrv_status"):
            out["hrvs"] = night["hrv_status"]

    upcoming = [e for e in store.events(conn, now_ms, now_ms + 12 * 60 * MIN, kinds=["calendar", "meeting"])
                if e.start_ms >= now_ms]
    if upcoming:
        nx = upcoming[0]
        nm = {"t": _hhmm(nx.start_ms, zone), "ti": _short(nx.title, 18)}
        risk = predicted_meeting_load(conn, nx.title, nx.attrs.get("attendees"))
        if risk is not None:
            nm["r"] = risk
        out["nm"] = nm

    # newest alert of the last 30 min: the watch shows it once as a native notification (by id)
    row = conn.execute("SELECT key, body FROM artefacts WHERE kind = 'alert' AND created_ms >= ? ORDER BY created_ms DESC LIMIT 1",
                       (now_ms - 30 * MIN,)).fetchone()
    if row:
        import json as _json

        al = _json.loads(row["body"])
        import zlib

        # stable short id (Python hash() is salted per process)
        out["al"] = {"i": zlib.crc32(al["id"].encode()) & 0x7FFFFFFF, "m": _short(al["text"], 60)}  # fits a Monkey C Number

    insight = store.get_artefact(conn, "insight:face", today)
    if insight and insight.get("text"):
        out["msg"] = _short(insight["text"], 48)
    return {k: v for k, v in out.items() if v is not None and not (isinstance(v, float) and math.isnan(v))}
