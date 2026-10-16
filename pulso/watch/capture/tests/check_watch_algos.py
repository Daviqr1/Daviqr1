"""Host-side checks for the capture app's on-watch algorithms.

These are hand transliterations of the Monkey C code (float32 where the watch uses Float), run
against the central's reference implementations and simple simulations. Keep them in sync with
source/HrvLive.mc, AccelFeatures.mc, BatchBuilder.mc and Governor.mc.

    python3 watch/capture/tests/check_watch_algos.py      (needs numpy + pydantic for the wire test)
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import types
from pathlib import Path

import numpy as np

CENTRAL = Path(__file__).resolve().parents[3] / "central"
sys.path.insert(0, str(CENTRAL))
f32 = np.float32
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str) -> None:
    print(("ok   " if ok else "FAIL ") + name + ": " + detail)
    if not ok:
        FAILED.append(name)


def central_dfa():
    """central dfa_alpha1; scipy (only needed there for Lomb-Scargle) is stubbed if missing."""
    try:
        import scipy.signal  # noqa: F401
    except ImportError:
        stub, sig = types.ModuleType("scipy"), types.ModuleType("scipy.signal")
        sig.lombscargle = None
        stub.signal = sig
        sys.modules["scipy"], sys.modules["scipy.signal"] = stub, sig
    from pulso.signal.hrv import dfa_alpha1
    return dfa_alpha1


# ---- HrvLive.dfaBegin/dfaStep/fluct/boxRss ------------------------------------------------------
def watch_dfa(g: list[int], n_win: int = 120) -> float | None:
    mean = f32(sum(g)) / f32(n_win)
    acc, y = f32(0.0), []
    for v in g:
        acc = f32(acc + (f32(v) - mean))
        y.append(acc)

    def box_rss(s: int, boxes: int, start: int) -> f32:
        xm, sxxc, f2 = f32((s - 1) / 2.0), f32((s * (s * s - 1)) / 12.0), f32(0.0)
        for b in range(boxes):
            o, sy, sjy = start + b * s, f32(0), f32(0)
            for j in range(s):
                sy = f32(sy + y[o + j])
                sjy = f32(sjy + f32(j) * y[o + j])
            ym, slope = f32(sy / f32(s)), f32((sjy - xm * sy) / sxxc)
            for j in range(s):
                r = f32(y[o + j] - ym - slope * f32(j - xm))
                f2 = f32(f2 + r * r)
        return f2

    sx = sy = sxx = sxy = f32(0)
    k = 0
    for s in range(4, 17):
        boxes = n_win // s
        f = f32(math.sqrt((box_rss(s, boxes, 0) + box_rss(s, boxes, n_win - boxes * s)) / (2 * boxes * s)))
        if f > 0:
            lx, ly = f32(math.log(s)), f32(math.log(f))
            sx += lx; sy += ly; sxx += lx * lx; sxy += lx * ly; k += 1
    den = k * sxx - sx * sx
    return float((k * sxy - sx * sy) / den) if k >= 4 and den > 0 else None


def test_dfa() -> None:
    ref = central_dfa()
    rng = np.random.default_rng(7)
    worst = 0.0
    for trial in range(150):
        if trial % 3 == 0:
            rr = 800 + rng.normal(0, 40, 120)
        elif trial % 3 == 1:
            rr = 800 + np.cumsum(rng.normal(0, 8, 120))
        else:
            e, x = rng.normal(0, 20, 120), np.zeros(120)
            for i in range(1, 120):
                x[i] = 0.9 * x[i - 1] + e[i]
            rr = 450 + x
        rr = np.round(rr).astype(int)
        worst = max(worst, abs(ref(rr) - watch_dfa(rr.tolist())))
    check("dfa", worst < 1e-3, f"max |watch(float32) - central| = {worst:.6f} over 150 series")


# ---- AccelFeatures.window ------------------------------------------------------------------------
def test_enmo_mad() -> None:
    from pulso.signal.activity import enmo_mad_epochs
    rng = np.random.default_rng(1)
    xyz = np.round(rng.normal(0, 150, (25 * 60, 3)) + [0, 0, 1000]).astype(int)
    _, e_ref, m_ref = enmo_mad_epochs(0, 25.0, xyz)
    worst = 0.0
    for k in range(60):
        w = xyz[k * 25:(k + 1) * 25]
        vm = [f32(math.sqrt(int(a) ** 2 + int(b) ** 2 + int(c) ** 2)) for a, b, c in w]
        mean = f32(sum(vm)) / f32(len(vm))
        enmo = int(sum(max(v - f32(1000), f32(0)) for v in vm) / len(vm) + 0.5)
        mad = int(sum(abs(v - mean) for v in vm) / len(vm) + 0.5)
        worst = max(worst, abs(enmo - e_ref[k]), abs(mad - m_ref[k]))
    check("enmo_mad", worst <= 0.5 + 1e-6, f"max |watch(int mg) - central| = {worst:.3f} mg")


# ---- HrvLive.add (gate) + RMSSD ------------------------------------------------------------------
def watch_rmssd(stream: list[int]) -> float | None:
    med: list[int] = []
    prev_ok, prev, d2 = False, 0, []
    for rr in stream:
        ok = False
        if 300 <= rr <= 2000:
            m = sorted(med)[len(med) // 2] if len(med) >= 3 else 0
            ok = m == 0 or abs(rr - m) * 10 < m * 3
            med = (med + [rr])[-9:]
        if ok and prev_ok:
            d2 = (d2 + [(rr - prev) ** 2])[-60:]
        prev_ok, prev = ok, rr
    return math.sqrt(sum(d2) / len(d2)) if len(d2) >= 10 else None


def test_gate() -> None:
    rng = np.random.default_rng(11)
    errs = []
    for _ in range(200):
        clean = np.round(900 + 40 * np.sin(np.arange(200) * 2 * np.pi / 4.5) + rng.normal(0, 18, 200)).astype(int)
        truth = math.sqrt(np.mean(np.diff(clean[-61:]) ** 2))
        s = clean.copy()
        for i in range(1, 199):
            r = rng.random()
            if r < 0.05 / 3:
                s[i], s[i + 1] = int(s[i] * 0.65), int(s[i + 1] * 1.35)
            elif r < 0.10 / 3:
                s[i] = s[i] + s[i + 1]
            elif r < 0.05:
                s[i] = int(rng.integers(250, 2400))
        errs.append(abs(watch_rmssd(s.tolist()) - truth) / truth)
    med = float(np.median(errs))
    check("gate_rmssd", med < 0.05, f"5 % artifacts: median RMSSD error {med:.1%} (ungated is several 100 %)")


# ---- BatchBuilder.addRr + central reconstruction ---------------------------------------------------
def test_rr_anchor() -> None:
    rng = np.random.default_rng(3)
    tol, slack_ms = 1500, 400
    for period in (1, 4):
        n = 3000
        rr = np.clip(np.round(rng.normal(850, 60, n)), 400, 1600).astype(int)
        t_beat = 1_760_000_000_000 + np.cumsum(rr)
        t_cb = t_beat[0] + 500 + np.arange(0, (t_beat[-1] - t_beat[0]) // 1000, period) * 1000
        t_cb = t_cb + rng.integers(0, 200, t_cb.size)
        last, chunks, cur, bi = 0, [], None, 0
        for tc in t_cb:
            ivs, idx = [], []
            while bi < n and t_beat[bi] <= tc:
                ivs.append(int(rr[bi])); idx.append(bi); bi += 1
            if not ivs:
                continue
            adjacent, t_last = False, int(tc)
            if last > 0:
                exp = last + sum(ivs)
                sl = int(tc) - exp
                if -tol <= sl <= ivs[-1] + slack_ms:
                    adjacent, t_last = True, (int(tc) if sl < 0 else exp)
            if not adjacent or cur is None:
                cur = [t_last, [], []]
                chunks.append(cur)
            cur[1].extend(ivs); cur[2].extend(idx); cur[0] = t_last; last = t_last
        err = []
        for t_last, arr, idx in chunks:
            a = np.array(arr)
            ts = t_last - np.concatenate([np.cumsum(a[::-1])[::-1][1:], [0]])   # handler.py
            err.extend(np.abs(ts - t_beat[np.array(idx)]).tolist())
        p95 = float(np.percentile(err, 95))
        check(f"rr_anchor_p{period}", p95 < 100, f"period {period}s: beat-time error p95 {p95:.0f} ms, {len(chunks)} chunk(s)")


# ---- Governor.tick closed loop ---------------------------------------------------------------------
def test_governor() -> None:
    ring, min_slope, min_decide, hold, alpha, up, down, memory = 13, 600, 1800, 1200, 0.5, 1.2, 0.7, 10800
    for name, drains in (("light", [2.5, 1.8, 1.4, 1.0]), ("heavy", [4.5, 2.0, 1.6, 1.1])):
        budget, level, ema, last_change = 3.0, 0, None, 0
        t_r, b_r, lvl_d, lvl_t = [], [], [-1.0] * 4, [0] * 4
        true_bat, changes, hours = 90.4, 0, 8
        for k in range(hours * 12 + 1):
            now = k * 300
            if k:
                true_bat -= drains[level] * 300 / 3600
            bat = math.floor(true_bat)
            t_r, b_r = (t_r + [now])[-ring:], (b_r + [bat])[-ring:]
            dt = now - t_r[0]
            if len(t_r) >= 2 and dt >= min_slope:
                r = (b_r[0] - bat) * 3600 / dt
                ema = r if ema is None else ema + alpha * (r - ema)
            if ema is None or dt < min_decide or now - last_change < hold:
                continue
            new = level
            if ema > budget * up and level < 3:
                lvl_d[level], lvl_t[level] = ema, now
                new = level + 1
            elif ema < budget * down and level > 0:
                kn = lvl_d[level - 1]
                if not (kn >= 0 and now - lvl_t[level - 1] < memory and kn > budget * up):
                    new = level - 1
            if new != level:
                level, changes, last_change = new, changes + 1, now
                t_r, b_r = [now], [bat]
        avg = (90.4 - true_bat) / hours
        check(f"governor_{name}", avg <= budget * 1.05 and changes <= 6,
              f"avg drain {avg:.2f} %/h (budget 3.0), {changes} level change(s) in {hours} h")


# ---- wire shape vs central validator + ingest --------------------------------------------------------
def test_wire() -> None:
    try:
        from pulso import store
        from pulso.ingest.handler import ingest
        from pulso.ingest.protocol import Batch
    except Exception as e:  # pragma: no cover
        check("wire", False, f"central import failed: {e!r}")
        return
    t0 = 1760630400
    batch = {
        "v": 1, "d": "fr255-cap-a1b2c3", "q": 7, "src": "cap", "sid": f"c-{t0}",
        "h": {"stress": [[t0 - 300, 31]], "bb": [[t0 - 300, 62]], "spo2": [[t0 - 3600, 96]]},
        "rr": [[t0 * 1000 + 5123, [812, 820, 805]]],
        "hr1": [t0, [72, None, 73]],
        "acc": [[t0 * 1000 + 1000, t0 + 1, [3, 0, 12], [10, 4, 25]]],
        "bar": [t0, 10, [101325, None, 101320]],
        "ev": [[t0, "cap", "start:foco"], [t0, "cfg", "acc_max=100 p=1 hz=25 v=0.1.0"], [t0 + 40, "mark", "café"]],
        "bat": [t0 + 60, 64.0, None],
        "m": {"t": t0 + 60, "stress": 31, "resp": 14, "steps": 5120, "rhr": 52},
        "t": t0 + 61,
    }
    b = Batch.model_validate_json(json.dumps(batch, ensure_ascii=False))
    with tempfile.TemporaryDirectory() as d:
        conn = store.connect(os.path.join(d, "t.sqlite"))
        r1, r2 = ingest(conn, b), ingest(conn, b)
    n = r1["n"]
    ok = not r1["dup"] and r2["dup"] and n.get("rr") == 3 and n.get("acc_s") == 3 and n.get("pressure_pa") == 2
    check("wire", ok, f"ingest {n}, resend dup={r2['dup']}")


# ---- Capture.harvestHistory size bound ---------------------------------------------------------------
def test_history_size() -> None:
    """Worst case of one harvest (3 series x HIST_MAX_POINTS, float values printed with 6 decimals)
    alone in a fresh batch (harvestHistory seals the open batch first when it would not fit) must
    stay under the 6 KB POST limit and pass the central validator."""
    from pulso.ingest.protocol import Batch
    hist_max_points, t0 = 60, 1760630400
    h = {k: [[t0 + 180 * i, 100.0] for i in range(hist_max_points)] for k in ("stress", "bb", "spo2")}
    batch = {"v": 1, "d": "fr255-cap-a1b2c3", "q": 99999, "src": "cap", "sid": f"c-{t0}", "h": h,
             "bat": [t0, 100.0, 12.5], "m": {"t": t0, "stress": 100, "resp": 14, "steps": 25000, "rhr": 52},
             "t": t0}
    body = json.dumps(batch, separators=(",", ":")).replace("100.0", "100.000000").replace("12.5", "12.500000")
    Batch.model_validate_json(body)
    check("history_size", len(body) < 6144, f"worst-case harvest batch {len(body)} B (limit 6144)")


if __name__ == "__main__":
    for t in (test_dfa, test_enmo_mad, test_gate, test_rr_anchor, test_governor, test_wire, test_history_size):
        t()
    sys.exit(1 if FAILED else 0)
