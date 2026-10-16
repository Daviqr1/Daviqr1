"""Irregular -> regular time grids without inventing data.

Garmin series arrive at different cadences (HR every 1-120 s, stress every 3 min, body battery
every few minutes, our RR windows every minute). Analysis wants them on one grid. The rule here
is: aggregate inside a bin, carry a value forward at most `max_gap` and otherwise leave NaN.
"""

from __future__ import annotations

import numpy as np

MIN = 60_000


def grid(start_ms: int, end_ms: int, step_ms: int = MIN) -> np.ndarray:
    """Bin starts aligned to multiples of step_ms (UTC)."""
    s = (start_ms // step_ms) * step_ms
    return np.arange(s, end_ms, step_ms, dtype=np.int64)


def bin_mean(ts_ms: np.ndarray, values: np.ndarray, bins: np.ndarray, step_ms: int = MIN) -> np.ndarray:
    """Mean of the samples falling in each [bin, bin+step). NaN for empty bins."""
    out = np.full(bins.size, np.nan)
    if ts_ms.size == 0 or bins.size == 0:
        return out
    idx = ((ts_ms - bins[0]) // step_ms).astype(np.int64)
    ok = (idx >= 0) & (idx < bins.size) & np.isfinite(values)
    sums = np.bincount(idx[ok], weights=values[ok], minlength=bins.size)
    cnts = np.bincount(idx[ok], minlength=bins.size)
    with np.errstate(invalid="ignore", divide="ignore"):
        m = sums / cnts
    out[cnts > 0] = m[cnts > 0]
    return out


def bin_sum(ts_ms: np.ndarray, values: np.ndarray, bins: np.ndarray, step_ms: int = MIN) -> np.ndarray:
    out = np.zeros(bins.size)
    if ts_ms.size == 0 or bins.size == 0:
        return out
    idx = ((ts_ms - bins[0]) // step_ms).astype(np.int64)
    ok = (idx >= 0) & (idx < bins.size) & np.isfinite(values)
    out += np.bincount(idx[ok], weights=values[ok], minlength=bins.size)
    return out


def hold(values: np.ndarray, max_steps: int) -> np.ndarray:
    """Forward-fill NaNs, but never further than max_steps bins from the last real value."""
    out = values.copy()
    last, age = np.nan, max_steps + 1
    for i in range(out.size):
        if np.isfinite(out[i]):
            last, age = out[i], 0
        else:
            age += 1
            if age <= max_steps:
                out[i] = last
    return out


def interp_limited(ts_ms: np.ndarray, values: np.ndarray, at_ms: np.ndarray, max_gap_ms: int) -> np.ndarray:
    """Linear interpolation that refuses to bridge gaps longer than max_gap_ms."""
    out = np.full(at_ms.size, np.nan)
    ok = np.isfinite(values)
    ts, v = ts_ms[ok], values[ok]
    if ts.size < 2:
        if ts.size == 1:
            out[np.abs(at_ms - ts[0]) <= max_gap_ms // 2] = v[0]
        return out
    y = np.interp(at_ms, ts, v, left=np.nan, right=np.nan)
    j = np.searchsorted(ts, at_ms, side="right")
    jl = np.clip(j - 1, 0, ts.size - 1)
    jr = np.clip(j, 0, ts.size - 1)
    gap = ts[jr] - ts[jl]
    exact = ts[jl] == at_ms
    good = (gap <= max_gap_ms) | exact
    out[good] = y[good]
    return out


def robust_z(x: np.ndarray, center: np.ndarray | float, scale: np.ndarray | float) -> np.ndarray:
    with np.errstate(invalid="ignore", divide="ignore"):
        return (x - center) / np.where(np.asarray(scale) > 0, scale, np.nan)


def mad(x: np.ndarray) -> float:
    """Median absolute deviation scaled to be consistent with the SD for normal data."""
    x = x[np.isfinite(x)]
    if x.size == 0:
        return float("nan")
    return float(1.4826 * np.median(np.abs(x - np.median(x))))
