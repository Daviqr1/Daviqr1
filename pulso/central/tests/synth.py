"""Synthetic physiology for tests: RR series with known properties and controlled artefacts."""

from __future__ import annotations

import numpy as np


def rr_series(
    n: int = 600,
    mean_rr: float = 900.0,
    hf_amp: float = 30.0,
    hf_hz: float = 0.25,
    lf_amp: float = 0.0,
    lf_hz: float = 0.1,
    noise_sd: float = 5.0,
    t0_ms: int = 1_700_000_000_000,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray]:
    """RR intervals (ms) modulated by respiration (HF) and optionally LF, with white noise.
    Returns (beat_ts_ms, rr_ms)."""
    rng = np.random.default_rng(seed)
    rr = np.empty(n)
    t = 0.0
    for i in range(n):
        v = mean_rr + hf_amp * np.sin(2 * np.pi * hf_hz * t) + lf_amp * np.sin(2 * np.pi * lf_hz * t)
        v += rng.normal(0, noise_sd)
        rr[i] = v
        t += v / 1000.0
    ts = t0_ms + np.cumsum(rr).astype(np.int64)
    return ts, rr


def inject_missed(ts: np.ndarray, rr: np.ndarray, idx: list[int]) -> tuple[np.ndarray, np.ndarray]:
    """Merge interval i with i+1 (one beat not detected -> one long interval)."""
    ts, rr = ts.copy(), rr.copy().astype(float)
    keep = np.ones(rr.size, dtype=bool)
    for i in idx:
        rr[i + 1] = rr[i] + rr[i + 1]
        keep[i] = False
    return ts[keep], rr[keep]


def inject_extra(ts: np.ndarray, rr: np.ndarray, idx: list[int], frac: float = 0.35) -> tuple[np.ndarray, np.ndarray]:
    """Split interval i in two (a spurious beat detected inside it)."""
    out_ts, out_rr = [], []
    s = set(idx)
    for i in range(rr.size):
        if i in s:
            a = rr[i] * frac
            out_ts += [ts[i] - (rr[i] - a), ts[i]]
            out_rr += [a, rr[i] - a]
        else:
            out_ts.append(ts[i])
            out_rr.append(rr[i])
    return np.asarray(out_ts, dtype=np.int64), np.asarray(out_rr)


def inject_ectopic(ts: np.ndarray, rr: np.ndarray, idx: list[int], frac: float = 0.3) -> tuple[np.ndarray, np.ndarray]:
    """Premature beat: interval i shortened by frac, i+1 lengthened by the same amount."""
    ts, rr = ts.copy(), rr.copy().astype(float)
    for i in idx:
        d = rr[i] * frac
        rr[i] -= d
        rr[i + 1] += d
        ts[i] -= int(d)
    return ts, rr


def rmssd(rr: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.diff(rr) ** 2)))
