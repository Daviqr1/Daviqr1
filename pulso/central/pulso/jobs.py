"""Batch jobs that turn derived series into answers: meetings, sleep, readiness.

Each job reads the store, runs the models and writes artefacts — never raw data — so they can be
re-run at will (`pulso analyze --days 30`) and the MCP server / reports / face just read them.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso import analysis, store
from pulso import metrics as M
from pulso.importers.transcripts import segments_for
from pulso.models import baseline, meetings, sleep

MIN = 60_000
DAY = 1440 * MIN


def _day_of(ms: int, tz: str) -> date:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).date()


def arousal_range(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> tuple[np.ndarray, np.ndarray]:
    return store.series(conn, M.AROUSAL_Z, start_ms, end_ms, source=M.DERIVED)


# ------------------------------------------------------------------------------- meetings


def meeting_job(conn: sqlite3.Connection, start: date, end: date, tz: str) -> dict:
    """Per-meeting responses, the matched-control test, and in-meeting segment effects."""
    a, _ = analysis.local_day_bounds(start, tz)
    _, b = analysis.local_day_bounds(end, tz)
    ts, z = arousal_range(conn, a - 42 * DAY, b + DAY)  # controls look ±6 weeks around
    evs = store.events(conn, a, b, kinds=["meeting", "calendar"])
    # a transcript and its calendar entry describe the same meeting: keep the transcript one
    meets = [e for e in evs if e.kind == "meeting"]
    covered = {e.attrs.get("start_source", "").removeprefix("calendar:") for e in meets}
    meets += [e for e in evs if e.kind == "calendar" and e.id not in covered
              and not any(abs(m.start_ms - e.start_ms) < 10 * MIN for m in meets if m.kind == "meeting")]
    busy = store.events(conn, a - 42 * DAY, b + 42 * DAY, kinds=["meeting", "calendar"])

    responses = []
    ordered = sorted(meets, key=lambda e: e.start_ms)
    with store.transaction(conn):
        for k, ev in enumerate(ordered):
            r = meetings.response(ts, z, ev.id, ev.start_ms, ev.end_ms)
            if r.coverage < 0.5:
                continue
            # gap since the previous meeting ended the same day (back-to-back = < 10 min)
            prev = [m for m in ordered[:k] if _day_of(m.start_ms, tz) == _day_of(ev.start_ms, tz)]
            gap = (ev.start_ms - max(m.end_ms for m in prev)) / MIN if prev else None
            d = r.as_dict() | {"title": ev.title, "attendees": ev.attrs.get("attendees") or [],
                               "start_ms": ev.start_ms, "end_ms": ev.end_ms,
                               "day": _day_of(ev.start_ms, tz).isoformat(),
                               "gap_before_min": None if gap is None else round(gap, 1)}
            store.put_artefact(conn, "meeting_response", ev.id, d)
            responses.append(d)
    cmp = meetings.matched_controls(ts, z, [e for e in meets], busy, tz)

    segs = []
    for ev in meets:
        if ev.kind == "meeting":
            segs += segments_for(conn, ev.id)
    seg_eff: dict = {}
    lag = 20
    if segs:
        lag = meetings.estimate_lag(ts, z, [s for s in segs if (s.get("speaker") or "").lower() in {"eu", "me", "davi", "gabi"}] or segs)
        seg_eff = meetings.segment_effects(ts, z, segs, lag_s=lag)

    # back-to-back vs with a break before (Microsoft 2021 EEG pilot: stress builds without breaks).
    # Uses the absolute level, not the baseline-corrected response: for a back-to-back meeting the
    # "baseline" window is the previous meeting, which would hide exactly the effect we look for.
    b2b = [r["level"] for r in responses if r.get("gap_before_min") is not None and r["gap_before_min"] < 10 and r.get("level") is not None]
    brk = [r["level"] for r in responses if (r.get("gap_before_min") is None or r["gap_before_min"] >= 10) and r.get("level") is not None]
    b2b_cmp = None
    if len(b2b) >= 3 and len(brk) >= 3:
        x, y = np.asarray(b2b), np.asarray(brk)
        obs = float(x.mean() - y.mean())
        pool = np.concatenate([x, y])
        rng = np.random.default_rng(0)
        null = []
        for _ in range(5000):
            p = rng.permutation(pool)
            null.append(p[: x.size].mean() - p[x.size:].mean())
        pval = float((np.sum(np.abs(null) >= abs(obs)) + 1) / (len(null) + 1))
        b2b_cmp = {"n_b2b": int(x.size), "n_break": int(y.size), "mean_b2b": _n(float(x.mean())),
                   "mean_break": _n(float(y.mean())), "diff": _n(obs), "p": _n(pval)}

    summary = {
        "range": [start.isoformat(), end.isoformat()],
        "back_to_back": b2b_cmp,
        "n_meetings": len(responses),
        "vs_matched_controls": {"n": cmp.n_meetings, "mean_dz": _n(cmp.mean_diff), "ci": [_n(cmp.ci_low), _n(cmp.ci_high)],
                                "p": _n(cmp.p_value)},
        "lag_s": lag,
        "by_speaker": seg_eff.get("speaker", [])[:10],
        "by_tag": seg_eff.get("tags", [])[:15],
        "worst": sorted(responses, key=lambda r: -(r.get("auc") or 0))[:5],
        "slowest_recovery": sorted([r for r in responses if r.get("recovery_min") is not None],
                                   key=lambda r: -r["recovery_min"])[:5],
    }
    store.put_artefact(conn, "meetings:summary", f"{start.isoformat()}..{end.isoformat()}", summary)
    return summary


def _n(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 3)


# ---------------------------------------------------------------------------------- sleep


def sleep_job(conn: sqlite3.Connection, start: date, end: date, tz: str, need_min: float = 450.0) -> list[dict]:
    a, _ = analysis.local_day_bounds(start - timedelta(days=1), tz)
    _, b = analysis.local_day_bounds(end, tz)
    rows = conn.execute(
        "SELECT ts_ms, duration_s, stage, source FROM sleep_epochs WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (a, b)
    ).fetchall()
    if not rows:
        return []
    # one staging source per night: prefer Garmin's own (fit/gc) over anything else
    by_src: dict[str, list] = {}
    for r in rows:
        by_src.setdefault(r["source"].split(":")[0], []).append(r)
    src_rows = by_src.get("fit") or by_src.get("gc") or next(iter(by_src.values()))
    ep_ts = np.array([r["ts_ms"] for r in src_rows], dtype=np.int64)
    ep_du = np.array([r["duration_s"] for r in src_rows], dtype=np.float64)
    ep_st = np.array([r["stage"] for r in src_rows])

    hr_ts, hr = store.series(conn, M.HR, a, b)
    nights = []
    for onset, offset in sleep.nights_from_epochs(ep_ts, ep_du, ep_st):
        rr_ts, rr = store.rr(conn, onset, offset)
        rm = math.nan
        if rr.size > 300:
            from pulso.signal import hrv
            from pulso.signal.rr import clean_rr

            cl = clean_rr(rr_ts, rr)
            wins = hrv.rolling(cl, window_s=300, step_s=300)
            if wins:
                rm = float(np.median([w.rmssd for w in wins]))
        g_ts, g = store.series(conn, M.HRV_GARMIN, onset, offset)
        if math.isnan(rm) and g.size:
            rm = float(np.mean(g))
        n = sleep.summarize_night(onset, offset, ep_ts, ep_du, ep_st, hr_ts, hr, rm)
        d = n.as_dict() | {"wake_day": _day_of(offset, tz).isoformat()}
        nights.append(d)

    debts = sleep.sleep_debt([n["total_sleep_min"] for n in nights], need_min=need_min)
    lnr = np.array([math.log(n["rmssd_night"]) if n.get("rmssd_night") else np.nan for n in nights])
    ready = baseline.hrv_readiness([n["wake_day"] for n in nights], lnr)
    with store.transaction(conn):
        for i, n in enumerate(nights):
            n["debt_min"] = debts[i]
            n["hrv_status"] = ready.status[i]
            n["hrv_ms"] = n.get("rmssd_night")
            n["ln_rmssd_7d"] = None if math.isnan(ready.roll7[i]) else round(float(ready.roll7[i]), 3)
            store.put_artefact(conn, "sleep:night", n["wake_day"], n)
        if nights:
            store.put_artefact(conn, "sleep:last", "latest", nights[-1])

    # regularity over the whole range from asleep minutes
    bins = np.arange(a, b, MIN, dtype=np.int64)
    mask = analysis.sleep_mask(conn, bins).astype(float)
    sri = sleep.sleep_regularity_index(mask) if bins.size >= 2 * 1440 else math.nan
    store.put_artefact(conn, "sleep:regularity", f"{start.isoformat()}..{end.isoformat()}", {"sri": _n(sri)})
    return nights


# ----------------------------------------------------------------------------- daily table

DAILY_COLUMNS = (
    "arousal_mean", "arousal_min_above_1_5", "episodes", "episodes_mental", "sleep_h", "sleep_debt_min",
    "hrv_ms", "hr_nadir", "meetings", "meeting_minutes", "commits", "late_commits", "marks",
)


def daily_table(conn: sqlite3.Connection, start: date, end: date, tz: str) -> list[dict]:
    """One row per local day with everything that can be crossed: physiology, sleep (the night
    that ended that morning), agenda load and coding behaviour. Missing = None."""
    rows = []
    d = start
    while d <= end:
        a, b = analysis.local_day_bounds(d, tz)
        day = store.get_artefact(conn, "analysis:day", d.isoformat()) or {}
        night = store.get_artefact(conn, "sleep:night", d.isoformat()) or {}
        meets = store.events(conn, a, b, kinds=["meeting", "calendar"])
        commits = store.events(conn, a, b, kinds=["commit"])
        late = 0
        for c in commits:
            h = datetime.fromtimestamp(c.start_ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).hour
            late += h >= 22 or h < 5
        eps = day.get("episodes") or []
        rows.append({
            "day": d.isoformat(),
            "arousal_mean": day.get("arousal_mean_awake"),
            "arousal_min_above_1_5": day.get("arousal_minutes_above_1_5"),
            "episodes": len(eps) if day else None,
            "episodes_mental": sum(1 for e in eps if e["kind"] == "mental") if day else None,
            "sleep_h": round(night["total_sleep_min"] / 60, 2) if night.get("total_sleep_min") else None,
            "sleep_debt_min": night.get("debt_min"),
            "hrv_ms": night.get("hrv_ms"),
            "hr_nadir": night.get("hr_nadir"),
            "meetings": len(meets),
            "meeting_minutes": int(sum((min(e.end_ms, b) - max(e.start_ms, a)) / MIN for e in meets)),
            "commits": len(commits),
            "late_commits": late,
            "marks": len(store.events(conn, a, b, kinds=["note"])),
        })
        d += timedelta(days=1)
    return rows


def correlate_daily(rows: list[dict], a: str, b: str, lag_days: int = 0, seed: int = 0) -> dict:
    """Spearman correlation between column a on day t and column b on day t+lag, with a
    permutation p-value. lag 1 answers 'does today's X go with tomorrow's Y?'."""
    x = [r.get(a) for r in rows]
    y = [r.get(b) for r in rows]
    if lag_days:
        x, y = x[:-lag_days], y[lag_days:]
    pairs = [(u, v) for u, v in zip(x, y) if u is not None and v is not None]
    if len(pairs) < 6:
        return {"n": len(pairs), "rho": None, "p": None, "note": "menos de 6 dias com os dois dados"}
    from scipy.stats import rankdata

    xr = rankdata([p[0] for p in pairs])
    yr = rankdata([p[1] for p in pairs])
    rho = float(np.corrcoef(xr, yr)[0, 1]) if np.std(xr) > 0 and np.std(yr) > 0 else float("nan")
    rng = np.random.default_rng(seed)
    null = np.array([np.corrcoef(xr, rng.permutation(yr))[0, 1] for _ in range(2000)])
    p = float((np.sum(np.abs(null) >= abs(rho)) + 1) / (null.size + 1)) if not math.isnan(rho) else None
    return {"n": len(pairs), "rho": _n(rho), "p": _n(p), "lag_days": lag_days, "a": a, "b": b}


# ------------------------------------------------------------------------------ everything


def run_range(conn: sqlite3.Connection, start: date, end: date, tz: str) -> dict:
    """Analyse every day, then meetings and sleep over the range."""
    days = []
    d = start
    while d <= end:
        days.append(analysis.analyze_day(conn, d, tz).summary())
        d += timedelta(days=1)
    nights = sleep_job(conn, start, end, tz)
    meet = meeting_job(conn, start, end, tz)
    return {"days": days, "nights": nights, "meetings": meet}
