"""Beat-to-beat (RR / PPI) cleaning.

Wrist optical PPI from the FR255 is noisy: motion, loose strap and cold hands produce missed
beats, extra beats and plain garbage. Every HRV number downstream is only as good as this step,
so it is deliberately conservative and it *reports* what it did (flags + quality), instead of
silently returning a nice-looking series.

Pipeline
--------
1. hard physiological gate (300..2000 ms) and gap segmentation (beats missing in time);
2. per contiguous segment, artefact detection with the Lipponen & Tarvainen (2019) method
   ("A robust algorithm for heart rate variability time series artefact correction using novel
   beat classification", J Med Eng Technol 43(3):173-181, doi:10.1080/03091902.2019.1640306),
   which is what Kubios uses: dRR and median-deviation series normalised by time-varying
   thresholds (alpha * quartile deviation over 91 beats), then classification of ectopic,
   long/short, missed and extra beats;
3. correction: ectopic -> beat moved to the midpoint (preserves duration), missed -> interval
   split, extra -> intervals merged, long/short -> interpolated from the local median.

Inputs/outputs are milliseconds. `ts_ms` is the timestamp of the beat that closes each interval.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

RR_MIN_MS = 300.0   # 200 bpm
RR_MAX_MS = 2000.0  # 30 bpm

FLAG_OK = 0
FLAG_CORRECTED = 1
FLAG_REJECTED = 2

# Lipponen & Tarvainen constants (paper defaults)
_ALPHA = 5.2
_WINDOW = 91
_MEDFILT = 11
_C1 = 0.13
_C2 = 0.17


@dataclass
class Artefacts:
    ectopic: list[int] = field(default_factory=list)
    missed: list[int] = field(default_factory=list)
    extra: list[int] = field(default_factory=list)
    longshort: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.ectopic) + len(self.missed) + len(self.extra) + len(self.longshort)


@dataclass
class CleanResult:
    ts_ms: np.ndarray          # int64, beat timestamps after correction
    rr_ms: np.ndarray          # float64, corrected intervals
    flag: np.ndarray           # int8, FLAG_* per output interval
    segment: np.ndarray        # int32, contiguous segment id per output interval
    n_input: int
    n_rejected: int            # dropped by the physiological gate
    artefacts: Artefacts

    @property
    def quality(self) -> float:
        """Fraction of output intervals that were not touched by the cleaner (0..1)."""
        if self.rr_ms.size == 0:
            return 0.0
        return float(np.mean(self.flag == FLAG_OK))


def _rolling_quantile(x: np.ndarray, q: float, window: int) -> np.ndarray:
    """Centred rolling quantile with shrinking edges (min_periods=1 semantics)."""
    n = x.size
    half = window // 2
    out = np.empty(n)
    if n <= window:
        # small series: use expanding/shrinking windows explicitly
        for i in range(n):
            lo, hi = max(0, i - half), min(n, i + half + 1)
            out[i] = np.quantile(x[lo:hi], q)
        return out
    from numpy.lib.stride_tricks import sliding_window_view

    core = np.quantile(sliding_window_view(x, window), q, axis=1)
    out[half : n - half] = core
    for i in range(half):
        out[i] = np.quantile(x[: i + half + 1], q)
        j = n - 1 - i
        out[j] = np.quantile(x[j - half :], q)
    return out


def _rolling_median(x: np.ndarray, window: int) -> np.ndarray:
    return _rolling_quantile(x, 0.5, window)


def _threshold(x: np.ndarray) -> np.ndarray:
    a = np.abs(x)
    q1 = _rolling_quantile(a, 0.25, _WINDOW)
    q3 = _rolling_quantile(a, 0.75, _WINDOW)
    th = _ALPHA * (q3 - q1) / 2.0
    # avoid division by zero on perfectly regular (synthetic/paced) series
    return np.maximum(th, 1e-6)


def detect_artefacts(rr_ms: np.ndarray) -> Artefacts:
    """Classify artefacts in one contiguous RR segment (Lipponen & Tarvainen 2019, Fig. 1)."""
    rr = np.asarray(rr_ms, dtype=np.float64) / 1000.0
    n = rr.size
    art = Artefacts()
    if n < 5:
        return art

    drrs = np.diff(rr, prepend=rr[0])
    drrs[0] = np.mean(drrs[1:])
    th1 = _threshold(drrs)
    drrs = drrs / th1

    pad = 2
    dp = np.pad(drrs, pad, mode="reflect")
    s12 = np.zeros(n)
    s22 = np.zeros(n)
    for k in range(n):
        d = k + pad
        if dp[d] > 0:
            s12[k] = max(dp[d - 1], dp[d + 1])
        elif dp[d] < 0:
            s12[k] = min(dp[d - 1], dp[d + 1])
        if dp[d] >= 0:
            s22[k] = min(dp[d + 1], dp[d + 2])
        else:
            s22[k] = max(dp[d + 1], dp[d + 2])

    medrr = _rolling_median(rr, _MEDFILT)
    mrrs = rr - medrr
    mrrs[mrrs < 0] *= 2.0
    th2 = _threshold(mrrs)
    mrrs = mrrs / th2

    i = 0
    while i < n - 2:
        if abs(drrs[i]) <= 1:
            i += 1
            continue
        eq1 = drrs[i] > 1 and s12[i] < (-_C1 * drrs[i] - _C2)
        eq2 = drrs[i] < -1 and s12[i] > (-_C1 * drrs[i] + _C2)
        if eq1 or eq2:
            art.ectopic.append(i)
            i += 1
            continue
        if not (abs(drrs[i]) > 1 or abs(mrrs[i]) > 3):
            i += 1
            continue
        candidates = [i]
        if abs(drrs[i + 1]) < abs(drrs[i + 2]):
            candidates.append(i + 1)
        for j in candidates:
            eq3 = drrs[j] > 1 and s22[j] < -1
            eq4 = abs(mrrs[j]) > 3
            eq5 = drrs[j] < -1 and s22[j] > 1
            if not (eq3 or eq4 or eq5):
                continue
            eq6 = abs(rr[j] / 2 - medrr[j]) < th2[j]
            eq7 = j + 1 < n and abs(rr[j] + rr[j + 1] - medrr[j]) < th2[j]
            if eq5 and eq7:
                art.extra.append(j)
            elif eq3 and eq6:
                art.missed.append(j)
            else:
                art.longshort.append(j)
        i += len(candidates)
    # a beat can be reached twice through the candidate look-ahead; keep unique, sorted
    art.ectopic = sorted(set(art.ectopic))
    art.missed = sorted(set(art.missed))
    art.extra = sorted(set(art.extra))
    art.longshort = sorted(set(art.longshort) - set(art.missed) - set(art.extra) - set(art.ectopic))
    return art


def _correct_segment(ts: np.ndarray, rr: np.ndarray, art: Artefacts) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply corrections to one segment. Returns (ts, rr, flag)."""
    ts = ts.astype(np.float64).copy()
    rr = rr.astype(np.float64).copy()
    flag = np.zeros(rr.size, dtype=np.int8)
    med = _rolling_median(rr, _MEDFILT)

    # Merges/splits own their indices; an extra beat at j consumes j+1 too, so the other
    # classes must not touch them (the remainder of a split interval looks "long/short").
    missed = set(art.missed)
    extra = {j for j in art.extra if j + 1 < rr.size}
    owned = missed | extra | {j + 1 for j in extra}
    touched: set[int] = set()

    # 1) ectopic: one beat is misplaced, so one interval is short and its neighbour long (or
    #    the reverse). The classifier may point at either interval of the pair, so pick the
    #    neighbouring pair whose equalisation best restores the local median, then split its
    #    total duration equally (sum preserved -> timeline preserved).
    for k in art.ectopic:
        best, gain_best = None, 0.0
        for a, b in ((k - 1, k), (k, k + 1)):
            if a < 0 or b >= rr.size or a in owned or b in owned or a in touched or b in touched:
                continue
            m = (med[a] + med[b]) / 2.0
            before = abs(rr[a] - m) + abs(rr[b] - m)
            after = 2.0 * abs((rr[a] + rr[b]) / 2.0 - m)
            if before - after > gain_best:
                best, gain_best = (a, b), before - after
        if best is None:
            continue
        a, b = best
        rr[a] = rr[b] = (rr[a] + rr[b]) / 2.0
        ts[a] = ts[b] - rr[b]
        flag[a] = flag[b] = FLAG_CORRECTED
        touched.update(best)

    # 2) long/short: replace by the local median (timeline shifts slightly, flagged)
    for i in art.longshort:
        if i in owned or i in touched:
            continue
        rr[i] = med[i]
        flag[i] = FLAG_CORRECTED

    # 3) missed (split) and extra (merge) change the number of beats -> rebuild arrays
    out_ts: list[float] = []
    out_rr: list[float] = []
    out_flag: list[int] = []
    i = 0
    while i < rr.size:
        if i in extra and i + 1 < rr.size:
            merged = rr[i] + rr[i + 1]
            out_ts.append(ts[i + 1])
            out_rr.append(merged)
            out_flag.append(FLAG_CORRECTED)
            i += 2
            continue
        if i in missed:
            k = max(2, int(round(rr[i] / max(med[i], 1.0))))
            part = rr[i] / k
            for m in range(k):
                out_ts.append(ts[i] - rr[i] + part * (m + 1))
                out_rr.append(part)
                out_flag.append(FLAG_CORRECTED)
            i += 1
            continue
        out_ts.append(ts[i])
        out_rr.append(rr[i])
        out_flag.append(int(flag[i]))
        i += 1
    return np.asarray(out_ts), np.asarray(out_rr), np.asarray(out_flag, dtype=np.int8)


