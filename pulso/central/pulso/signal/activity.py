"""Movement intensity from the wrist accelerometer.

ENMO (Euclidean Norm Minus One, clipped at 0; van Hees et al. 2013, PLoS ONE 8:e61691) and MAD
(Mean Amplitude Deviation; Vähä-Ypyä et al. 2015) per epoch. Both are robust, cheap and
validated against energy expenditure; they are what we use to tell "heart racing because you
moved" from "heart racing while you sat still in a meeting".

Units: input accelerometer in milli-g (Connect IQ reports mG); outputs in milli-g.
"""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np


def enmo_mad_epochs(
    t0_ms: int, rate_hz: float, xyz_mg: np.ndarray, epoch_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Per-epoch ENMO and MAD for one chunk. Returns (epoch_ts_ms, enmo_mg, mad_mg)."""
    xyz = np.asarray(xyz_mg, dtype=np.float64)
    n = xyz.shape[0]
    per = max(1, int(round(rate_hz * epoch_s)))
    k = n // per
    if k == 0:
        e = np.empty(0)
        return e.astype(np.int64), e, e
    vm = np.sqrt(np.sum(xyz[: k * per] ** 2, axis=1)).reshape(k, per)
    enmo = np.maximum(vm - 1000.0, 0.0).mean(axis=1)
    mad = np.abs(vm - vm.mean(axis=1, keepdims=True)).mean(axis=1)
    ts = t0_ms + (np.arange(k) * per * 1000.0 / rate_hz).astype(np.int64)
    return ts, enmo, mad


def epochs_from_chunks(
    chunks: Iterable[tuple[int, float, np.ndarray]], epoch_s: float = 1.0
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    ts_l, e_l, m_l = [], [], []
    for t0, hz, xyz in chunks:
        ts, e, m = enmo_mad_epochs(t0, hz, xyz, epoch_s)
        ts_l.append(ts)
        e_l.append(e)
        m_l.append(m)
    if not ts_l:
        e = np.empty(0)
        return e.astype(np.int64), e, e
    ts = np.concatenate(ts_l)
    order = np.argsort(ts, kind="stable")
    return ts[order], np.concatenate(e_l)[order], np.concatenate(m_l)[order]


def cadence_spm(rate_hz: float, xyz_mg: np.ndarray, lo: float = 0.8, hi: float = 3.5) -> float:
    """Dominant step frequency (steps/min) from the vector magnitude spectrum; NaN when the
    wrist is not doing anything periodic. Arm swing gives one peak per stride, so the
    step rate is twice the arm-swing frequency when the stride peak dominates."""
    xyz = np.asarray(xyz_mg, dtype=np.float64)
    if xyz.shape[0] < rate_hz * 4:
        return float("nan")
    vm = np.sqrt(np.sum(xyz**2, axis=1))
    vm = vm - vm.mean()
    if np.std(vm) < 30.0:  # < 0.03 g of movement: not walking/running
        return float("nan")
    spec = np.abs(np.fft.rfft(vm * np.hanning(vm.size)))
    f = np.fft.rfftfreq(vm.size, 1.0 / rate_hz)
    m = (f >= lo) & (f <= hi)
    if not np.any(m):
        return float("nan")
    fpk = f[m][np.argmax(spec[m])]
    # vector magnitude oscillates once per step (heel strikes), so fpk is already step rate
    return float(fpk * 60.0)


def sedentary_mask(enmo_mg: np.ndarray, threshold_mg: float = 20.0) -> np.ndarray:
    """Epochs considered physically still (ENMO under ~20 mg is sitting/standing still)."""
    return np.asarray(enmo_mg) < threshold_mg
