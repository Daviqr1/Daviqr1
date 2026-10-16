"""What meetings (and moments inside them) do to you.

Three levels, from coarse to fine, all within-person:

1. Event-locked response per meeting: baseline (−45..−15 min), anticipation (−15..0), during,
   peak, area above baseline, and recovery time after the end.
2. Is it the meeting or just the hour? Each meeting is compared with *matched control windows*
   — same weekday type, same clock time, same duration, on days when you had no meeting then.
   The paired differences are tested with a sign-flip permutation test and reported with a
   bootstrap CI. If the effect is not distinguishable from the hour of day, the report says so.
3. Inside the meeting: transcript segments (who spoke, what topic, tags from the AI tagger) are
   aligned to the arousal series with a physiological lag (estimated by cross-correlation,
   default 20 s) and aggregated per speaker/tag with bootstrap CIs — "when X talks about
   deadlines your arousal rises 0.6 z (CI 0.3..0.9)".
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import asdict, dataclass

import numpy as np

MIN = 60_000


@dataclass
class MeetingResponse:
    event_id: str
    baseline: float
    anticipation: float     # mean(−15..0) − baseline
    during: float           # mean(during) − baseline
    peak: float             # max(during) − baseline
    auc: float              # Σ max(z − baseline, 0) · minutes, during
    recovery_min: float     # minutes after end until back within baseline + 0.25 (NaN if never in 60 min)
    coverage: float         # fraction of the meeting with data
    level: float = math.nan  # mean z during, NOT baseline-corrected (z is already relative to your norm)

    def as_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in asdict(self).items()}


def _window(ts: np.ndarray, z: np.ndarray, a: int, b: int) -> np.ndarray:
    m = (ts >= a) & (ts < b)
    return z[m]


def response(ts: np.ndarray, z: np.ndarray, event_id: str, start_ms: int, end_ms: int) -> MeetingResponse:
    base_w = _window(ts, z, start_ms - 45 * MIN, start_ms - 15 * MIN)
    base = float(np.nanmedian(base_w)) if np.any(np.isfinite(base_w)) else 0.0
    ant = _window(ts, z, start_ms - 15 * MIN, start_ms)
    dur = _window(ts, z, start_ms, end_ms)
    n_expected = max(1, (end_ms - start_ms) // MIN)
    cov = float(np.sum(np.isfinite(dur)) / n_expected)
    rec = math.nan
    post_m = (ts >= end_ms) & (ts < end_ms + 60 * MIN)
    for t, v in zip(ts[post_m], z[post_m]):
        if np.isfinite(v) and v <= base + 0.25:
            rec = (t - end_ms) / MIN
            break
    f = lambda w: float(np.nanmean(w)) - base if np.any(np.isfinite(w)) else math.nan  # noqa: E731
    return MeetingResponse(
        event_id=event_id, baseline=base, anticipation=f(ant), during=f(dur),
        peak=float(np.nanmax(dur)) - base if np.any(np.isfinite(dur)) else math.nan,
        auc=float(np.nansum(np.maximum(dur - base, 0))) if dur.size else math.nan,
        recovery_min=rec, coverage=cov,
        level=float(np.nanmean(dur)) if np.any(np.isfinite(dur)) else math.nan,
    )


@dataclass
class ControlComparison:
    n_meetings: int
    mean_diff: float          # mean(meeting during − control during), z units
    ci_low: float
    ci_high: float
    p_value: float            # two-sided sign-flip permutation test
    per_meeting: list[dict]


def matched_controls(ts: np.ndarray, z: np.ndarray, meetings: list, busy: list, tz: str,
                     max_controls: int = 8, seed: int = 0) -> ControlComparison:
    """`meetings` and `busy` are store.Event lists; `busy` = everything that disqualifies a
    control window (all meetings/calendar items)."""
    from datetime import datetime, timedelta, timezone
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz)
    rng = np.random.default_rng(seed)
    busy_iv = sorted((e.start_ms, e.end_ms) for e in busy)

    def free(a: int, b: int) -> bool:
        return not any(s < b + 15 * MIN and e > a - 15 * MIN for s, e in busy_iv)

    def mean_in(a: int, b: int) -> float:
        w = _window(ts, z, a, b)
        return float(np.nanmean(w)) if np.sum(np.isfinite(w)) >= max(3, (b - a) // MIN // 2) else math.nan

    diffs, rows = [], []
    for ev in meetings:
        m_val = mean_in(ev.start_ms, ev.end_ms)
        if math.isnan(m_val):
            continue
        d0 = datetime.fromtimestamp(ev.start_ms / 1000, tz=timezone.utc).astimezone(zone)
        controls = []
        for k in range(1, 43):  # look up to 6 weeks around, both directions
            for sign in (-1, 1):
                d = d0 + timedelta(days=sign * k)
                if (d.weekday() >= 5) != (d0.weekday() >= 5):
                    continue
                a = int(d.astimezone(timezone.utc).timestamp() * 1000)
                b = a + (ev.end_ms - ev.start_ms)
                if free(a, b):
                    v = mean_in(a, b)
                    if not math.isnan(v):
                        controls.append(v)
            if len(controls) >= max_controls:
                break
        if len(controls) < 2:
            continue
        c = float(np.mean(controls))
        diffs.append(m_val - c)
        rows.append({"event_id": ev.id, "meeting": m_val, "control": c, "n_controls": len(controls)})

    x = np.asarray(diffs)
    if x.size < 3:
        return ControlComparison(int(x.size), math.nan, math.nan, math.nan, math.nan, rows)
    obs = float(x.mean())
    flips = rng.choice([-1.0, 1.0], size=(10_000, x.size))
    null = (flips * x).mean(axis=1)
    p = float((np.sum(np.abs(null) >= abs(obs)) + 1) / (null.size + 1))
    boots = rng.choice(x, size=(5_000, x.size), replace=True).mean(axis=1)
    lo, hi = np.percentile(boots, [2.5, 97.5])
    return ControlComparison(int(x.size), obs, float(lo), float(hi), p, rows)


def estimate_lag(ts: np.ndarray, z: np.ndarray, segments: list[dict], max_lag_s: int = 90,
                 step_ms: int = 60_000) -> int:
    """Lag (s) maximising the correlation between 'user is being addressed/speaking' indicator
    and arousal. Falls back to 20 s when the data cannot tell (flat correlation)."""
    if not segments or z.size < 10:
        return 20
    ind = np.zeros(z.size)
    for s in segments:
        a = np.searchsorted(ts, s["start_ms"])
        b = np.searchsorted(ts, s["end_ms"])
        ind[a : max(b, a + 1)] = 1.0
    zz = np.nan_to_num(z - np.nanmean(z))
    best, best_c = 20, -np.inf
    for lag_s in range(0, max_lag_s + 1, 10):
        k = int(round(lag_s * 1000 / step_ms))
        if k >= z.size - 2:
            break
        a, b = ind[: z.size - k], zz[k:]
        if np.std(a) == 0 or np.std(b) == 0:
            continue
        c = float(np.corrcoef(a, b)[0, 1])
        if c > best_c + 1e-3:
            best, best_c = lag_s, c
    return best if np.isfinite(best_c) else 20


def segment_effects(ts: np.ndarray, z: np.ndarray, segments: list[dict], lag_s: int = 20,
                    keys: tuple[str, ...] = ("speaker", "tags"), seed: int = 0,
                    min_n: int = 3) -> dict[str, list[dict]]:
    """Per speaker / per tag: mean arousal (lagged, baseline-corrected by the meeting median)
    with bootstrap 95% CI. `segments` dicts: start_ms, end_ms, event_id, speaker, tags(list)."""
    rng = np.random.default_rng(seed)
    lag = lag_s * 1000
    bounds: dict[str, list[int]] = {}
    for s in segments:
        b = bounds.setdefault(s.get("event_id", ""), [s["start_ms"], s["end_ms"]])
        b[0], b[1] = min(b[0], s["start_ms"]), max(b[1], s["end_ms"])
    by_event_median: dict[str, float] = {}
    for eid, (a, b) in bounds.items():
        w = _window(ts, z, a + lag, b + lag)
        by_event_median[eid] = float(np.nanmedian(w)) if np.any(np.isfinite(w)) else math.nan
    groups: dict[str, dict[str, list[float]]] = {k: defaultdict(list) for k in keys}
    for s in segments:
        w = _window(ts, z, s["start_ms"] + lag, max(s["end_ms"], s["start_ms"] + MIN) + lag)
        base = by_event_median.get(s.get("event_id", ""), math.nan)
        if not np.any(np.isfinite(w)) or math.isnan(base):
            continue
        v = float(np.nanmean(w)) - base
        for key in keys:
            vals = s.get(key)
            if vals is None:
                continue
            for label in vals if isinstance(vals, list) else [vals]:
                groups[key][str(label)].append(v)
    out: dict[str, list[dict]] = {}
    for key, g in groups.items():
        rows = []
        for label, vals in g.items():
            x = np.asarray(vals)
            if x.size < min_n:
                continue
            boots = rng.choice(x, size=(2_000, x.size), replace=True).mean(axis=1)
            lo, hi = np.percentile(boots, [2.5, 97.5])
            rows.append({"label": label, "n": int(x.size), "mean_dz": float(x.mean()),
                         "ci_low": float(lo), "ci_high": float(hi)})
        out[key] = sorted(rows, key=lambda r: -r["mean_dz"])
    return out
