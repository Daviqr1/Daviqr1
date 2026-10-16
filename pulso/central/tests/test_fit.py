from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pulso import store
from pulso import metrics as M

g = pytest.importorskip("garmin_fit_sdk")
from garmin_fit_sdk import Encoder, Profile  # noqa: E402

from pulso.importers import fit  # noqa: E402

NUM = {m["name"]: int(n) for n, m in Profile["messages"].items()}
T0 = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)


def fit_ts(dt: datetime) -> int:
    return int(dt.timestamp()) - fit.FIT_EPOCH_UNIX


def write(path: Path, mesgs: list[dict]) -> Path:
    enc = Encoder()
    for m in mesgs:
        enc.write_mesg(m)
    path.write_bytes(enc.close())
    return path


def test_monitoring_file(tmp_path):
    m = [{"mesg_num": NUM["file_id"], "type": "monitoring_b", "manufacturer": "garmin", "product": 3992,
          "time_created": T0, "serial_number": 1}]
    # full timestamp, then timestamp_16 samples every 60 s with HR, and cumulative walking cycles
    m.append({"mesg_num": NUM["monitoring"], "timestamp": T0, "activity_type": "walking", "cycles": 100.0})
    for i in range(1, 6):
        t = T0 + timedelta(minutes=i)
        m.append({"mesg_num": NUM["monitoring"], "timestamp_16": fit_ts(t) & 0xFFFF, "heart_rate": 60 + i})
    m.append({"mesg_num": NUM["monitoring"], "timestamp": T0 + timedelta(minutes=10), "activity_type": "walking", "cycles": 150.0})
    m.append({"mesg_num": NUM["stress_level"], "stress_level_time": T0 + timedelta(minutes=3), "stress_level_value": 42})
    m.append({"mesg_num": NUM["stress_level"], "stress_level_time": T0 + timedelta(minutes=6), "stress_level_value": -1})
    m.append({"mesg_num": NUM["respiration_rate"], "timestamp": T0 + timedelta(minutes=4), "respiration_rate": 14.5})
    (tmp_path / "Monitor").mkdir()
    f = write(tmp_path / "Monitor" / "x.fit", m)

    conn = store.connect(":memory:")
    res = fit.import_paths(conn, [str(tmp_path)])
    counts = res[str(f)]
    assert counts["hr"] == 5
    _, hr = store.series(conn, M.HR, 0, 2**62)
    assert hr.tolist() == [61, 62, 63, 64, 65]
    ts, _ = store.series(conn, M.HR, 0, 2**62)
    assert ts[0] == int((T0 + timedelta(minutes=1)).timestamp() * 1000)
    _, st = store.series(conn, M.STRESS_GARMIN, 0, 2**62)
    assert st.tolist() == [42]
    _, steps = store.series(conn, M.STEPS_PM, 0, 2**62)
    # cycles 100 -> 150 (scale 2) is raw 200 -> 300, decoded through the 'steps' sub-field
    assert steps.tolist() == [pytest.approx(100 / 10)]
    _, rsp = store.series(conn, M.RESP_RATE, 0, 2**62)
    assert rsp.tolist() == [pytest.approx(14.5)]
    # idempotent by content hash
    assert fit.import_paths(conn, [str(f)])[str(f)] == "já importado"


def test_sleep_and_hrv_status(tmp_path):
    m = [{"mesg_num": NUM["file_id"], "type": "monitoring_daily", "manufacturer": "garmin", "time_created": T0}]
    for i, lvl in enumerate(["light", "deep", "deep", "rem", "awake"]):
        m.append({"mesg_num": NUM["sleep_level"], "timestamp": T0 + timedelta(minutes=i), "sleep_level": lvl})
    for i in range(3):
        m.append({"mesg_num": NUM["hrv_value"], "timestamp": T0 + timedelta(minutes=5 * i), "value": 40.0 + i})
    m.append({"mesg_num": NUM["hrv_status_summary"], "timestamp": T0 + timedelta(hours=4), "last_night_average": 41.0,
              "weekly_average": 45.0, "status": "unbalanced"})
    f = write(tmp_path / "s.fit", m)
    conn = store.connect(":memory:")
    fit.import_paths(conn, [str(f)])
    rows = conn.execute("SELECT stage, duration_s FROM sleep_epochs ORDER BY ts_ms").fetchall()
    assert [r["stage"] for r in rows] == ["light", "deep", "deep", "rem", "awake"]
    assert rows[0]["duration_s"] == 60
    _, h = store.series(conn, M.HRV_GARMIN, 0, 2**62)
    assert h.tolist() == [40.0, 41.0, 42.0]
    assert store.series(conn, M.HRV_GARMIN_NIGHT, 0, 2**62)[1].tolist() == [41.0]
    ev = store.events(conn, 0, 2**62, kinds=["note"])
    assert ev and ev[0].attrs["status"] == "unbalanced"


