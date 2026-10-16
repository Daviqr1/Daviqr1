"""Performance: training load and HRV-derived thresholds.

* TRIMP (Banister 1991): duration × HR reserve fraction × 0.64·e^(1.92·x) (men) / 0.86·e^(1.67·x) (women).
* Fitness–fatigue impulse-response (Banister et al. 1975; Busso 2003): fitness τ≈42 d,
  fatigue τ≈7 d, form = fitness − 2·fatigue. ACWR is *not* used — Impellizzeri et al. 2020
  (Int J Sports Physiol Perform 15:907) showed the ratio is a statistical artefact.
* HRV thresholds from DFA α1 during incremental runs (Rogers et al. 2021, Front Physiol
  11:596567): HR at α1 = 0.75 ≈ first ventilatory/aerobic threshold, α1 = 0.5 ≈ second.
  Needs true beat-to-beat data (a chest strap is far better than the wrist here).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def trimp(hr: np.ndarray, dt_s: np.ndarray | float, hr_rest: float, hr_max: float, sex: str = "male") -> float:
    hr = np.asarray(hr, dtype=np.float64)
    x = np.clip((hr - hr_rest) / max(1.0, hr_max - hr_rest), 0, 1)
    a, b = (0.64, 1.92) if sex == "male" else (0.86, 1.67)
    w = a * np.exp(b * x)
    dt_min = np.broadcast_to(np.asarray(dt_s, dtype=np.float64) / 60.0, hr.shape)
    ok = np.isfinite(x)
    return float(np.sum(dt_min[ok] * x[ok] * w[ok]))


@dataclass
class FitnessFatigue:
    fitness: np.ndarray
    fatigue: np.ndarray
    form: np.ndarray


def fitness_fatigue(daily_load: np.ndarray, tau_fit: float = 42.0, tau_fat: float = 7.0,
                    k_fit: float = 1.0, k_fat: float = 2.0) -> FitnessFatigue:
    load = np.nan_to_num(np.asarray(daily_load, dtype=np.float64))
    fit = np.zeros(load.size)
    fat = np.zeros(load.size)
    a1, a2 = np.exp(-1 / tau_fit), np.exp(-1 / tau_fat)
    f1 = f2 = 0.0
    for i, w in enumerate(load):
        f1 = f1 * a1 + w
        f2 = f2 * a2 + w
        fit[i], fat[i] = f1, f2
    return FitnessFatigue(fit, fat, k_fit * fit - k_fat * fat)


@dataclass
class DfaThresholds:
    hr_at_075: float
    hr_at_050: float
    slope: float          # d(alpha1)/d(HR)
    r2: float
    n: int


def dfa_thresholds(hr: np.ndarray, alpha1: np.ndarray) -> DfaThresholds:
    """Linear fit of α1 vs HR over the falling part of the curve (α1 between 0.3 and 1.2),
    solved for 0.75 and 0.5. Returns NaNs when the run didn't cross the range."""
    hr = np.asarray(hr, dtype=np.float64)
    a = np.asarray(alpha1, dtype=np.float64)
    m = np.isfinite(hr) & np.isfinite(a) & (a > 0.3) & (a < 1.2)
    nan = DfaThresholds(math.nan, math.nan, math.nan, math.nan, int(m.sum()))
    if m.sum() < 6 or np.ptp(hr[m]) < 15:
        return nan
    slope, icpt = np.polyfit(hr[m], a[m], 1)
    if slope >= 0:
        return nan
    pred = slope * hr[m] + icpt
    ss_res = float(np.sum((a[m] - pred) ** 2))
    ss_tot = float(np.sum((a[m] - a[m].mean()) ** 2)) or 1.0
    return DfaThresholds((0.75 - icpt) / slope, (0.5 - icpt) / slope, float(slope), 1 - ss_res / ss_tot, int(m.sum()))
