"""Non-metabolic HRV: how much lower your RMSSD is than your movement explains.

Verkuil et al. 2016 / Brown et al. 2020 (Front Neurosci 14:564123) fitted, per person, a
regression of RMSSD on accelerometry and flagged sustained negative residuals (> 2 SE below the
prediction for >= 7.5 min) as psychological-stress episodes — exploratory (n = 6), but the closest
published template for detecting stress from a wrist device in daily life.

Here: ln RMSSD ~ log1p(ENMO) + 24 h harmonics, fitted by median (q = 0.5) regression on still-ish
historical windows only; the residual, scaled by its MAD, is the "rr_hrv" channel of the fusion
(sign flipped so that + means more aroused).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from pulso.models.additional_hr import quantile_irls

MIN_FIT_WINDOWS = 60


def design(enmo: np.ndarray, hours: np.ndarray) -> np.ndarray:
    e = np.log1p(np.maximum(np.nan_to_num(enmo, nan=0.0), 0.0))
    w = 2 * np.pi * hours / 24.0
    return np.column_stack([np.ones_like(e), e, np.cos(w), np.sin(w)])


@dataclass
class NonMetabolicHrv:
    beta: np.ndarray
    scale: float
    n_fit: int

    def z(self, ln_rmssd: np.ndarray, enmo: np.ndarray, hours: np.ndarray) -> np.ndarray:
        """+ = RMSSD lower than expected (more aroused)."""
        return -(ln_rmssd - design(enmo, hours) @ self.beta) / self.scale


def fit(ln_rmssd: np.ndarray, enmo: np.ndarray, hours: np.ndarray) -> NonMetabolicHrv | None:
    ok = np.isfinite(ln_rmssd)
    if ok.sum() < MIN_FIT_WINDOWS:
        return None
    X = design(enmo[ok], hours[ok])
    beta = quantile_irls(X, ln_rmssd[ok], q=0.5)
    r = ln_rmssd[ok] - X @ beta
    scale = float(1.4826 * np.median(np.abs(r - np.median(r))))
    return NonMetabolicHrv(beta, max(scale, 0.05), int(ok.sum()))
