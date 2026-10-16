"""The daily analysis pipeline: raw store -> minute grid -> models -> derived series + artefacts.

    clean RR -> HRV windows ─┐
    HR + steps -> additional HR ─┼─> Kalman fusion -> arousal z (+σ) -> episodes -> events link
    Garmin stress ───────────────┘

Everything written back is tagged `derived:v1` so it can be recomputed and never confused with
measured data. All models are fitted on *trailing* history only (no peeking at the future).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso import metrics as M
from pulso import store
from pulso.models import additional_hr, baseline, episodes, fusion, nonmetabolic_hrv
from pulso.signal import hrv
from pulso.signal.rr import clean_rr
from pulso.signal.series import MIN, bin_mean, grid, mad

DAY = 24 * 60 * MIN
FIT_DAYS = 14
# Wrist PPI is only a fair HRV surrogate when still (Bent 2020; Georgiou 2018): windows whose mean
# ENMO exceeds this (sedentary/light-walking cut, Bakrania 2016) are not used as a stress signal.
STILL_ENMO_MG = 45.0
STILL_STEPS_PM = 5.0
ENMO_WIN = "enmo_win"


def local_day_bounds(day: date, tz: str) -> tuple[int, int]:
    z = ZoneInfo(tz)
    a = datetime.combine(day, time(0, 0), tzinfo=z).astimezone(timezone.utc)
    b = datetime.combine(day + timedelta(days=1), time(0, 0), tzinfo=z).astimezone(timezone.utc)
    return int(a.timestamp() * 1000), int(b.timestamp() * 1000)


def best_minute_series(conn: sqlite3.Connection, metric: str, start_ms: int, end_ms: int,
                       preference: tuple[str, ...] = M.HR_PREFERENCE) -> np.ndarray:
    """Per-minute mean of `metric`, taking each minute from the most preferred source that has
    data for it (source tags are matched by prefix, so 'fit:monitor' matches 'fit')."""
    bins = grid(start_ms, end_ms)
    out = np.full(bins.size, np.nan)
    rows = conn.execute(
        "SELECT DISTINCT source FROM samples WHERE metric = ? AND ts_ms >= ? AND ts_ms < ?",
        (metric, start_ms, end_ms),
    ).fetchall()
    sources = [r[0] for r in rows]

    def rank(src: str) -> int:
        for i, p in enumerate(preference):
            if src.startswith(p):
                return i
        return len(preference)

    for src in sorted(sources, key=rank):
        ts, v = store.series(conn, metric, start_ms, end_ms, source=src)
        b = bin_mean(ts, v, bins)
        fill = np.isnan(out) & np.isfinite(b)
        out[fill] = b[fill]
    return out


def steps_from_cumulative(conn: sqlite3.Connection, start_ms: int, end_ms: int) -> np.ndarray:
    """Steps/min spread evenly between consecutive daily-counter snapshots from the watch
    (ActivityMonitor.steps, reset at midnight). Gaps over 30 min stay NaN."""
    bins = grid(start_ms, end_ms)
    out = np.full(bins.size, np.nan)
    ts, v = store.series(conn, "steps_day_total", start_ms - 30 * MIN, end_ms)
    for (t0, a), (t1, b) in zip(zip(ts[:-1], v[:-1]), zip(ts[1:], v[1:])):
        dt_min = (t1 - t0) / MIN
        if dt_min <= 0 or dt_min > 30 or b < a:  # b < a: midnight reset
            continue
        i0 = max(0, int((t0 - bins[0]) // MIN))
        i1 = min(bins.size, int((t1 - bins[0]) // MIN))
        if i1 > i0:
            out[i0:i1] = (b - a) / dt_min
    return out


def sleep_mask(conn: sqlite3.Connection, bins: np.ndarray) -> np.ndarray:
    """True for minutes inside any sleep epoch (any source) or 'sleep' event."""
    m = np.zeros(bins.size, dtype=bool)
    if bins.size == 0:
        return m
    a, b = int(bins[0]), int(bins[-1] + MIN)
    for r in conn.execute(
        "SELECT ts_ms, duration_s, stage FROM sleep_epochs WHERE ts_ms < ? AND ts_ms + duration_s * 1000 > ?", (b, a)
    ):
        if r["stage"] == "unmeasurable":
            continue
        i0 = max(0, (r["ts_ms"] - a) // MIN)
        i1 = min(bins.size, (r["ts_ms"] + r["duration_s"] * 1000 - a + MIN - 1) // MIN)
        m[i0:i1] = True
    for ev in store.events(conn, a, b, kinds=["sleep"]):
        i0 = max(0, (ev.start_ms - a) // MIN)
        i1 = min(bins.size, (ev.end_ms - a + MIN - 1) // MIN)
        m[i0:i1] = True
    return m


def hrv_minutes(conn: sqlite3.Connection, start_ms: int, end_ms: int, window_s: float = 120.0) -> dict[str, np.ndarray]:
    """Clean all RR in range and compute 2-min windows stepped every minute, keyed to the
    minute grid by window end. Also returns the mean ENMO of each window (NaN when no
    accelerometer data covers it) so callers can keep only still windows."""
    bins = grid(start_ms, end_ms)
    keys = (M.RMSSD, M.LN_RMSSD, M.SDNN, M.DFA_A1, M.RESP_RSA, M.HR_RR, M.RR_QUALITY, ENMO_WIN)
    out = {k: np.full(bins.size, np.nan) for k in keys}
    ts, rr = store.rr(conn, start_ms - int(window_s * 1000), end_ms)
    if rr.size < 50:
        return out
    e_ts, e_v = store.series(conn, M.ENMO, start_ms - int(window_s * 1000), end_ms)
    clean = clean_rr(ts, rr)
    for w in hrv.rolling(clean, window_s=window_s, step_s=60.0, min_coverage=0.75, min_quality=0.85):
        i = (w.t_end_ms - bins[0]) // MIN - 1
        if 0 <= i < bins.size:
            if e_ts.size:
                m = (e_ts >= w.t_start_ms) & (e_ts < w.t_end_ms)
                if m.sum() >= 0.5 * window_s:
                    out[ENMO_WIN][i] = float(np.mean(e_v[m]))
            out[M.RMSSD][i] = w.rmssd
            out[M.LN_RMSSD][i] = w.ln_rmssd
            out[M.SDNN][i] = w.sdnn
            out[M.DFA_A1][i] = w.dfa_a1
            out[M.RESP_RSA][i] = w.resp_rate_rsa
            out[M.HR_RR][i] = w.mean_hr
            out[M.RR_QUALITY][i] = w.quality
    return out


@dataclass
class DayAnalysis:
    day: str
    ts_ms: np.ndarray
    hr: np.ndarray
    steps: np.ndarray
    asleep: np.ndarray
    additional_hr: np.ndarray
    arousal: np.ndarray
    arousal_sd: np.ndarray
    channel_r: dict[str, float]
    episodes: list[episodes.Episode]
    hrv: dict[str, np.ndarray]
    notes: list[str]

    def summary(self) -> dict:
        awake = ~self.asleep
        valid = awake & np.isfinite(self.arousal)
        return {
            "day": self.day,
            "minutes_with_hr": int(np.isfinite(self.hr).sum()),
            "minutes_awake_scored": int(valid.sum()),
            "arousal_mean_awake": _f(np.nanmean(self.arousal[valid])) if valid.any() else None,
            "arousal_minutes_above_1_5": int(np.sum(self.arousal[valid] > 1.5)),
            "additional_hr_mean_awake": _f(np.nanmean(self.additional_hr[awake])) if np.any(np.isfinite(self.additional_hr[awake])) else None,
            "episodes": [e.as_dict() for e in self.episodes],
            "channel_noise_r": self.channel_r,
            "rr_windows": int(np.isfinite(self.hrv[M.RMSSD]).sum()),
            "notes": self.notes,
        }


def _f(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _robust_z(x: np.ndarray, ref: np.ndarray) -> np.ndarray:
    ref = ref[np.isfinite(ref)]
    if ref.size < 30:
        return np.full(x.size, np.nan)
    s = mad(ref)
    if not s or math.isnan(s):
        return np.full(x.size, np.nan)
    return (x - np.median(ref)) / s


def analyze_day(conn: sqlite3.Connection, day: date, tz: str, write: bool = True) -> DayAnalysis:
    start, end = local_day_bounds(day, tz)
    fit_start = start - FIT_DAYS * DAY
    notes: list[str] = []

    # --- training window (trailing) + target day on one grid -----------------------------
    bins_all = grid(fit_start, end)
    hr_all = best_minute_series(conn, M.HR, fit_start, end)
    steps_all = best_minute_series(conn, M.STEPS_PM, fit_start, end, preference=(M.SRC_FIT, M.SRC_GC, M.SRC_CIQ_FACE))
    steps_watch = steps_from_cumulative(conn, fit_start, end)
    steps_all = np.where(np.isnan(steps_all), steps_watch, steps_all)
    hours_all, _ = baseline.local_hours(bins_all, tz)
    asleep_all = sleep_mask(conn, bins_all)
    day_sl = slice(int((start - bins_all[0]) // MIN), int((end - bins_all[0]) // MIN))
    ts = bins_all[day_sl]

    # --- additional HR -----------------------------------------------------------------
    add = np.full(ts.size, np.nan)
    train = np.zeros(bins_all.size, dtype=bool)
    train[: day_sl.start] = True
    try:
        model = additional_hr.fit(hr_all, steps_all, hours_all, awake=(~asleep_all) & train)
        add = model.additional(hr_all, steps_all, hours_all)[day_sl]
        add_z = add / model.resid_scale
        notes.append(f"additional-HR model fitted on {model.n_fit} trailing awake minutes")
    except ValueError:
        # not enough history yet: fit on the day itself (in-sample, flagged)
        try:
            model = additional_hr.fit(hr_all[day_sl], steps_all[day_sl], hours_all[day_sl], awake=~asleep_all[day_sl])
            add = model.additional(hr_all[day_sl], steps_all[day_sl], hours_all[day_sl])
            add_z = add / model.resid_scale
            notes.append("additional-HR model fitted in-sample (less than 2 h of trailing history)")
        except ValueError:
            add_z = np.full(ts.size, np.nan)
            notes.append("not enough HR to model additional HR")
    add_z = np.where(asleep_all[day_sl], np.nan, add_z)

    # --- Garmin stress channel (standardised against trailing awake history) -------------
    stress_all = best_minute_series(conn, M.STRESS_GARMIN, fit_start, end, preference=(M.SRC_FIT, M.SRC_GC, M.SRC_CIQ_FACE))
    # live 30-s stress snapshots from the watch fill minutes the 3-min series doesn't cover yet
    stress_now = best_minute_series(conn, M.STRESS_NOW, fit_start, end, preference=(M.SRC_CIQ_CAPTURE, M.SRC_CIQ_FACE))
    stress_all = np.where(np.isnan(stress_all), stress_now, stress_all)
    ref = stress_all[: day_sl.start][~asleep_all[: day_sl.start]]
    stress_z = _robust_z(stress_all[day_sl], ref if ref.size else stress_all[day_sl])
    stress_z = np.where(asleep_all[day_sl], np.nan, stress_z)

    # --- our own HRV channel -------------------------------------------------------------
    hv = hrv_minutes(conn, start, end)
    hrv_z = np.full(ts.size, np.nan)
    if np.any(np.isfinite(hv[M.LN_RMSSD])):
        hist = hrv_minutes(conn, fit_start, start)

        def still(h: dict, steps: np.ndarray) -> np.ndarray:
            e = h[ENMO_WIN]
            by_accel = np.isfinite(e) & (e < STILL_ENMO_MG)
            by_steps = ~np.isfinite(e) & ~(np.nan_to_num(steps, nan=0.0) > STILL_STEPS_PM)
            return np.isfinite(h[M.LN_RMSSD]) & (by_accel | by_steps)

        hours_day = hours_all[day_sl]
        hours_hist = hours_all[: day_sl.start]
        st_day = still(hv, steps_all[day_sl])
        st_hist = still(hist, steps_all[: day_sl.start])
        model_nm = nonmetabolic_hrv.fit(np.where(st_hist, hist[M.LN_RMSSD], np.nan), hist[ENMO_WIN], hours_hist)
        if model_nm is not None:
            hrv_z = model_nm.z(hv[M.LN_RMSSD], hv[ENMO_WIN], hours_day)
            notes.append(f"non-metabolic HRV model fitted on {model_nm.n_fit} still historical windows")
        else:
            ref = np.where(st_hist, hist[M.LN_RMSSD], np.nan)
            ref = ref if np.sum(np.isfinite(ref)) >= 30 else np.where(st_day, hv[M.LN_RMSSD], np.nan)
            hrv_z = -_robust_z(hv[M.LN_RMSSD], ref)
        n_moving = int(np.sum(np.isfinite(hv[M.LN_RMSSD]) & ~st_day))
        if n_moving:
            notes.append(f"{n_moving} RR windows dropped from the stress signal (moving)")
        hrv_z = np.where(st_day & ~asleep_all[day_sl], hrv_z, np.nan)

    channels = [fusion.Channel("additional_hr", add_z), fusion.Channel("garmin_stress", stress_z),
                fusion.Channel("rr_hrv", hrv_z)]
    channels = [c for c in channels if np.sum(np.isfinite(c.z)) > 0]
    if channels:
        fr = fusion.fuse(ts, channels)
        arousal, arousal_sd, rmap = fr.mean, fr.sd, fr.r
        arousal = np.where(asleep_all[day_sl], np.nan, arousal)
    else:
        arousal = np.full(ts.size, np.nan)
        arousal_sd = np.full(ts.size, np.nan)
        rmap = {}
        notes.append("no observation channel available for this day")

    eps = episodes.detect(ts, arousal, steps_all[day_sl])
    evs = store.events(conn, start - 60 * MIN, end + 60 * MIN, kinds=["meeting", "calendar", "commit", "note", "workout"])
    episodes.link_events(eps, evs)

    res = DayAnalysis(day.isoformat(), ts, hr_all[day_sl], steps_all[day_sl], asleep_all[day_sl], add,
                      arousal, arousal_sd, rmap, eps, hv, notes)
    if write:
        _persist(conn, res)
    return res


def _persist(conn: sqlite3.Connection, a: DayAnalysis) -> None:
    def rows(v: np.ndarray):
        return [(int(t), float(x)) for t, x in zip(a.ts_ms, v) if np.isfinite(x)]

    with store.transaction(conn):
        conn.execute(
            "DELETE FROM samples WHERE source = ? AND ts_ms >= ? AND ts_ms < ?",
            (M.DERIVED, int(a.ts_ms[0]), int(a.ts_ms[-1]) + MIN),
        )
        store.upsert_samples(conn, M.ADDITIONAL_HR, M.DERIVED, rows(a.additional_hr))
        store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, rows(a.arousal))
        store.upsert_samples(conn, M.AROUSAL_SD, M.DERIVED, rows(a.arousal_sd))
        for k, v in a.hrv.items():
            if k != ENMO_WIN:
                store.upsert_samples(conn, k, M.DERIVED, rows(v))
        store.put_artefact(conn, "analysis:day", a.day, a.summary())
