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


def dedup_meetings(evs: list[store.Event]) -> list[store.Event]:
    """A transcript and its calendar entry describe the same meeting: keep the transcript one."""
    meets = [e for e in evs if e.kind == "meeting"]
    covered = {e.attrs.get("start_source", "").removeprefix("calendar:") for e in meets}
    return meets + [e for e in evs if e.kind == "calendar" and e.id not in covered
                    and not any(abs(m.start_ms - e.start_ms) < 10 * MIN for m in meets)]


def meeting_job(conn: sqlite3.Connection, start: date, end: date, tz: str) -> dict:
    """Per-meeting responses, the matched-control test, and in-meeting segment effects."""
    a, _ = analysis.local_day_bounds(start, tz)
    _, b = analysis.local_day_bounds(end, tz)
    ts, z = arousal_range(conn, a - 42 * DAY, b + DAY)  # controls look ±6 weeks around
    meets = dedup_meetings(store.events(conn, a, b, kinds=["meeting", "calendar"]))
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


# readiness compares the 7-day mean with the 60-day normal range, debt sums 14 days: every night is
# scored on a calendar-day grid reaching this far back, whatever range the caller asked for
SLEEP_CONTEXT_DAYS = 60
# a later epoch within this gap would still join the night (sleep.nights_from_epochs), so a night is
# only final, and only written, once its last epoch is this old, or once Garmin has closed it (its
# 'sleep' event, the main sleep period of dailySleepDTO, ends where the night ends)
NIGHT_OPEN_MIN = 90
STAGING_PREFERENCE = ("fit", "gc")  # Garmin's own staging first; anything else only when alone


def _staging_source(families: np.ndarray) -> str:
    present = set(families.tolist())
    for f in STAGING_PREFERENCE:
        if f in present:
            return f
    vals, counts = np.unique(families, return_counts=True)
    return str(vals[int(np.argmax(counts))])


def _night_rmssd(conn: sqlite3.Connection, onset: int, offset: int) -> float:
    """Median 5-min RMSSD of the night's RR (one source per minute), else Garmin's readings."""
    rr_ts, rr = analysis.best_rr(conn, onset, offset)
    rm = math.nan
    if rr.size > 300:
        from pulso.signal import hrv
        from pulso.signal.rr import clean_rr

        wins = hrv.rolling(clean_rr(rr_ts, rr), window_s=300, step_s=300)
        if wins:
            rm = float(np.median([w.rmssd for w in wins]))
    if math.isnan(rm):
        _, g = store.series(conn, M.HRV_GARMIN, onset, offset)
        if g.size:
            rm = float(np.mean(g))
    return rm