def test_activity_with_hrv_logging(tmp_path):
    m = [{"mesg_num": NUM["file_id"], "type": "activity", "manufacturer": "garmin", "time_created": T0}]
    rr_all = []
    for s in range(10):
        t = T0 + timedelta(seconds=s)
        m.append({"mesg_num": NUM["record"], "timestamp": t, "heart_rate": 120 + s})
        rr = [0.5, 0.5]
        rr_all += rr
        m.append({"mesg_num": NUM["hrv"], "time": rr})
    m.append({"mesg_num": NUM["session"], "timestamp": T0 + timedelta(seconds=10), "start_time": T0,
              "total_elapsed_time": 10.0, "sport": "running", "sub_sport": "generic", "avg_heart_rate": 125})
    f = write(tmp_path / "a.fit", m)
    conn = store.connect(":memory:")
    fit.import_paths(conn, [str(f)])
    ts, rr = store.rr(conn, 0, 2**62)
    assert rr.tolist() == [500] * 20
    assert ts[0] == int(T0.timestamp() * 1000) + 500
    assert store.series(conn, M.HR, 0, 2**62)[1].size == 10
    ev = store.events(conn, 0, 2**62, kinds=["workout"])
    assert ev and ev[0].title == "running" and ev[0].end_ms - ev[0].start_ms == 10_000


def test_garmin_account_export(tmp_path):
    import io
    import json
    import zipfile

    from pulso.importers import garmin_export

    m = [{"mesg_num": NUM["file_id"], "type": "monitoring_b", "manufacturer": "garmin", "time_created": T0},
         {"mesg_num": NUM["monitoring"], "timestamp": T0, "heart_rate": 58},
         {"mesg_num": NUM["stress_level"], "stress_level_time": T0, "stress_level_value": 20}]
    enc = Encoder()
    for x in m:
        enc.write_mesg(x)
    fit_bytes = enc.close()
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("123456_WELLNESS.fit", fit_bytes)
        z.writestr("not_a_fit.fit", b"garbage")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as z:
        z.writestr("DI_CONNECT/DI-Connect-Uploaded-Files/UploadedFiles_0-_Part1.zip", inner.getvalue())
        z.writestr("DI_CONNECT/DI-Connect-Wellness/2026-10-01_2026-11-01_123_sleepData.json", json.dumps([
            {"calendarDate": "2026-10-15", "sleepStartTimestampGMT": "2026-10-15T02:00:00.0",
             "sleepEndTimestampGMT": "2026-10-15T09:30:00.0", "deepSleepSeconds": 4000,
             "sleepScores": {"overallScore": 78}}]))
        z.writestr("DI_CONNECT/DI-Connect-Aggregator/UDSFile_2026-10-01_2026-11-01.json", json.dumps([
            {"calendarDate": "2026-10-15", "restingHeartRate": 51}, {"calendarDate": "2026-10-16"}]))
    conn = store.connect(":memory:")
    c = garmin_export.import_bytes(conn, outer.getvalue(), "America/Sao_Paulo", archive=tmp_path / "arch")
    assert c["fit_files"] == 1 and c["fit_new"] == 1 and c["sleep_nights"] == 1 and c["daily_summaries"] == 1
    assert store.series(conn, M.HR, 0, 2**62)[1].tolist() == [58]
    assert store.series(conn, M.RESTING_HR, 0, 2**62)[1].tolist() == [51]
    assert store.events(conn, 0, 2**62, kinds=["sleep"])[0].attrs["score"] == 78
    assert (tmp_path / "arch" / "export" / "123456_WELLNESS.fit").exists()
    # re-import is a no-op
    c2 = garmin_export.import_bytes(conn, outer.getvalue(), "America/Sao_Paulo", archive=tmp_path / "arch")
    assert c2["fit_new"] == 0
