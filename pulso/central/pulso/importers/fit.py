"""FIT files from the watch (USB: GARMIN/Monitor, GARMIN/Sleep, GARMIN/Metrics, GARMIN/HRVStatus,
GARMIN/Activity...) or from a Garmin Connect "export original" — decoded with Garmin's official
FIT SDK (`garmin-fit-sdk`).

What gets imported (message -> metric):
    monitoring.heart_rate (timestamp_16 expanded)         -> hr                (fit:monitor)
    monitoring.cycles per activity type (cumulative)      -> steps_pm
    monitoring.intensity / active_calories                -> intensity, active_kcal_pm
    stress_level                                           -> stress_garmin (negative codes dropped)
    respiration_rate                                       -> resp_rate
    spo2_data                                              -> spo2
    hrv_value (overnight 5-min RMSSD)                      -> hrv_garmin
    hrv_status_summary.last_night_average                  -> hrv_garmin_night_avg (+ status in attrs)
    sleep_level                                            -> sleep_epochs
    hsa_* (Health SDK style messages)                      -> same metrics
    record.heart_rate (activities, 1 s)                    -> hr (fit:activity)
    hrv (beat-to-beat logging in activities) / beat_intervals -> rr_intervals
    developer field "rr" from Pulso Captura                -> rr_intervals
    accelerometer_data (SensorLogger raw IMU)              -> motion_chunks
    session                                                -> 'workout' / 'session' events
Message order matters (timestamp_16, hrv after record), so decoding uses the SDK's ordered
message listener instead of the grouped dict.
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pulso import metrics as M
from pulso import store
from pulso.store import Event

FIT_EPOCH_UNIX = 631065600
# monitoring 'cycles' for walking/running after scale: Garmin's profile documents cycles_16 as
# "2 * cycles (steps)", i.e. one cycle = two steps. Verified against Garmin Connect daily totals
# in the docs/relogio.md calibration step; change here if your totals disagree.
STEPS_PER_CYCLE = 2.0
STEP_ACTIVITY_TYPES = {"walking", "running", "generic"}


def _ms(v) -> int | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return int(v.timestamp() * 1000)
    if isinstance(v, (int, float)):
        return int((v + FIT_EPOCH_UNIX) * 1000)
    return None


class _Collector:
    def __init__(self, source: str):
        self.source = source
        self.samples: dict[str, list[tuple[int, float]]] = defaultdict(list)
        self.rr: list[tuple[int, int]] = []
        self.sleep: list[tuple[int, str]] = []
        self.events: list[Event] = []
        self.motion: list[tuple[int, float, list, list, list]] = []
        self.last_ts: int | None = None           # FIT-epoch seconds of the last full timestamp
        self.cycles_prev: dict[str, tuple[int, float]] = {}
        self.hrv_clock_ms: int | None = None      # running beat clock for 'hrv' messages
        self.last_record_ms: int | None = None
        self.dev_names: dict = {}
        self.file_type = ""
        self.n_msgs = 0

    # ---------------------------------------------------------------- per message
    def on(self, num: int, m: dict) -> None:
        self.n_msgs += 1
        name = _NAMES.get(num)
        ts_full = m.get("timestamp")
        if isinstance(ts_full, datetime):
            self.last_ts = int(ts_full.timestamp()) - FIT_EPOCH_UNIX
        handler = getattr(self, f"_m_{name}", None) if name else None
        if handler:
            handler(m)
        if "developer_fields" in m and m["developer_fields"]:
            self._developer(m)

    def _t(self, m: dict) -> int | None:
        t = _ms(m.get("timestamp"))
        if t is not None:
            return t
        t16 = m.get("timestamp_16")
        if t16 is not None and self.last_ts is not None:
            full = self.last_ts + ((int(t16) - (self.last_ts & 0xFFFF)) & 0xFFFF)
            self.last_ts = full
            return (full + FIT_EPOCH_UNIX) * 1000
        return None

    def _m_file_id(self, m):
        self.file_type = str(m.get("type") or "")

    def _m_monitoring(self, m):
        t = self._t(m)
        if t is None:
            return
        hr = m.get("heart_rate")
        if hr:
            self.samples[M.HR].append((t, float(hr)))
        if m.get("intensity") is not None:
            self.samples[M.INTENSITY].append((t, float(m["intensity"])))
        at = str(m.get("activity_type") or "")
        cyc = m.get("cycles")
        if cyc is not None and at in STEP_ACTIVITY_TYPES:
            prev = self.cycles_prev.get(at)
            if prev is not None and cyc >= prev[1] and 0 < t - prev[0] <= 3600_000:
                steps = (cyc - prev[1]) * STEPS_PER_CYCLE
                minutes = (t - prev[0]) / 60_000
                if minutes > 0:
                    self.samples[M.STEPS_PM].append((t, steps / minutes))
            self.cycles_prev[at] = (t, float(cyc))
        if m.get("active_calories") is not None:
            self.samples["active_kcal_cum"].append((t, float(m["active_calories"])))

    def _m_stress_level(self, m):
        t = _ms(m.get("stress_level_time")) or self._t(m)
        v = m.get("stress_level_value")
        if t is not None and v is not None and 0 <= v <= 100:
            self.samples[M.STRESS_GARMIN].append((t, float(v)))

    def _m_respiration_rate(self, m):
        t = self._t(m)
        v = m.get("respiration_rate")
        if t is not None and v is not None and 0 < v < 80:
            self.samples[M.RESP_RATE].append((t, float(v)))

    def _m_spo2_data(self, m):
        t = self._t(m)
        v = m.get("reading_spo2")
        if t is not None and v and 50 <= v <= 100:
            self.samples[M.SPO2].append((t, float(v)))

    def _m_hrv_value(self, m):
        t = self._t(m)
        v = m.get("value")
        if t is not None and v and 0 < v < 400:
            self.samples[M.HRV_GARMIN].append((t, float(v)))

    def _m_hrv_status_summary(self, m):
        t = self._t(m)
        v = m.get("last_night_average")
        if t is not None and v and 0 < v < 400:
            self.samples[M.HRV_GARMIN_NIGHT].append((t, float(v)))
            self.events.append(Event(f"hrvstatus:{t}", "note", t, t, f"HRV status: {m.get('status')}", self.source,
                                     {"kind": "hrv_status", "status": str(m.get("status")),
                                      "weekly_average": m.get("weekly_average"),
                                      "baseline": [m.get("baseline_balanced_lower"), m.get("baseline_balanced_upper")]}))

    def _m_sleep_level(self, m):
        t = self._t(m)
        lvl = m.get("sleep_level")
        if t is not None and lvl is not None:
            self.sleep.append((t, str(lvl)))

    def _hsa(self, m, field: str, metric: str, lo: float, hi: float):
        t = self._t(m)
        v = m.get(field)
        if t is None or v is None:
            return
        vals = v if isinstance(v, list) else [v]
        step = int(m.get("processing_interval") or 60) * 1000 // max(1, len(vals))
        for i, x in enumerate(vals):
            if x is not None and lo <= x <= hi:
                self.samples[metric].append((t + i * step, float(x)))

    def _m_hsa_heart_rate_data(self, m):
        self._hsa(m, "heart_rate", M.HR, 25, 230)

    def _m_hsa_stress_data(self, m):
        self._hsa(m, "stress_level", M.STRESS_GARMIN, 0, 100)

    def _m_hsa_respiration_data(self, m):
        self._hsa(m, "respiration_rate", M.RESP_RATE, 1, 80)

    def _m_hsa_body_battery_data(self, m):
        self._hsa(m, "level", M.BODY_BATTERY, 0, 100)

    def _m_hsa_spo2_data(self, m):
        self._hsa(m, "reading_spo2", M.SPO2, 50, 100)

    def _m_record(self, m):
        t = self._t(m)
        if t is None:
            return
        self.last_record_ms = t
        if self.hrv_clock_ms is None:
            self.hrv_clock_ms = t
        hr = m.get("heart_rate")
        if hr:
            self.samples[M.HR].append((t, float(hr)))

    def _m_hrv(self, m):
        # beat-to-beat logging: arrays of RR in seconds (scaled), 0xFFFF-ish invalids come as None
        times = m.get("time")
        if times is None:
            return
        vals = times if isinstance(times, list) else [times]
        if self.hrv_clock_ms is None:
            self.hrv_clock_ms = self.last_record_ms or ((self.last_ts or 0) + FIT_EPOCH_UNIX) * 1000
        for v in vals:
            if v is None or not (0.25 <= float(v) <= 2.5):
                continue
            rr = int(round(float(v) * 1000))
            self.hrv_clock_ms += rr
            # the beat clock must not drift behind the record clock by more than a few seconds
            if self.last_record_ms is not None and self.hrv_clock_ms < self.last_record_ms - 5000:
                self.hrv_clock_ms = self.last_record_ms
            self.rr.append((self.hrv_clock_ms, rr))

    def _m_beat_intervals(self, m):
        t = self._t(m)
        if t is None:
            return
        t += int(m.get("timestamp_ms") or 0)
        vals = m.get("time")
        vals = vals if isinstance(vals, list) else [vals]
        good = [int(v) for v in vals if v is not None and 250 <= v <= 2500]
        tail = sum(good)
        for v in good:  # timestamp marks the last beat of the message
            tail -= v
            self.rr.append((t - tail, v))

    def _m_accelerometer_data(self, m):
        t = self._t(m)
        if t is None:
            return
        t += int(m.get("timestamp_ms") or 0)
        xs, ys, zs = (m.get(f"compressed_calibrated_accel_{a}") for a in "xyz")
        offs = m.get("sample_time_offset")
        if not (isinstance(xs, list) and isinstance(ys, list) and isinstance(zs, list)) or not xs:
            return
        n = min(len(xs), len(ys), len(zs))
        hz = 25.0
        if isinstance(offs, list) and len(offs) >= 2 and offs[1] and offs[1] > offs[0]:
            hz = 1000.0 / (offs[1] - offs[0])
        self.motion.append((t, hz, [int(v) for v in xs[:n]], [int(v) for v in ys[:n]], [int(v) for v in zs[:n]]))

    def _m_session(self, m):
        s = _ms(m.get("start_time"))
        dur = m.get("total_elapsed_time")
        if s is None or not dur:
            return
        sport = str(m.get("sport") or "")
        kind = "workout" if sport not in {"generic", "training", ""} else "session"
        self.events.append(Event(f"fit:session:{s}", kind, s, s + int(float(dur) * 1000), sport or "sessão",
                                 self.source, {"sport": sport, "sub_sport": str(m.get("sub_sport") or ""),
                                               "avg_hr": m.get("avg_heart_rate"), "max_hr": m.get("max_heart_rate"),
                                               "training_effect": m.get("total_training_effect"),
                                               "calories": m.get("total_calories")}))

    def _developer(self, m):
        t = self._t(m) if m.get("timestamp") is None else _ms(m.get("timestamp"))
        for key, v in m["developer_fields"].items():
            name = str(self.dev_names.get(key, "")).lower()
            if name in {"rr", "pulso_rr", "rr_ms"} and t is not None:
                vals = [int(x) for x in (v if isinstance(v, list) else [v]) if x and 250 <= x <= 2500]
                tail = sum(vals)
                for x in vals:
                    tail -= x
                    self.rr.append((t - tail, x))


_NAMES: dict[int, str] = {}


def _names() -> dict[int, str]:
    if not _NAMES:
        from garmin_fit_sdk import Profile

        for num, mesg in Profile["messages"].items():
            _NAMES[int(num)] = mesg["name"]
    return _NAMES


def decode_file(path: str | Path, source_prefix: str = "fit") -> _Collector:
    from garmin_fit_sdk import Decoder, Stream

    _names()
    p = Path(path)
    col = _Collector(f"{source_prefix}:{p.parent.name.lower() or 'file'}")
    stream = Stream.from_file(str(p))
    dec = Decoder(stream)

    def on_desc(key, desc):
        fd = desc.get("field_description_mesg") or desc
        col.dev_names[key] = fd.get("field_name", "")

    _messages, errors = dec.read(mesg_listener=col.on, field_description_listener=on_desc,
                                 merge_heart_rates=False)
    if errors:
        col.events.append(Event(f"fiterr:{p.name}", "note", 0, 0, f"erros FIT: {errors[:3]}", col.source, {"kind": "import_error"}))
    if col.file_type:
        col.source = f"{source_prefix}:{col.file_type}"
    return col


def _store(conn: sqlite3.Connection, col: _Collector) -> dict[str, int]:
    n: dict[str, int] = {}
    src = col.source
    with store.transaction(conn):
        for metric, rows in col.samples.items():
            if metric == "active_kcal_cum":
                continue
            n[metric] = store.insert_samples(conn, metric, src, rows)
        if col.rr:
            n["rr"] = store.insert_rr(conn, src, col.rr)
        if col.sleep:
            col.sleep.sort()
            rows = []
            for i, (t, lvl) in enumerate(col.sleep):
                nxt = col.sleep[i + 1][0] if i + 1 < len(col.sleep) else t + 60_000
                dur = max(1, min(600, (nxt - t) // 1000))
                rows.append((src, t, int(dur), lvl))
            conn.executemany("INSERT OR IGNORE INTO sleep_epochs (source, ts_ms, duration_s, stage) VALUES (?, ?, ?, ?)", rows)
            n["sleep_epochs"] = len(rows)
        for ev in col.events:
            if ev.start_ms:
                store.upsert_event(conn, ev)
        for t0, hz, x, y, z in col.motion:
            n["motion_chunks"] = n.get("motion_chunks", 0) + store.insert_motion_chunk(conn, src, "accel", t0, hz, x, y, z)
    return n


def import_paths(conn: sqlite3.Connection, paths: list[str], tz: str | None = None) -> dict:
    """Import .fit files or directories (recursively). Returns per-file counts."""
    files: list[Path] = []
    for p in paths:
        q = Path(p)
        if q.is_dir():
            files += sorted(x for x in q.rglob("*") if x.suffix.lower() == ".fit")
        elif q.suffix.lower() == ".fit":
            files.append(q)
    out = {}
    seen = set()
    for f in files:
        digest = hashlib.sha1(f.read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        if conn.execute("SELECT 1 FROM artefacts WHERE kind = 'fit_imported' AND key = ?", (digest,)).fetchone():
            out[str(f)] = "já importado"
            continue
        try:
            col = decode_file(f)
            counts = _store(conn, col)
        except Exception as e:  # one corrupt file must not stop a folder import
            out[str(f)] = f"erro: {e!r}"
            continue
        store.put_artefact(conn, "fit_imported", digest, {"file": f.name, "source": col.source, "counts": counts})
        out[str(f)] = counts
    return out


def _isnan(x) -> bool:
    return isinstance(x, float) and math.isnan(x)