def sleep_job(conn: sqlite3.Connection, start: date, end: date, tz: str, need_min: float = 450.0) -> list[dict]:
    """Summarise the nights that ended (wake day) in [start, end] and write `sleep:night` for each.

    Each night gets its own trailing context: debt and HRV status are computed on a calendar-day grid
    reaching SLEEP_CONTEXT_DAYS back (missing nights are gaps, not skipped), so a short run — the
    morning refresh, `analyze --days 1` — scores a night exactly like a long one. Nights before
    `start` are context only (their stored summary when there is one) and are never rewritten.
    Staging comes from one source per night (fit > gc > other), and a night that may still be filling
    in (last epoch less than NIGHT_OPEN_MIN old, not closed by Garmin) is left for a later run."""
    first = start - timedelta(days=SLEEP_CONTEXT_DAYS)
    # from the evening before the first wake day, so no night kept below is clipped at the bound
    a, _ = analysis.local_day_bounds(first - timedelta(days=1), tz)
    _, b = analysis.local_day_bounds(end, tz)
    rows = conn.execute(
        "SELECT ts_ms, duration_s, stage, source FROM sleep_epochs WHERE ts_ms >= ? AND ts_ms < ? ORDER BY ts_ms", (a, b)
    ).fetchall()
    if not rows:
        return []
    ep_ts = np.array([r["ts_ms"] for r in rows], dtype=np.int64)
    ep_du = np.array([r["duration_s"] for r in rows], dtype=np.float64)
    ep_st = np.array([r["stage"] for r in rows])
    ep_fam = np.array([r["source"].split(":")[0] for r in rows])
    final_before = store.now_ms() - NIGHT_OPEN_MIN * MIN
    closed = [e.end_ms for e in store.events(conn, a, b, kinds=["sleep"])]

    def final(offset: int) -> bool:
        return offset <= final_before or any(abs(t - offset) <= 15 * MIN for t in closed)

    by_day: dict[str, dict] = {}  # wake day -> main sleep (most sleep) of that day
    for u_on, u_off in sleep.nights_from_epochs(ep_ts, ep_du, ep_st):  # all sources together
        inside = (ep_ts >= u_on) & (ep_ts < u_off)
        m = inside & (ep_fam == _staging_source(ep_fam[inside]))
        s_ts, s_du, s_st = ep_ts[m], ep_du[m], ep_st[m]
        for onset, offset in sleep.nights_from_epochs(s_ts, s_du, s_st):
            wake = _day_of(offset, tz)
            if wake < first or wake > end or not final(offset):
                continue
            key = wake.isoformat()
            if wake < start:
                cached = store.get_artefact(conn, "sleep:night", key)
                if cached and cached.get("total_sleep_min") is not None:
                    if key not in by_day or cached["total_sleep_min"] > by_day[key]["total_sleep_min"]:
                        by_day[key] = cached | {"_context": True}
                    continue
            hr_ts, hr = store.series(conn, M.HR, onset, offset)
            n = sleep.summarize_night(onset, offset, s_ts, s_du, s_st, hr_ts, hr, _night_rmssd(conn, onset, offset))
            d = n.as_dict() | {"wake_day": key, "_context": wake < start}
            if key not in by_day or d["total_sleep_min"] > by_day[key]["total_sleep_min"]:
                by_day[key] = d

    days = [first + timedelta(days=k) for k in range((end - first).days + 1)]
    grid_n = [by_day.get(d.isoformat()) for d in days]
    debts = sleep.sleep_debt([n["total_sleep_min"] if n else None for n in grid_n], need_min=need_min)
    lnr = np.array([math.log(n["rmssd_night"]) if n and n.get("rmssd_night") else np.nan for n in grid_n])
    ready = baseline.hrv_readiness([d.isoformat() for d in days], lnr)
    nights = []
    with store.transaction(conn):
        for i, n in enumerate(grid_n):
            if n is None or n.pop("_context"):
                continue
            n["debt_min"] = debts[i]
            n["hrv_status"] = ready.status[i]
            n["hrv_ms"] = n.get("rmssd_night")
            n["ln_rmssd_7d"] = None if math.isnan(ready.roll7[i]) else round(float(ready.roll7[i]), 3)
            store.put_artefact(conn, "sleep:night", n["wake_day"], n)
            nights.append(n)
        last = store.get_artefact(conn, "sleep:last", "latest")
        if nights and (not last or str(last.get("wake_day", "")) <= nights[-1]["wake_day"]):
            store.put_artefact(conn, "sleep:last", "latest", nights[-1])  # never moved back by an old range

    # regularity over the requested range from asleep minutes
    a, _ = analysis.local_day_bounds(start - timedelta(days=1), tz)
    bins = np.arange(a, b, MIN, dtype=np.int64)
    mask = analysis.sleep_mask(conn, bins).astype(float)
    sri = sleep.sleep_regularity_index(mask) if bins.size >= 2 * 1440 else math.nan
    store.put_artefact(conn, "sleep:regularity", f"{start.isoformat()}..{end.isoformat()}", {"sri": _n(sri)})
    return nights


def last_night(conn: sqlite3.Connection, today: date, tz: str) -> dict | None:
    """Morning refresh: the night that ended today, summarised as soon as its staging is in and
    final (Garmin publishes it after the wake-up sync, hours after the 04:30 range job). Returns it,
    or None while it is still missing."""
    night = store.get_artefact(conn, "sleep:night", today.isoformat())
    if night is not None:
        return night
    a, _ = analysis.local_day_bounds(today, tz)
    if conn.execute("SELECT 1 FROM sleep_epochs WHERE ts_ms >= ? LIMIT 1", (a - 12 * 60 * MIN,)).fetchone() is None:
        return None  # nothing staged since yesterday noon
    sleep_job(conn, today, today, tz)
    return store.get_artefact(conn, "sleep:night", today.isoformat())


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
        meets = dedup_meetings(store.events(conn, a, b, kinds=["meeting", "calendar"]))
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
