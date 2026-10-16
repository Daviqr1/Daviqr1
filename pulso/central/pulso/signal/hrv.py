"""HRV metrics on cleaned RR windows.

Time domain (Task Force 1996): mean RR/HR, SDNN, RMSSD, lnRMSSD, pNN50.
Poincaré: SD1, SD2.
Frequency domain: Lomb-Scargle periodogram directly on the unevenly sampled series (no
resampling artefacts), VLF/LF/HF band powers. LF/HF is reported but should NOT be read as
"sympathovagal balance" (Billman 2013, Front Physiol 4:26) — the reports say so.
HF peak frequency -> respiratory rate estimate (RSA), useful when the breathing band is clean.
DFA alpha1 (Peng 1995; scales 4..16 beats) — the exercise-intensity marker of Rogers et al. 2021
(Front Physiol 11:596567): ~0.75 near the aerobic threshold, ~0.5 near the anaerobic one.

Every function returns NaN rather than a made-up number when the window is not good enough.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
from scipy.signal import lombscargle

from pulso.signal.rr import FLAG_OK, CleanResult

VLF = (0.0033, 0.04)
LF = (0.04, 0.15)
HF = (0.15, 0.40)


@dataclass
class HrvWindow:
    t_start_ms: int
    t_end_ms: int
    n_beats: int
    coverage: float        # fraction of the window covered by beats (0..1)
    quality: float         # fraction of untouched (not corrected) beats
    mean_rr: float
    mean_hr: float
    sdnn: float
    rmssd: float
    ln_rmssd: float
    pnn50: float
    sd1: float
    sd2: float
    lf: float
    hf: float
    lf_hf: float
    hf_peak_hz: float
    resp_rate_rsa: float   # breaths/min estimated from the HF peak
    dfa_a1: float

    def as_dict(self) -> dict:
        return {k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in asdict(self).items()}


def time_domain(rr: np.ndarray) -> dict[str, float]:
    rr = np.asarray(rr, dtype=np.float64)
    if rr.size < 3:
        return dict(mean_rr=math.nan, mean_hr=math.nan, sdnn=math.nan, rmssd=math.nan,
                    ln_rmssd=math.nan, pnn50=math.nan, sd1=math.nan, sd2=math.nan)
    d = np.diff(rr)
    rmssd = float(np.sqrt(np.mean(d**2)))
    sdnn = float(np.std(rr, ddof=1))
    sd1 = float(np.sqrt(0.5) * np.std(d, ddof=1))
    sd2_sq = 2 * sdnn**2 - sd1**2
    return dict(
        mean_rr=float(np.mean(rr)),
        mean_hr=float(60000.0 / np.mean(rr)),
        sdnn=sdnn,
        rmssd=rmssd,
        ln_rmssd=float(np.log(rmssd)) if rmssd > 0 else math.nan,
        pnn50=float(np.mean(np.abs(d) > 50.0) * 100.0),
        sd1=sd1,
        sd2=float(np.sqrt(sd2_sq)) if sd2_sq > 0 else math.nan,
    )


def frequency_domain(ts_ms: np.ndarray, rr: np.ndarray, min_duration_s: float = 120.0) -> dict[str, float]:
    """Lomb-Scargle band powers (ms^2). Needs >= ~2 min for LF to mean anything."""
    nan = dict(lf=math.nan, hf=math.nan, lf_hf=math.nan, hf_peak_hz=math.nan, resp_rate_rsa=math.nan)
    rr = np.asarray(rr, dtype=np.float64)
    if rr.size < 30:
        return nan
    t = (np.asarray(ts_ms, dtype=np.float64) - ts_ms[0]) / 1000.0
    if t[-1] < min_duration_s:
        return nan
    y = rr - rr.mean()
    f = np.linspace(VLF[0], HF[1], 800)
    # Unnormalised Lomb-Scargle gives P = N*A^2/4 at the peak of a sinusoid of amplitude A;
    # the peak is ~1/T wide (T = N/fs), so PSD = 2P/fs integrates to the variance A^2/2.
    p = lombscargle(t, y, 2 * np.pi * f)
    fs_mean = 1.0 / float(np.mean(np.diff(t)))
    psd = 2.0 * p / fs_mean
    df = f[1] - f[0]

    def band(lo: float, hi: float) -> float:
        m = (f >= lo) & (f < hi)
        return float(np.sum(psd[m]) * df)

    lf, hf = band(*LF), band(*HF)
    hm = (f >= HF[0]) & (f < HF[1])
    hf_peak = float(f[hm][np.argmax(psd[hm])]) if np.any(hm) else math.nan
    return dict(
        lf=lf,
        hf=hf,
        lf_hf=lf / hf if hf > 0 else math.nan,
        hf_peak_hz=hf_peak,
        resp_rate_rsa=hf_peak * 60.0 if not math.isnan(hf_peak) else math.nan,
    )


def dfa_alpha1(rr: np.ndarray, scales: range = range(4, 17)) -> float:
    """Short-term DFA scaling exponent. Needs ~>= 100 beats to be stable (Rogers uses 2-min windows)."""
    rr = np.asarray(rr, dtype=np.float64)
    if rr.size < 50:
        return math.nan
    y = np.cumsum(rr - rr.mean())
    ns, fs = [], []
    for n in scales:
        k = y.size // n
        if k < 4:
            continue
        x = np.arange(n, dtype=np.float64)
        A = np.vstack([x, np.ones(n)]).T
        pinv = np.linalg.pinv(A)
        flucts = []
        # forward and backward segmentation so the tail is not ignored
        for segs in (y[: k * n].reshape(k, n), y[y.size - k * n :].reshape(k, n)):
            coef = pinv @ segs.T
            resid = segs - (A @ coef).T
            flucts.append(np.mean(resid**2, axis=1))
        F = np.sqrt(np.mean(np.concatenate(flucts)))
        if F > 0:
            ns.append(n)
            fs.append(F)
    if len(ns) < 4:
        return math.nan
    slope = np.polyfit(np.log(ns), np.log(fs), 1)[0]
    return float(slope)


def window_metrics(ts_ms: np.ndarray, rr: np.ndarray, flag: np.ndarray, t0: int, t1: int) -> HrvWindow:
    m = (ts_ms >= t0) & (ts_ms < t1)
    wts, wrr, wfl = ts_ms[m], rr[m], flag[m]
    dur = max(1.0, (t1 - t0))
    coverage = float(min(1.0, np.sum(wrr) / dur)) if wrr.size else 0.0
    quality = float(np.mean(wfl == FLAG_OK)) if wrr.size else 0.0
    td = time_domain(wrr)
    fd = frequency_domain(wts, wrr) if wrr.size else frequency_domain(np.empty(0), np.empty(0))
    return HrvWindow(
        t_start_ms=int(t0), t_end_ms=int(t1), n_beats=int(wrr.size), coverage=coverage, quality=quality,
        dfa_a1=dfa_alpha1(wrr), **td, **fd,
    )


def rolling(
    clean: CleanResult,
    window_s: float = 300.0,
    step_s: float = 60.0,
    min_coverage: float = 0.8,
    min_quality: float = 0.9,
) -> list[HrvWindow]:
    """Sliding windows over a cleaned series; windows failing coverage/quality are dropped,
    not filled. Returned windows are aligned to multiples of step_s (UTC)."""
    if clean.rr_ms.size == 0:
        return []
    ts, rr, fl = clean.ts_ms, clean.rr_ms, clean.flag
    w, st = int(window_s * 1000), int(step_s * 1000)
    start = (int(ts[0]) // st) * st
    out: list[HrvWindow] = []
    for t0 in range(start, int(ts[-1]) - w + st + 1, st):
        hw = window_metrics(ts, rr, fl, t0, t0 + w)
        if hw.coverage >= min_coverage and hw.quality >= min_quality:
            out.append(hw)
    return out
