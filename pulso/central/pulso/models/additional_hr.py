"""Additional heart rate: the part of your heart rate that movement does not explain.

Myrtek et al. (e.g. Myrtek, Aschenbrenner & Brügner 2005, Int J Psychophysiol 56:27-37;
Myrtek 2004 "Heart and Emotion") separated *emotional* from *metabolic* heart rate in daily
life by calibrating HR against physical activity and treating the excess as psychological
activation. Garmin's stress score is HRV-based and simply blanks out when you move; this model
keeps working through movement because it explains movement away instead of ignoring it.

Model (per minute, awake minutes only):

    HR_t = b0 + Σ_k b_k · EMA_τk(log1p(activity))_t + circadian(t) + e_t

fitted as a *low quantile* regression (q≈0.3): the expected HR of a relaxed you, given what
your body is doing and the time of day. Additional HR = HR − prediction, positive when the
heart is beating faster than the body needs.

Activity input is either Garmin steps/min (24/7) or accelerometer ENMO (capture sessions).
Time constants cover HR on-kinetics (~1 min), recovery (~3 min) and slow EPOC-like drift (~10 min).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

TAUS_MIN = (1.0, 3.0, 10.0)


def ema(x: np.ndarray, tau_steps: float) -> np.ndarray:
    """Exponential moving average treating NaN as 0 (no movement recorded)."""
    a = 1.0 - np.exp(-1.0 / max(tau_steps, 1e-9))
    out = np.empty(x.size)
    s = 0.0
    for i, v in enumerate(np.nan_to_num(x, nan=0.0)):
        s += a * (v - s)
        out[i] = s
    return out


def design(activity: np.ndarray, hours: np.ndarray, step_min: float = 1.0) -> np.ndarray:
    la = np.log1p(np.maximum(np.nan_to_num(activity, nan=0.0), 0.0))
    cols = [np.ones_like(la)]
    cols += [ema(la, tau / step_min) for tau in TAUS_MIN]
    w = 2 * np.pi * hours / 24.0
    cols += [np.cos(w), np.sin(w), np.cos(2 * w), np.sin(2 * w)]
    return np.column_stack(cols)


def quantile_irls(X: np.ndarray, y: np.ndarray, q: float = 0.3, iters: int = 60, eps: float = 1e-3) -> np.ndarray:
    """Linear quantile regression by iteratively reweighted least squares (check-loss with a
    small smoothing eps). Plenty for ~10 features and 10^5 rows; no LP solver needed."""
    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    for _ in range(iters):
        r = y - X @ beta
        w = np.where(r >= 0, q, 1 - q) / np.maximum(np.abs(r), eps)
        Xw = X * w[:, None]
        new = np.linalg.solve(X.T @ Xw + 1e-8 * np.eye(X.shape[1]), Xw.T @ y)
        if np.max(np.abs(new - beta)) < 1e-6:
            beta = new
            break
        beta = new
    return beta


@dataclass
class AdditionalHrModel:
    beta: np.ndarray
    q: float
    resid_scale: float     # MAD of residuals (bpm), used to standardise
    n_fit: int

    def predict(self, activity: np.ndarray, hours: np.ndarray) -> np.ndarray:
        return design(activity, hours) @ self.beta

    def additional(self, hr: np.ndarray, activity: np.ndarray, hours: np.ndarray) -> np.ndarray:
        return hr - self.predict(activity, hours)


def fit(hr: np.ndarray, activity: np.ndarray, hours: np.ndarray, awake: np.ndarray | None = None,
        q: float = 0.3) -> AdditionalHrModel:
    """Fit on awake minutes with valid HR. `activity` and `hours` must be on the same 1-min grid
    as `hr` (NaN activity = no data = treated as still)."""
    hr = np.asarray(hr, dtype=np.float64)
    X = design(activity, hours)
    ok = np.isfinite(hr)
    if awake is not None:
        ok &= awake
    if ok.sum() < 120:
        raise ValueError(f"need >= 120 awake minutes with HR to fit, got {int(ok.sum())}")
    beta = quantile_irls(X[ok], hr[ok], q=q)
    r = hr[ok] - X[ok] @ beta
    scale = float(1.4826 * np.median(np.abs(r - np.median(r))))
    return AdditionalHrModel(beta=beta, q=q, resid_scale=max(scale, 1.0), n_fit=int(ok.sum()))
