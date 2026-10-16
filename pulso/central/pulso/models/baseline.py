"""Personal baselines.

Population norms are useless for a single person's HRV (inter-individual spread dwarfs
day-to-day change), so everything is compared against *your own* history:

* time-of-day profiles (median/MAD per hour, weekday vs weekend) — a 15:00 HR is compared with
  your usual 15:00 HR, not with your 04:00 HR;
* cosinor fits (24 h + 12 h harmonics) for circadian amplitude/acrophase (Cornelissen 2014,
  Theor Biol Med Model 11:16);
* HRV-guided "readiness" in the style of Plews et al. 2013 (Sports Med 43:773-781) and
  Kiviniemi et al. 2007: 7-day rolling mean of lnRMSSD against a normal range of the 60-day
  mean ± 0.5 SD (the smallest worthwhile change), plus the 7-day coefficient of variation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso.signal.series import mad


def local_hours(ts_ms: np.ndarray, tz: str) -> tuple[np.ndarray, np.ndarray]:
    """(fractional local hour 0..24, is_weekend) for UTC ms timestamps."""
    z = ZoneInfo(tz)
    hours = np.empty(ts_ms.size)
    weekend = np.empty(ts_ms.size, dtype=bool)
    # vectorise per unique UTC offset hour to keep this fast on long series
    for i, t in enumerate(ts_ms):
        d = datetime.fromtimestamp(t / 1000.0, tz=timezone.utc).astimezone(z)
        hours[i] = d.hour + d.minute / 60.0 + d.second / 3600.0
        weekend[i] = d.weekday() >= 5
    return hours, weekend


@dataclass
class HourProfile:
    median: np.ndarray   # [2, 24] (weekday, weekend) x hour
    scale: np.ndarray    # [2, 24] MAD-based SD
    count: np.ndarray    # [2, 24]

    def expected(self, hours: np.ndarray, weekend: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = np.clip(hours.astype(int), 0, 23)
        w = weekend.astype(int)
        med = self.median[w, h]
        sc = self.scale[w, h]
        # sparse cells fall back to the all-days value for that hour
        sparse = self.count[w, h] < 10
        if np.any(sparse):
            pooled_med = np.nanmedian(self.median, axis=0)[h]
            pooled_sc = np.nanmedian(self.scale, axis=0)[h]
            med = np.where(sparse, pooled_med, med)
            sc = np.where(sparse, pooled_sc, sc)
        return med, sc


def hour_profile(ts_ms: np.ndarray, values: np.ndarray, tz: str) -> HourProfile:
    hours, weekend = local_hours(ts_ms, tz)
    med = np.full((2, 24), np.nan)
    sc = np.full((2, 24), np.nan)
    cnt = np.zeros((2, 24), dtype=int)
    ok = np.isfinite(values)
    for w in (0, 1):
        for h in range(24):
            m = ok & (weekend == bool(w)) & (hours.astype(int) == h)
            cnt[w, h] = int(m.sum())
            if cnt[w, h] >= 3:
                med[w, h] = float(np.median(values[m]))
                sc[w, h] = mad(values[m])
    return HourProfile(med, sc, cnt)


@dataclass
class Cosinor:
    mesor: float
    amplitude: float
    acrophase_h: float     # local clock hour of the fitted peak
    r2: float
    coef: np.ndarray       # [mesor, c1, s1, c2, s2]

    def predict(self, hours: np.ndarray) -> np.ndarray:
        w = 2 * np.pi * hours / 24.0
        X = np.column_stack([np.ones_like(w), np.cos(w), np.sin(w), np.cos(2 * w), np.sin(2 * w)])
        return X @ self.coef


def cosinor(hours: np.ndarray, values: np.ndarray) -> Cosinor:
    ok = np.isfinite(values)
    h, y = hours[ok], values[ok]
    if y.size < 24:
        nan = math.nan
        return Cosinor(nan, nan, nan, nan, np.full(5, np.nan))
    w = 2 * np.pi * h / 24.0
    X = np.column_stack([np.ones_like(w), np.cos(w), np.sin(w), np.cos(2 * w), np.sin(2 * w)])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    fit = X @ coef
    ss_res = float(np.sum((y - fit) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1.0
    grid = np.linspace(0, 24, 24 * 12, endpoint=False)
    model = Cosinor(float(coef[0]), 0.0, 0.0, 1 - ss_res / ss_tot, coef)
    curve = model.predict(grid)
    model.amplitude = float((curve.max() - curve.min()) / 2.0)
    model.acrophase_h = float(grid[int(np.argmax(curve))])
    return model


@dataclass
class Readiness:
    day: list[str]
    ln_rmssd: np.ndarray
    roll7: np.ndarray
    cv7: np.ndarray
    normal_lo: np.ndarray
    normal_hi: np.ndarray
    status: list[str]        # 'below' | 'normal' | 'above' | 'insufficient'


def hrv_readiness(days: list[str], ln_rmssd: np.ndarray, long_window: int = 60) -> Readiness:
    """Plews-style status per day. Needs >= 14 days of history before calling anything."""
    x = np.asarray(ln_rmssd, dtype=np.float64)
    n = x.size
    roll7 = np.full(n, np.nan)
    cv7 = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    hi = np.full(n, np.nan)
    status: list[str] = []
    for i in range(n):
        w7 = x[max(0, i - 6) : i + 1]
        w7 = w7[np.isfinite(w7)]
        if w7.size >= 4:
            roll7[i] = w7.mean()
            cv7[i] = float(np.std(w7, ddof=1) / w7.mean() * 100.0) if w7.size > 1 else np.nan
        wl = x[max(0, i - long_window + 1) : i + 1]
        wl = wl[np.isfinite(wl)]
        if wl.size >= 14 and np.isfinite(roll7[i]):
            mu, sd = wl.mean(), np.std(wl, ddof=1)
            lo[i], hi[i] = mu - 0.5 * sd, mu + 0.5 * sd
            status.append("below" if roll7[i] < lo[i] else "above" if roll7[i] > hi[i] else "normal")
        else:
            status.append("insufficient")
    return Readiness(list(days), x, roll7, cv7, lo, hi, status)
