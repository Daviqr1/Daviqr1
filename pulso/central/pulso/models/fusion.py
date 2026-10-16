"""Latent arousal by sensor fusion (Kalman filter + RTS smoother).

None of the available signals *is* stress. Each is a noisy, intermittent, differently-biased
view of the same hidden state — sympathetic activation:

* additional HR (HR not explained by movement)            — every minute HR exists
* Garmin stress (HRV-derived, 3-min, blank while moving)    — intermittent
* −lnRMSSD from our own RR windows                          — only during capture sessions/night
* RSA-derived respiration rate deviation                    — when the HF band is clean

So we model it the way you'd fuse GNSS + IMU: a 1-D Ornstein-Uhlenbeck latent state (it decays
back to baseline with time constant τ) observed through several standardised channels, each with
its own noise variance R_i estimated from the data (a few EM-style passes over the innovations).
Missing observations are simply skipped, which is exactly what a Kalman filter is good at.
Output: per-minute arousal z-score with a 1σ band, plus each channel's share of the update.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Channel:
    name: str
    z: np.ndarray            # standardised observation per step (NaN = missing); + means "more aroused"
    r: float = 1.0           # observation noise variance (in z units); re-estimated


@dataclass
class FusionResult:
    ts_ms: np.ndarray
    mean: np.ndarray         # smoothed latent arousal (z units)
    sd: np.ndarray           # smoothed 1-sigma
    filtered: np.ndarray     # causal estimate (what the watch could have shown live)
    r: dict[str, float] = field(default_factory=dict)
    share: dict[str, np.ndarray] = field(default_factory=dict)   # per-channel contribution to the update


def fuse(ts_ms: np.ndarray, channels: list[Channel], tau_min: float = 20.0, step_min: float = 1.0,
         em_iters: int = 4) -> FusionResult:
    n = ts_ms.size
    phi = float(np.exp(-step_min / tau_min))
    qv = 1.0 - phi**2  # keeps the stationary variance of the latent state at 1
    for ch in channels:
        if ch.z.size != n:
            raise ValueError(f"channel {ch.name} has {ch.z.size} steps, grid has {n}")

    for it in range(max(1, em_iters)):
        xf, pf, xp, pp, shares = _filter(channels, phi, qv, n)
        xs, ps = _smooth(xf, pf, xp, pp, phi)
        if it == em_iters - 1:
            break
        # M-step for each channel's R: E[(z - x)^2] = (z - xs)^2 + ps over observed steps
        for ch in channels:
            m = np.isfinite(ch.z)
            if m.sum() >= 30:
                ch.r = float(np.clip(np.mean((ch.z[m] - xs[m]) ** 2 + ps[m]), 0.05, 25.0))

    return FusionResult(
        ts_ms=ts_ms, mean=xs, sd=np.sqrt(ps), filtered=xf,
        r={ch.name: ch.r for ch in channels}, share=shares,
    )


def _filter(channels: list[Channel], phi: float, qv: float, n: int):
    xf = np.zeros(n)
    pf = np.zeros(n)
    xp = np.zeros(n)
    pp = np.zeros(n)
    shares = {ch.name: np.zeros(n) for ch in channels}
    x, p = 0.0, 1.0
    for t in range(n):
        x, p = phi * x, phi * phi * p + qv
        xp[t], pp[t] = x, p
        # sequential scalar updates (channels are conditionally independent given x)
        total = 0.0
        moves: dict[str, float] = {}
        for ch in channels:
            z = ch.z[t]
            if not np.isfinite(z):
                continue
            k = p / (p + ch.r)
            dx = k * (z - x)
            x += dx
            p *= 1.0 - k
            moves[ch.name] = abs(dx)
            total += abs(dx)
        if total > 0:
            for name, v in moves.items():
                shares[name][t] = v / total
        xf[t], pf[t] = x, p
    return xf, pf, xp, pp, shares


def _smooth(xf, pf, xp, pp, phi):
    n = xf.size
    xs = xf.copy()
    ps = pf.copy()
    for t in range(n - 2, -1, -1):
        c = pf[t] * phi / pp[t + 1]
        xs[t] = xf[t] + c * (xs[t + 1] - xp[t + 1])
        ps[t] = pf[t] + c * c * (ps[t + 1] - pp[t + 1])
    return xs, ps
