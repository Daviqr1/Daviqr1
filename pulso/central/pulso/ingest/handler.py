"""Batch -> store. Pure function over a sqlite connection, so the HTTP app, the BLE bridge and
tests all share exactly the same ingest semantics."""

from __future__ import annotations

import base64
import sqlite3

import numpy as np

from pulso import metrics as M
from pulso import store
from pulso.ingest.protocol import HISTORY_KEYS, Batch
from pulso.store import Event

SRC = {"face": M.SRC_CIQ_FACE, "cap": M.SRC_CIQ_CAPTURE, "ble": M.SRC_BLE, "strap": M.SRC_STRAP}

# ActivityMonitor snapshot keys -> canonical metrics
SNAPSHOT_KEYS = {"stress": "stress_now", "resp": M.RESP_RATE, "rhr": M.RESTING_HR}


def _valid(metric: str, v: float | None) -> bool:
    if v is None:
        return False
    lo, hi = {
        "hr": (25, 230), "stress_garmin": (0, 100), "body_battery": (0, 100), "spo2": (70, 100),
        "temp_c": (-20, 60), "pressure_pa": (30_000, 110_000), "altitude_m": (-500, 9_000),
        "resp_rate": (3, 70), "stress_now": (0, 100), "resting_hr": (25, 120),
    }.get(metric, (-1e12, 1e12))
    return lo <= v <= hi


def ingest(conn: sqlite3.Connection, b: Batch, now_ms: int | None = None) -> dict:
    """Store one batch. Returns {"dup": bool, "n": {...counts}}. Idempotent on (device, seq)."""
    now_ms = now_ms or store.now_ms()
    batch_id = f"{b.d}:{b.q}"
    src = SRC[b.src]
    counts: dict[str, int] = {}
    with store.transaction(conn):
        if conn.execute("SELECT 1 FROM ingest_batches WHERE batch_id = ?", (batch_id,)).fetchone():
            return {"dup": True, "n": {}}

        for key, pts in b.h.items():
            metric = HISTORY_KEYS.get(key)
            if metric is None:
                continue
            rows = [(int(t) * 1000, float(v)) for t, v in pts if _valid(metric, v)]
            counts[metric] = store.insert_samples(conn, metric, src, rows)

        if b.m:
            t = int(b.m.get("t") or b.t or now_ms // 1000) * 1000
            for k, metric in SNAPSHOT_KEYS.items():
                v = b.m.get(k)
                if v is not None and _valid(metric, float(v)):
                    counts[metric] = counts.get(metric, 0) + store.insert_samples(conn, metric, src, [(t, float(v))])
            if b.m.get("steps") is not None:
                # cumulative daily counter; the analysis differentiates it
                store.insert_samples(conn, "steps_day_total", src, [(t, float(b.m["steps"]))])

        n_rr = 0
        for t_last, arr in b.rr:
            rr = np.asarray(arr, dtype=np.int64)
            if rr.size == 0:
                continue
            # beat i closes at t_last - sum(rr[i+1:])
            tail = np.concatenate([np.cumsum(rr[::-1])[::-1][1:], [0]])
            ts = int(t_last) - tail
            n_rr += store.insert_rr(conn, src, zip(ts.tolist(), rr.tolist()), session_id=b.sid)
        if n_rr:
            counts["rr"] = n_rr

        if b.hr1:
            t0, vals = b.hr1
            rows = [((t0 + i) * 1000, float(v)) for i, v in enumerate(vals) if v is not None and _valid("hr", float(v))]
            counts["hr"] = counts.get("hr", 0) + store.insert_samples(conn, M.HR, src, rows)

        for t0_ms, _epoch_s, enmo, mad in b.acc:
            store.insert_samples(conn, M.ENMO, src, [(t0_ms + 1000 * i, float(v)) for i, v in enumerate(enmo) if v is not None])
            store.insert_samples(conn, M.MAD, src, [(t0_ms + 1000 * i, float(v)) for i, v in enumerate(mad) if v is not None])
            counts["acc_s"] = counts.get("acc_s", 0) + len(enmo)

        for t0_ms, hz, bx, by, bz in b.raw:
            x, y, z = (np.frombuffer(base64.b64decode(s), dtype="<i2") for s in (bx, by, bz))
            n = min(x.size, y.size, z.size)
            counts["raw"] = counts.get("raw", 0) + store.insert_motion_chunk(
                conn, src, "accel", int(t0_ms), float(hz), x[:n], y[:n], z[:n], session_id=b.sid)

        if b.bar:
            t0, step, vals = b.bar
            rows = [((t0 + i * step) * 1000, float(v)) for i, v in enumerate(vals) if v is not None and _valid("pressure_pa", float(v))]
            counts["pressure_pa"] = counts.get("pressure_pa", 0) + store.insert_samples(conn, M.PRESSURE_PA, src, rows)

        # id keyed on (d, q), the batch's idempotency key, not on t: the capture app seals a 'stop'
        # and opens the next session's 'start' in the same second, both at index 0 of their batch
        for i, (t, kind, label) in enumerate(b.ev):
            store.upsert_event(conn, Event(
                id=f"mark:{b.d}:{b.q}:{i}", kind="note", start_ms=int(t) * 1000, end_ms=int(t) * 1000,
                title=label[:120], source=f"watch:{b.src}", attrs={"kind": kind[:32]},
            ))

        if b.bat:
            t, pct, days = b.bat
            store.insert_samples(conn, "battery_pct", src, [(int(t) * 1000, float(pct))])
            if days is not None:
                store.insert_samples(conn, "battery_days", src, [(int(t) * 1000, float(days))])

        if b.t:
            store.insert_samples(conn, "clock_skew_s", src, [(now_ms, now_ms / 1000.0 - b.t)])

        conn.execute(
            "INSERT INTO ingest_batches (batch_id, device, received_ms, n_items) VALUES (?, ?, ?, ?)",
            (batch_id, b.d, now_ms, int(sum(counts.values()))),
        )
    return {"dup": False, "n": counts}
