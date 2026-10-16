"""Read-only dashboard served by the central at /painel (self-contained HTML, no CDN: health data
pages should not pull third-party code). Auth: a painel-scoped bearer token (`pulso token create
<label> --painel`), kept in the browser's localStorage and sent as a header by the page's fetch
calls. The watch's device token is refused here: it lives on the watch and in build artefacts."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from importlib import resources
from zoneinfo import ZoneInfo

import numpy as np

from pulso import analysis, jobs, store
from pulso import metrics as M

MIN = 60_000


def _series_minutes(conn: sqlite3.Connection, metric: str, a: int, b: int, source: str | None = None,
                    pref: tuple[str, ...] | None = None) -> list[float | None]:
    if pref is not None:
        v = analysis.best_minute_series(conn, metric, a, b, preference=pref)
    else:
        ts, vals = store.series(conn, metric, a, b, source=source)
        v = np.full((b - a) // MIN, np.nan)
        if ts.size:
            idx = ((ts - a) // MIN).astype(int)
            ok = (idx >= 0) & (idx < v.size)
            v[idx[ok]] = vals[ok]
    return [None if not np.isfinite(x) else round(float(x), 2) for x in v]


def day_payload(conn: sqlite3.Connection, d: date, tz: str) -> dict:
    a, b = analysis.local_day_bounds(d, tz)
    summary = store.get_artefact(conn, "analysis:day", d.isoformat()) or {}
    evs = store.events(conn, a, b, kinds=["meeting", "calendar", "commit", "note", "workout", "session"])
    z = ZoneInfo(tz)

    def hm(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(z).strftime("%H:%M")

    return {
        "day": d.isoformat(),
        "t0_ms": a,
        "n": (b - a) // MIN,
        "arousal": _series_minutes(conn, M.AROUSAL_Z, a, b, source=M.DERIVED),
        "arousal_sd": _series_minutes(conn, M.AROUSAL_SD, a, b, source=M.DERIVED),
        "additional_hr": _series_minutes(conn, M.ADDITIONAL_HR, a, b, source=M.DERIVED),
        "hr": _series_minutes(conn, M.HR, a, b, pref=M.HR_PREFERENCE),
        "stress": _series_minutes(conn, M.STRESS_GARMIN, a, b, pref=(M.SRC_FIT, M.SRC_GC, M.SRC_CIQ_FACE)),
        "steps": _series_minutes(conn, M.STEPS_PM, a, b, pref=(M.SRC_FIT, M.SRC_GC, M.SRC_CIQ_FACE)),
        "rmssd": _series_minutes(conn, M.RMSSD, a, b, source=M.DERIVED),
        "episodes": [{"start": (e["start_ms"] - a) // MIN, "end": (e["end_ms"] - a) // MIN, "peak_z": e["peak_z"],
                      "kind": e["kind"], "label": f"{hm(e['start_ms'])}–{hm(e['end_ms'])}"} for e in summary.get("episodes", [])],
        "events": [{"start": max(0, (e.start_ms - a) // MIN), "end": min((b - a) // MIN, (e.end_ms - a) // MIN),
                    "kind": e.kind, "title": (e.title or "")[:60], "at": hm(e.start_ms)} for e in evs],
        "night": store.get_artefact(conn, "sleep:night", d.isoformat()),
        "notes": summary.get("notes", []),
    }


def table_payload(conn: sqlite3.Connection, days: int, tz: str) -> dict:
    end = datetime.now(ZoneInfo(tz)).date()
    return {"rows": jobs.daily_table(conn, end - timedelta(days=days - 1), end, tz)}


def html() -> str:
    return resources.files("pulso").joinpath("painel.html").read_text(encoding="utf-8")
