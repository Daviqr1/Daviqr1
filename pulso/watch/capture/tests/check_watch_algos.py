"""Host-side checks for the capture app's on-watch algorithms.

These are hand transliterations of the Monkey C code (float32 where the watch uses Float), run
against the central's reference implementations and simple simulations. Keep them in sync with
source/HrvLive.mc, AccelFeatures.mc, BatchBuilder.mc, Governor.mc, BleLink.mc and Json.mc (for
BleLink: frame layout, HELLO and its proof, the blob MAC over q, the signed blob ack and its
attempt check, the stream type filter and the time base reset on HELLO, all against the bridge's
real decoder in central/pulso/bridge/frames.py and handle_blob in peripheral.py).

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


# ---- BleLink (frames) + Json (blob body) vs the bridge decoder ----------------------------------------
def watch_unit(t0_ms: int, sync_ms: int, xyz: list[tuple[int, int, int]], period: int, hint: int = 25,
               sid: int = 1) -> list[bytearray]:
    """BleLink.pushAccel: ACC_KEY + ACC_DELTA frames for one sensor callback (seq left at 0);
    above `hint` Hz, groups of k = ceil(rate / hint) samples are averaged (Monkey C integer division).
    Every ACC_KEY repeats the id of the SYNC it is timed against."""
    n = len(xyz)
    rate = (n + period // 2) // period
    k = (rate + hint - 1) // hint if rate > hint else 1
    n_out = (n + k - 1) // k
    hz = max(1, min(255, (n_out + period // 2) // period))

    def idiv(a: int, b: int) -> int:   # Monkey C truncates toward zero
        q = abs(a) // abs(b)
        return q if (a >= 0) == (b >= 0) else -q

    pts = []
    for j in range(n_out):
        grp = xyz[j * k: min(n, j * k + k)]
        pts.append(tuple(max(-32768, min(32767, idiv(sum(p[c] for p in grp), len(grp)))) for c in range(3)))
    dt = t0_ms - sync_ms
    unit: list[bytearray] = []
    d, m, prev = None, 0, (0, 0, 0)
    for i, (sx, sy, sz) in enumerate(pts):
        dx, dy, dz = sx - prev[0], sy - prev[1], sz - prev[2]
        if i > 0 and all(-127 <= v <= 127 for v in (dx, dy, dz)):
            if d is None:
                d, m = bytearray(18), 0
                d[1] = 0x03
            o = 3 + 3 * m
            d[o], d[o + 1], d[o + 2] = dx & 0xFF, dy & 0xFF, dz & 0xFF
            m += 1
            if m == 5:
                d[2] = 5
                unit.append(d)
                d = None
        else:
            if d is not None:
                d[2] = m
                unit.append(d[: 3 + 3 * m])
                d = None
            f = bytearray(13)
            f[1] = 0x02
            v = dt + (i * 1000) // hz
            f[2:5] = bytes((v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF))
            f[5] = hz
            for jj, c in enumerate((sx, sy, sz)):
                f[6 + 2 * jj], f[7 + 2 * jj] = c & 0xFF, (c >> 8) & 0xFF
            f[12] = sid
            unit.append(f)
        prev = (sx, sy, sz)
    if d is not None:
        d[2] = m
        unit.append(d[: 3 + 3 * m])
    return unit, pts


def watch_json(root, budget: int) -> str:
    """JsonJob.step(budget) called until done (explicit stack, resumable at any value)."""
    out: list[str] = []
    stack: list[list] = []

    def scalar(v) -> str:
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, int):
            return str(v)
        if isinstance(v, float):
            return watch_json_num(v)
        return json.dumps(v, ensure_ascii=False)

    def open_(v) -> None:
        if isinstance(v, list):
            out.append("["); stack.append([v, None, 0])
        elif isinstance(v, dict):
            out.append("{"); stack.append([v, list(v.keys()), 0])
        else:
            out.append(scalar(v))

    open_(root)
    steps = 0
    while stack:
        steps += 1
        b = budget
        while b > 0 and stack:
            c, keys, i = stack[-1]
            n = len(keys) if keys is not None else len(c)
            if i >= n:
                out.append("}" if keys is not None else "]"); stack.pop(); continue
            if i > 0:
                out.append(",")
            if keys is not None:
                out.append(json.dumps(str(keys[i]), ensure_ascii=False) + ":"); v = c[keys[i]]
            else:
                v = c[i]
            stack[-1][2] = i + 1
            open_(v)
            b -= 1
    return "".join(out)


def watch_sync(sync_ms: int, session: int, sid: int = 1) -> bytearray:
    f = bytearray(13)
    f[1] = 0x00
    s = sync_ms // 1000
    f[2:6] = s.to_bytes(4, "little")
    f[6:8] = (sync_ms - s * 1000).to_bytes(2, "little")
    f[8:12] = session.to_bytes(4, "little")
    f[12] = sid
    return f


def watch_key(token: str) -> bytes:
    """BleLink.keyOf: SHA-256(token)."""
    import hashlib
    return hashlib.sha256(token.encode()).digest()


def watch_hmac(key: bytes, *parts: bytes, n: int) -> bytes:
    """BleLink.hmac: HMAC-SHA256(key, a | b | c)[0..n)."""
    import hashlib
    import hmac
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()[:n]


def watch_same(a: bytes, o: int, b: bytes | None, n: int) -> bool:
    """BleLink.same."""
    if b is None or len(a) < o + n or len(b) < n:
        return False
    d = 0
    for i in range(n):
        d |= a[o + i] ^ b[i]
    return d == 0


def watch_hello(token: str, nonce: bytes) -> tuple[bytearray, bytes]:
    """BleLink.sendHello: [seq, 0x0B, SHA-256(key)[0..4), nonce[12]] and the proof it expects."""
    import hashlib
    key = watch_key(token)
    f = bytearray(18)
    f[1] = 0x0B
    f[2:6] = hashlib.sha256(key).digest()[:4]
    f[6:18] = nonce
    return f, watch_hmac(key, b"pulso-hello", nonce, n=16)


def watch_hello_answer(v: bytes, proof: bytes) -> str:
    """BleLink.helloAnswer: READY, WAIT (bridge does not know the token) or rescan elsewhere."""
    if watch_same(v, 3, proof, 16):
        return "ready"
    return "wait" if len(v) < 19 else "rescan"


def i32(v: int) -> int:
    v &= 0xFFFFFFFF
    return v - (1 << 32) if v >= 1 << 31 else v


def watch_ack_status(v: bytes, blob_q: int, attempt: int, key: bytes, nonce: bytes) -> int:
    """BleLink.onCharacteristicChanged + ackStatus: the status; -1 ignored (another batch or
    attempt), -2 ignored and the link re-proven (our attempt, MAC does not check)."""
    if not (len(v) >= 17 and v[2] == 0xB1):
        return -1
    q = i32(v[3] | (v[4] << 8) | (v[5] << 16) | (v[6] << 24))
    if q != i32(blob_q) or v[7] != attempt:
        return -1
    mac = watch_hmac(key, b"pulso-ack", nonce, bytes(v[3:9]), n=8)
    return v[8] if watch_same(v, 9, mac, 8) else -2


def watch_blob(q: int, body: bytes, token: str, attempt: int = 1) -> list[bytearray]:
    """BleLink.sendBlob/nextBlobFrame; MAC = HMAC-SHA256(SHA-256(token), "pulso-blob" | q4 | body)[:16]
    (q4 = put32(q), so a negative Number q signs the same 4 bytes as the frame carries)."""
    q4 = (q & 0xFFFFFFFF).to_bytes(4, "little")
    mac = watch_hmac(watch_key(token), b"pulso-blob", q4, body, n=16)
    out = [bytearray(b"\0\x08") + q4 + bytes((attempt,))]
    for o in range(0, len(body), 18):
        out.append(bytearray(b"\0\x09") + body[o:o + 18])
    out.append(bytearray(b"\0\x0a") + len(body).to_bytes(2, "little") + mac)
    return out


def _decode(frames: list[bytearray]) -> list:
    from pulso.bridge import frames as FR
    dec = FR.Decoder()
    for f in stamp(frames):
        dec.feed(f)
    return dec.take_events()


def stamp(frames: list[bytearray], start: int = 0) -> list[bytes]:
    """BleLink.write: seq at transmission time."""
    out = []
    for i, f in enumerate(frames):
        f = bytearray(f)
        f[0] = (start + i) & 0xFF
        assert len(f) <= 20
        out.append(bytes(f))
    return out


def watch_json_num(d: float) -> str:
    """Json.num: "%.4f", trailing zeros and dot stripped."""
    s = "%.4f" % d
    s = s.rstrip("0").rstrip(".") if "." in s else s
    return "0" if s in ("", "-") else s


def test_ble_frames() -> None:
    import re
    from pulso.bridge import frames as FR
    from pulso.bridge import peripheral as P
    from pulso.ingest import auth
    from pulso import store

    src = (Path(__file__).resolve().parents[1] / "source" / "BleLink.mc").read_text()
    consts = {k: int(v, 16) for k, v in re.findall(r"const ([A-Z_]+) = (0x[0-9A-Fa-f]+);", src)}
    want = {"T_SYNC": FR.SYNC, "T_ACC_KEY": FR.ACC_KEY, "T_ACC_DELTA": FR.ACC_DELTA, "T_BAT": FR.BAT,
            "T_BLOB_START": FR.BLOB_START, "T_BLOB_DATA": FR.BLOB_DATA, "T_BLOB_END": FR.BLOB_END,
            "T_HELLO": FR.HELLO, "BLOB_ACK": FR.BLOB_ACK, "HELLO_ACK": FR.HELLO_ACK}
    same_types = all(consts.get(k) == v for k, v in want.items())
    # the stream filter: every type the watch writes is accepted, none is a reserved (dropped) one
    sent = {v for k, v in consts.items() if k.startswith("T_")}
    filt = sent == {want[k] for k in want if k.startswith("T_")} and not sent & FR.RESERVED
    labels = set(re.findall(r'Json\.utf8\("(pulso-[a-z]+)"\)', src)) == {"pulso-hello", "pulso-ack", "pulso-blob"}
    # sendBlob signs label | q4 | json, q4 being put32(q): what watch_blob transliterates
    blob_mac_src = bool(re.search(r'put32\(q4, 0, q\);\s*var mac = \(_key != null\) \? hmac\(_key as ByteArray, '
                                  r'Json\.utf8\("pulso-blob"\), q4, json, 16\)', src))
    uuids = set(re.findall(r"6c1e000[123]-5b7a-4d6e-9f2a-50554c534f31", src))
    same_uuids = uuids == {P.SERVICE_UUID, P.DATA_UUID, P.CTRL_UUID}

    rng = np.random.default_rng(3)
    t0 = 1760630400000
    sync_ms = t0 - 10000
    xyz, p = [], (0, 0, 1000)
    for i in range(50):   # two 1-s callbacks at 25 Hz, with one jump that forces a keyframe
        p = (p[0] + int(rng.integers(-60, 60)), p[1] + int(rng.integers(-60, 60)), p[2] + (300 if i == 37 else 0))
        xyz.append(p)
    stream = [watch_sync(sync_ms, 1760630400, sid=7)] + watch_unit(t0, sync_ms, xyz[:25], 1, sid=7)[0] \
        + watch_unit(t0 + 1000, sync_ms, xyz[25:], 1, sid=7)[0]
    bat = bytearray(b"\0\x07") + bytes((64, 23))
    tok = "pulso_dev_" + "y" * 43
    key = watch_key(tok)
    batch = {"v": 1, "d": "fr255-cap-a1b2c3", "q": 11, "src": "cap", "t": 1760630461,
             "rr": [[t0 + 900, [812, 820]]], "bat": [1760630460, float(watch_json_num(63.5)), None],
             "ev": [[1760630400, "mark", "reunião começou"]]}
    body = watch_json(batch, budget=3).encode()
    ok_job = json.loads(body) == batch and watch_json(batch, budget=1) == watch_json(batch, budget=10_000)

    # handshake: watch HELLO -> real decoder -> bridge answer -> watch check
    nonce = bytes(rng.integers(0, 256, 12).tolist())
    hello, proof = watch_hello(tok, nonce)
    dec = FR.Decoder()
    dec.feed(stamp([hello])[0])
    ev = dec.take_events()
    bkey = FR.match_key([FR.blob_key("pulso_dev_other"), FR.blob_key(tok)], ev[0].key_id) if ev else None
    answer = FR.hello_ack(0, 25, bkey, ev[0].nonce) if bkey else b""
    stranger = FR.hello_ack(0, 25, None, nonce)
    fake = FR.hello_ack(0, 25, FR.blob_key("pulso_dev_other"), nonce)
    ok_hello = (bkey == key and len(hello) == 18 and len(answer) == 19
                and watch_hello_answer(answer, proof) == "ready" and watch_hello_answer(stranger, proof) == "wait"
                and watch_hello_answer(fake, proof) == "rescan")

    # one abandoned attempt (F1), then BleLink.pump alternating blob / stream frames, seq at send time
    abandoned = watch_blob(11, body, tok, attempt=1)[:6]
    blob = watch_blob(11, body, tok, attempt=2)
    order, si, bi, turn = list(abandoned), 0, 0, False
    allstream = stream + [bat]
    while si < len(allstream) or bi < len(blob):
        if bi < len(blob) and (turn or si >= len(allstream)):
            order.append(blob[bi]); bi += 1; turn = False
        else:
            order.append(allstream[si]); si += 1; turn = True
    for f in stamp(order, start=1):
        dec.feed(f)
    events = dec.take_events()
    out = dec.take()
    samples = [s for _, _, ss in out.accel for s in ss]
    t_first = out.accel[0][0] if out.accel else None
    ok_stream = samples == xyz and t_first == t0 and out.battery == (64, 2.3) and out.lost_frames == 0
    with tempfile.TemporaryDirectory() as d:
        db = os.path.join(d, "t.sqlite")
        conn = store.connect(db)
        conn.execute("INSERT INTO device_tokens (token_sha256, label, created_ms) VALUES (?, ?, 0)", (auth._digest(tok), "fr255"))
        conn.commit()
        blobs = [e for e in events if isinstance(e, FR.Blob)]
        sink = P.local_sink(db)
        st = P.handle_blob(sink, blobs[0].q, blobs[0].payload, blobs[0].mac, log=lambda m: None) if blobs else -1
        n_rr = conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0]
        # SEC-1: someone else on DATA replays this genuine blob (sniffed) under the q/attempt of the
        # next batch in flight (12, 4): the MAC covers q, so the bridge must not sign "rejected"
        relabel = watch_blob(11, body, tok, attempt=4)
        relabel[0][2:6] = (12).to_bytes(4, "little")
        dec5 = FR.Decoder()
        for f in stamp(relabel):
            dec5.feed(f)
        rb = [e for e in dec5.take_events() if isinstance(e, FR.Blob)]
        st_relabel = P.handle_blob(sink, rb[0].q, rb[0].payload, rb[0].mac, log=lambda m: None) if rb else -1
        # and content the watch signed for 12 that names 11 (never sent by BleTransport) is no
        # verdict on 12 either
        other = [e for e in _decode(watch_blob(12, body, tok)) if isinstance(e, FR.Blob)]
        st_other = P.handle_blob(sink, other[0].q, other[0].payload, other[0].mac, log=lambda m: None) if other else -1
        same_rr = conn.execute("SELECT COUNT(*) FROM rr_intervals").fetchone()[0] == n_rr
    ok_blob = [type(e) for e in events] == [FR.Blob] and blobs[0].attempt == 2 and st == FR.BLOB_OK
    ok_relabel = (blob_mac_src and st_relabel == FR.BLOB_BAD_MAC and st_other == FR.BLOB_RETRY and same_rr
                  and watch_ack_status(FR.blob_ack(0, 25, 12, 4, st_relabel, key, nonce), 12, 4, key, nonce)
                  == FR.BLOB_BAD_MAC)
    # the bridge's signed ack, as the watch reads it: only for this nonce, batch, attempt and key
    ack = FR.blob_ack(0, 25, 11, 2, st, bkey or b"", nonce)
    ok_ack = (watch_ack_status(ack, 11, 2, key, nonce) == FR.BLOB_OK
              and watch_ack_status(ack, 11, 1, key, nonce) == -1
              and watch_ack_status(ack, 12, 2, key, nonce) == -1
              and watch_ack_status(ack, 11, 2, key, bytes(12)) == -2
              and watch_ack_status(FR.blob_ack(0, 25, 11, 2, 0, FR.blob_key("x"), nonce), 11, 2, key, nonce) == -2
              and watch_ack_status(ack[:8] + bytes((1,)) + ack[9:], 11, 2, key, nonce) == -2
              and watch_ack_status(FR.blob_ack(0, 25, 0x80000001, 2, 3, key, nonce), 0x80000001, 2, key, nonce) == 3
              and len(ack) == 17)

    # W3: the next SYNC is lost on the air; the chunk timed against it is dropped, never misdated
    lost_sync = [watch_sync(t0, 1760630400, sid=8)]
    later = watch_unit(t0 + 10000, t0, [(5, 5, 900)] * 25, 1, sid=8)[0]
    for f in stamp(lost_sync + later, start=200)[1:]:          # seq gap where the SYNC was
        dec.feed(f)
    o3 = dec.take()
    ok_sync = o3.accel == [] and o3.unsynced == 1 and o3.lost_frames >= 1
    # ... nor after an app restart: the new run's sid counter starts over (its first SYNC may carry
    # the id of the old run's last one); HELLO makes the decoder forget the old time base
    dec.feed(stamp([watch_sync(t0 + 60000, 1760630999, sid=9)], start=220)[0])
    hello2, _ = watch_hello(tok, bytes(12))
    restart = [hello2] + watch_unit(t0 + 3 * 3600000, t0 + 3 * 3600000 - 10000, [(7, 7, 700)] * 25, 1, sid=9)[0]
    for f in stamp(restart, start=0)[:1] + stamp(restart, start=1)[1:]:   # frame 1 (the new SYNC) lost
        dec.feed(f)
    o5 = dec.take()
    ok_sync = ok_sync and o5.accel == [] and o5.unsynced == 1

    # frames the watch never sends (RR, FEAT, HR, MARK) are refused on the open stream
    inj = [bytearray(b"\0\x01") + bytes((0, 0, 0, 1)) + (800).to_bytes(2, "little"),
           bytearray(b"\0\x04") + bytes((0, 0, 0, 1, 1, 0, 1, 0)),
           bytearray(b"\0\x05") + bytes((0, 0, 0, 180)), bytearray(b"\0\x06") + bytes((0, 0, 0, 1))]
    dec4 = FR.Decoder()
    for f in stamp([watch_sync(sync_ms, 1, sid=1)] + inj):
        dec4.feed(f)
    o4 = dec4.take()
    b4 = FR.to_batch(o4, "fr255-ble", 1, t0 // 1000)
    ok_filter = filt and o4.ignored == 4 and not {"rr", "hr1", "ev", "acc"} & set(b4)

    # MAX mode at 100 Hz: averaged down to the bridge hint (25 Hz) before framing
    rng2 = np.random.default_rng(5)
    raw100 = [(int(v[0]), int(v[1]), int(v[2])) for v in rng2.integers(-900, 900, size=(100, 3))]
    u100, pts25 = watch_unit(t0, sync_ms, raw100, 1)
    dec2 = FR.Decoder()
    for f in stamp([watch_sync(sync_ms, 1)] + u100):
        dec2.feed(f)
    o2 = dec2.take()
    got25 = [x for _, _, ss in o2.accel for x in ss]
    ok_avg = (len(pts25) == 25 and got25 == pts25 and all(hz == 25.0 for _, hz, _ in o2.accel)
              and pts25[0] == tuple(int(np.trunc(np.mean([p[c] for p in raw100[:4]]))) for c in range(3)))
    nums = [watch_json_num(v) for v in (64.0, 63.5, 0.1, -0.00001, 12.34567)]
    ok_num = nums == ["64", "63.5", "0.1", "-0", "12.3457"] and all(json.loads(x) is not None for x in nums)
    check("ble_frames", same_types and same_uuids and labels and ok_stream and ok_blob and ok_num and ok_job
          and ok_avg and ok_hello and ok_ack and ok_sync and ok_filter and ok_relabel,
          f"types={same_types} uuids={same_uuids} labels={labels} hello={ok_hello} stream={ok_stream} "
          f"({len(samples)} samples, {len(order)} frames) blob_after_abandoned={ok_blob} ack_mac={ok_ack} "
          f"relabel={ok_relabel} "
          f"lost_sync={ok_sync} filter={ok_filter} avg100to25={ok_avg} ({len(u100)} frames) json_job={ok_job} "
          f"json_num={nums}")


if __name__ == "__main__":
    for t in (test_dfa, test_enmo_mad, test_gate, test_rr_anchor, test_governor, test_wire, test_history_size,
              test_ble_frames):
        t()
    sys.exit(1 if FAILED else 0)
