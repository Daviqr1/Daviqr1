"""Stress episodes ("picos") from the fused arousal series.

Hysteresis segmentation: an episode opens when arousal stays above `enter_z` for at least
`min_minutes`, and closes when it falls below `exit_z`. Episodes separated by less than
`merge_gap_min` are merged. Each episode is then labelled physical / mental / mixed from the
movement during it, and linked to whatever events overlap it (meetings, commits, notes).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Episode:
    start_ms: int
    end_ms: int
    peak_ms: int
    peak_z: float
    mean_z: float
    auc_z_min: float          # area above exit threshold, z·min
    movement: float           # mean activity during the episode (same units as input)
    kind: str                 # 'mental' | 'physical' | 'mixed'
    overlaps: list[str]       # event ids

    def as_dict(self) -> dict:
        return asdict(self)


def detect(ts_ms: np.ndarray, z: np.ndarray, activity: np.ndarray | None = None,
           enter_z: float = 1.5, exit_z: float = 0.5, min_minutes: int = 3, merge_gap_min: int = 5,
           step_ms: int = 60_000, physical_activity: float = 60.0) -> list[Episode]:
    """`activity` is per-step movement (e.g. steps/min). `physical_activity` is the level above
    which an episode is considered driven by movement (60 steps/min ~ walking)."""
    n = z.size
    raw: list[list[int]] = []
    i = 0
    zz = np.nan_to_num(z, nan=-np.inf)
    while i < n:
        if zz[i] > enter_z:
            j = i
            while j < n and zz[j] > enter_z:
                j += 1
            if j - i >= min_minutes:
                s = i
                while s > 0 and zz[s - 1] > exit_z:
                    s -= 1
                e = j
                while e < n and zz[e] > exit_z:
                    e += 1
                raw.append([s, e])
                i = e
                continue
            i = j
        else:
            i += 1
    merged: list[list[int]] = []
    for s, e in raw:
        if merged and (s - merged[-1][1]) * step_ms <= merge_gap_min * 60_000:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    out: list[Episode] = []
    for s, e in merged:
        seg = z[s:e]
        k = s + int(np.nanargmax(seg))
        mov = float(np.nanmean(activity[s:e])) if activity is not None and np.any(np.isfinite(activity[s:e])) else 0.0
        frac_moving = float(np.nanmean(activity[s:e] > physical_activity)) if activity is not None else 0.0
        kind = "physical" if frac_moving > 0.6 else "mixed" if frac_moving > 0.2 else "mental"
        out.append(Episode(
            start_ms=int(ts_ms[s]), end_ms=int(ts_ms[e - 1] + step_ms), peak_ms=int(ts_ms[k]),
            peak_z=float(z[k]), mean_z=float(np.nanmean(seg)),
            auc_z_min=float(np.nansum(np.maximum(seg - exit_z, 0)) * step_ms / 60_000),
            movement=mov, kind=kind, overlaps=[],
        ))
    return out


def link_events(episodes: list[Episode], events: list, pad_ms: int = 10 * 60_000) -> None:
    """Attach ids of events overlapping each episode (with padding: anticipation happens before
    a meeting and recovery after it)."""
    for ep in episodes:
        ep.overlaps = [ev.id for ev in events if ev.start_ms - pad_ms < ep.end_ms and ev.end_ms + pad_ms > ep.start_ms]


def changepoints(z: np.ndarray, penalty: float = 10.0, min_size: int = 15) -> list[int]:
    """Regime shifts in a day's arousal (PELT, Killick et al. 2012) — e.g. 'after 14:10 you never
    came back down'. Optional dependency: ruptures."""
    try:
        import ruptures as rpt
    except ImportError:  # pragma: no cover - optional
        return []
    x = np.nan_to_num(z, nan=float(np.nanmedian(z)) if np.any(np.isfinite(z)) else 0.0)
    if x.size < 2 * min_size:
        return []
    algo = rpt.Pelt(model="l2", min_size=min_size).fit(x.reshape(-1, 1))
    return [int(b) for b in algo.predict(pen=penalty) if b < x.size]
