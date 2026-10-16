"""Sleep: per-night summaries, regularity and debt.

* Nightly summary from staging epochs (Garmin's or ours) plus overnight physiology:
  nocturnal HR nadir and *when* it happens (a nadir late in the night is a classic sign of a
  late meal, alcohol or an evening that never wound down), overnight RMSSD when RR exists.
* Sleep Regularity Index (Phillips et al. 2017, Sci Rep 7:3216): probability of being in the
  same state (asleep/awake) at two time points 24 h apart, rescaled to −100..100. Irregularity
  predicts poor outcomes independently of duration.
* Sleep debt: rolling 14-day sum of (need − sleep), need defaulting to 7.5 h.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np

MIN = 60_000
ASLEEP = {"light", "deep", "rem"}


@dataclass
class Night:
    onset_ms: int
    offset_ms: int
    time_in_bed_min: float
    total_sleep_min: float
    efficiency: float
    deep_min: float
    light_min: float
    rem_min: float
    awake_min: float
    awakenings: int
    hr_nadir: float
    hr_nadir_at_frac: float       # 0 = at onset, 1 = at offset
    hr_mean: float
    rmssd_night: float            # from RR when available, else NaN

    def as_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in asdict(self).items()}


def nights_from_epochs(ts_ms: np.ndarray, dur_s: np.ndarray, stage: np.ndarray, gap_min: int = 90) -> list[tuple[int, int]]:
    """Group staging epochs into nights: consecutive epochs separated by < gap_min."""
    if ts_ms.size == 0:
        return []
    order = np.argsort(ts_ms)
    ts_ms, dur_s = ts_ms[order], dur_s[order]
    out: list[list[int]] = [[int(ts_ms[0]), int(ts_ms[0] + dur_s[0] * 1000)]]
    for t, d in zip(ts_ms[1:], dur_s[1:]):
        if t - out[-1][1] <= gap_min * MIN:
            out[-1][1] = max(out[-1][1], int(t + d * 1000))
        else:
            out.append([int(t), int(t + d * 1000)])
    return [(a, b) for a, b in out if b - a >= 3 * 60 * MIN]


def summarize_night(onset: int, offset: int, ep_ts: np.ndarray, ep_dur: np.ndarray, ep_stage: np.ndarray,
                    hr_ts: np.ndarray, hr: np.ndarray, rmssd_night: float = math.nan) -> Night:
    m = (ep_ts >= onset) & (ep_ts < offset)
    st, du = ep_stage[m], ep_dur[m] / 60.0
    mins = {s: float(du[st == s].sum()) for s in ("deep", "light", "rem", "awake")}
    tst = mins["deep"] + mins["light"] + mins["rem"]
    tib = (offset - onset) / MIN
    wake_runs = 0
    prev_awake = False
    for s in st:
        cur = s == "awake"
        if cur and not prev_awake:
            wake_runs += 1
        prev_awake = cur
    hm = (hr_ts >= onset) & (hr_ts < offset) & np.isfinite(hr)
    if hm.sum() >= 10:
        # nadir on a 5-min rolling median so one artefactual sample can't be "the nadir"
        hts, hv = hr_ts[hm], hr[hm]
        k = 5
        sm = np.array([np.median(hv[max(0, i - k // 2) : i + k // 2 + 1]) for i in range(hv.size)])
        j = int(np.argmin(sm))
        nadir, at = float(sm[j]), float((hts[j] - onset) / max(1, offset - onset))
        hmean = float(np.mean(hv))
    else:
        nadir = at = hmean = math.nan
    return Night(onset, offset, tib, tst, tst / tib if tib > 0 else math.nan, mins["deep"], mins["light"],
                 mins["rem"], mins["awake"], wake_runs, nadir, at, hmean, rmssd_night)


def sleep_regularity_index(asleep_by_minute: np.ndarray) -> float:
    """`asleep_by_minute`: bool/0-1 array on a contiguous 1-min grid covering >= 2 days
    (NaN = unknown). SRI = 200·P(same state at t and t+24h) − 100."""
    x = np.asarray(asleep_by_minute, dtype=np.float64)
    day = 1440
    if x.size < 2 * day:
        return math.nan
    a, b = x[:-day], x[day:]
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < day:
        return math.nan
    return float(200.0 * np.mean(a[ok] == b[ok]) - 100.0)


def sleep_debt(total_sleep_min: list[float], need_min: float = 450.0, window: int = 14) -> list[float]:
    """Rolling debt in minutes (positive = owed). Nights with no data count as zero debt
    rather than as zero sleep, because a missing watch is not a missing night."""
    out = []
    for i in range(len(total_sleep_min)):
        w = [need_min - v for v in total_sleep_min[max(0, i - window + 1) : i + 1] if v is not None and not math.isnan(v)]
        out.append(float(sum(w)))
    return out
