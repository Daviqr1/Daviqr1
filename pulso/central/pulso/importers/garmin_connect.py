"""Garmin Connect (cloud) via the unofficial `garminconnect` library — your own account only.

Why: some numbers exist only after Garmin's firmware/cloud processing (overnight HRV readings,
sleep stages, Body Battery, all-day stress at 3 min) and the official Health/Activity APIs are
closed to new applicants. Trade-off: it is unofficial, can break when Garmin changes endpoints,
and automated access may conflict with Garmin's terms — keep the sync infrequent (daily).

Auth: first run uses GARMIN_EMAIL / GARMIN_PASSWORD (MFA prompt in the terminal); tokens are
stored in $PULSO_GARMIN_TOKENS (default ~/.garminconnect) and reused afterwards.

Every parser is tolerant: a payload whose shape changed is skipped and reported, never fatal.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pulso import metrics as M
from pulso import store
from pulso.store import Event

SRC = M.SRC_GC
# sleepLevels.activityLevel as used by Garmin Connect's sleep chart
SLEEP_LEVELS = {0: "deep", 1: "light", 2: "rem", 3: "awake"}


def _gmt_ms(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace("Z", "")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            continue
    return None


def _pairs(arr: Any, lo: float, hi: float, value_idx: int = 1) -> list[tuple[int, float]]:
    out = []
    for row in arr or []:
        try:
            t, v = int(row[0]), row[value_idx]
        except (TypeError, IndexError, ValueError):
            continue
        if v is not None and lo <= float(v) <= hi:
            out.append((t, float(v)))
    return out


def parse_heart_rates(js: dict) -> dict[str, list]:
    out = {M.HR: _pairs(js.get("heartRateValues"), 25, 230)}
    if js.get("restingHeartRate"):
        start = _gmt_ms(js.get("startTimestampGMT"))
        if start:
            out[M.RESTING_HR] = [(start, float(js["restingHeartRate"]))]
    return out


def parse_stress(js: dict) -> dict[str, list]:
    bb = []
    for row in js.get("bodyBatteryValuesArray") or []:
        # [ts, "MEASURED", level, version]  (older payloads: [ts, level])
        try:
            t = int(row[0])
            v = row[2] if len(row) >= 3 and isinstance(row[1], str) else row[1]
        except (TypeError, IndexError, ValueError):
            continue
        if v is not None and 0 <= float(v) <= 100:
            bb.append((t, float(v)))
    return {M.STRESS_GARMIN: _pairs(js.get("stressValuesArray"), 0, 100), M.BODY_BATTERY: bb}


def parse_respiration(js: dict) -> dict[str, list]:
    return {M.RESP_RATE: _pairs(js.get("respirationValuesArray"), 1, 80)}


def parse_spo2(js: dict) -> dict[str, list]:
    rows = _pairs(js.get("spO2SingleValues"), 50, 100)
    if not rows:
        rows = _pairs(js.get("spO2HourlyAverages"), 50, 100)
    return {M.SPO2: rows}


def parse_steps(js: list) -> dict[str, list]:
    out = []
    for b in js or []:
        s, e = _gmt_ms(b.get("startGMT")), _gmt_ms(b.get("endGMT"))
        if s is None or e is None or e <= s or b.get("steps") is None:
            continue
        minutes = (e - s) / 60_000
        rate = float(b["steps"]) / minutes
        out += [(s + k * 60_000, rate) for k in range(int(minutes))]
    return {M.STEPS_PM: out}


def parse_hrv(js: dict | None) -> tuple[dict[str, list], list[Event]]:
    if not js:
        return {}, []
    readings = []
    for r in js.get("hrvReadings") or []:
        t = _gmt_ms(r.get("readingTimeGMT"))
        v = r.get("hrvValue")
        if t and v and 0 < v < 400:
            readings.append((t, float(v)))
    out: dict[str, list] = {M.HRV_GARMIN: readings}
    evs = []
    summ = js.get("hrvSummary") or {}
    end = _gmt_ms(js.get("endTimestampGMT")) or (readings[-1][0] if readings else None)
    if summ.get("lastNightAvg") and end:
        out[M.HRV_GARMIN_NIGHT] = [(end, float(summ["lastNightAvg"]))]
        evs.append(Event(f"gc:hrvstatus:{summ.get('calendarDate', end)}", "note", end, end,
                         f"HRV status: {summ.get('status')}", SRC,
                         {"kind": "hrv_status", "status": summ.get("status"), "weekly_average": summ.get("weeklyAvg"),
                          "baseline": summ.get("baseline")}))
    return out, evs


def parse_sleep(js: dict) -> tuple[list[tuple[int, int, str]], list[Event]]:
    epochs = []
    for lv in js.get("sleepLevels") or []:
        s, e = _gmt_ms(lv.get("startGMT")), _gmt_ms(lv.get("endGMT"))
        lvl = lv.get("activityLevel")
        if s is None or e is None or e <= s or lvl is None:
            continue
        stage = SLEEP_LEVELS.get(int(round(float(lvl))), "unmeasurable")
        # store 1-min epochs so overlaps with other sources stay comparable
        for t in range(s, e, 60_000):
            epochs.append((t, min(60, (e - t) // 1000), stage))
    evs = []
    dto = js.get("dailySleepDTO") or {}
    s, e = dto.get("sleepStartTimestampGMT"), dto.get("sleepEndTimestampGMT")
    if s and e:
        evs.append(Event(f"gc:sleep:{s}", "sleep", int(s), int(e), "sono (Garmin)", SRC,
                         {"score": ((dto.get("sleepScores") or {}).get("overall") or {}).get("value"),
                          "deep_s": dto.get("deepSleepSeconds"), "light_s": dto.get("lightSleepSeconds"),
                          "rem_s": dto.get("remSleepSeconds"), "awake_s": dto.get("awakeSleepSeconds"),
                          "avg_stress": dto.get("avgSleepStress")}))
    return epochs, evs


def _client():
    try:
        from garminconnect import Garmin
    except ImportError as e:  # pragma: no cover
        raise SystemExit("pip install 'pulso[garmin]' para sincronizar com o Garmin Connect") from e
    tokens = os.environ.get("PULSO_GARMIN_TOKENS", str(Path.home() / ".garminconnect"))
    try:
        g = Garmin()
        g.login(tokens)
        return g
    except Exception:
        email, pw = os.environ.get("GARMIN_EMAIL"), os.environ.get("GARMIN_PASSWORD")
        if not (email and pw):
            raise SystemExit("defina GARMIN_EMAIL e GARMIN_PASSWORD no primeiro login (os tokens ficam salvos depois)")
        g = Garmin(email, pw, prompt_mfa=lambda: input("código MFA da Garmin: "))
        g.login()
        g.garth.dump(tokens)  # garth persists the OAuth tokens for the next runs
        return g


def store_day(conn: sqlite3.Connection, series: dict[str, list], epochs: list, events: list[Event]) -> dict[str, int]:
    n: dict[str, int] = {}
    with store.transaction(conn):
        for metric, rows in series.items():
            if rows:
                n[metric] = n.get(metric, 0) + store.insert_samples(conn, metric, SRC, rows)
        if epochs:
            conn.executemany("INSERT OR IGNORE INTO sleep_epochs (source, ts_ms, duration_s, stage) VALUES (?, ?, ?, ?)",
                             [(SRC, t, d, s) for t, d, s in epochs])
            n["sleep_epochs"] = len(epochs)
        for ev in events:
            store.upsert_event(conn, ev)
    return n


def sync(conn: sqlite3.Connection, days: int = 7, tz: str | None = None, client=None) -> dict:
    g = client or _client()
    today = date.today()
    report: dict[str, Any] = {}
    for k in range(days):
        d = (today - timedelta(days=k)).isoformat()
        series: dict[str, list] = {}
        epochs: list = []
        events: list[Event] = []
        problems = []
        calls = [
            ("heart_rates", lambda: series.update(parse_heart_rates(g.get_heart_rates(d)))),
            ("stress", lambda: series.update(parse_stress(g.get_stress_data(d)))),
            ("respiration", lambda: series.update(parse_respiration(g.get_respiration_data(d)))),
            ("spo2", lambda: series.update(parse_spo2(g.get_spo2_data(d)))),
            ("steps", lambda: series.update(parse_steps(g.get_steps_data(d)))),
        ]
        for name, fn in calls:
            try:
                fn()
            except Exception as e:  # shape changes / missing data must not abort the sync
                problems.append(f"{name}: {e!r}"[:200])
        try:
            s, ev = parse_hrv(g.get_hrv_data(d))
            series.update(s)
            events += ev
        except Exception as e:
            problems.append(f"hrv: {e!r}"[:200])
        try:
            ep, ev = parse_sleep(g.get_sleep_data(d))
            epochs += ep
            events += ev
        except Exception as e:
            problems.append(f"sleep: {e!r}"[:200])
        report[d] = {"stored": store_day(conn, series, epochs, events), "problems": problems}
    return report