def segment_by_gaps(ts_ms: np.ndarray, rr_ms: np.ndarray, tol_ms: float = 1500.0) -> np.ndarray:
    """Segment id per beat: a new segment starts when the time between two consecutive
    beat timestamps exceeds the interval that is supposed to separate them by more than tol_ms
    (i.e. beats are missing from the stream, not just irregular)."""
    if ts_ms.size == 0:
        return np.empty(0, dtype=np.int32)
    dt = np.diff(ts_ms.astype(np.float64), prepend=ts_ms[0] - rr_ms[0])
    new = (dt - rr_ms) > tol_ms
    new[0] = False
    return np.cumsum(new).astype(np.int32)


def clean_rr(ts_ms: np.ndarray, rr_ms: np.ndarray, min_segment: int = 20) -> CleanResult:
    """Full cleaning pipeline. Segments shorter than `min_segment` beats are rejected
    (too short to tell an artefact from a real change)."""
    ts = np.asarray(ts_ms, dtype=np.int64)
    rr = np.asarray(rr_ms, dtype=np.float64)
    order = np.argsort(ts, kind="stable")
    ts, rr = ts[order], rr[order]
    n_input = rr.size

    gate = (rr >= RR_MIN_MS) & (rr <= RR_MAX_MS)
    n_rejected = int(np.sum(~gate))
    ts, rr = ts[gate], rr[gate]

    seg = segment_by_gaps(ts, rr)
    all_art = Artefacts()
    parts_ts, parts_rr, parts_flag, parts_seg = [], [], [], []
    for s in np.unique(seg):
        m = seg == s
        sts, srr = ts[m], rr[m]
        if srr.size < min_segment:
            n_rejected += int(srr.size)
            continue
        art = detect_artefacts(srr)
        cts, crr, cfl = _correct_segment(sts, srr, art)
        offset = int(np.flatnonzero(m)[0])
        all_art.ectopic += [offset + k for k in art.ectopic]
        all_art.missed += [offset + k for k in art.missed]
        all_art.extra += [offset + k for k in art.extra]
        all_art.longshort += [offset + k for k in art.longshort]
        parts_ts.append(cts)
        parts_rr.append(crr)
        parts_flag.append(cfl)
        parts_seg.append(np.full(crr.size, s, dtype=np.int32))

    if not parts_rr:
        e = np.empty(0)
        return CleanResult(e.astype(np.int64), e, e.astype(np.int8), e.astype(np.int32), n_input, n_rejected, all_art)
    return CleanResult(
        ts_ms=np.round(np.concatenate(parts_ts)).astype(np.int64),
        rr_ms=np.concatenate(parts_rr),
        flag=np.concatenate(parts_flag),
        segment=np.concatenate(parts_seg),
        n_input=n_input,
        n_rejected=n_rejected,
        artefacts=all_art,
    )
