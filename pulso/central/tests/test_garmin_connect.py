from pulso import metrics as M
from pulso import store
from pulso.importers import garmin_connect as gc


class FakeGarmin:
    def get_heart_rates(self, d):
        return {"startTimestampGMT": "2026-10-15T03:00:00.0", "restingHeartRate": 52,
                "heartRateValues": [[1760497200000, 60], [1760497320000, None], [1760497440000, 250]]}

    def get_stress_data(self, d):
        return {"stressValuesArray": [[1760497200000, 30], [1760497380000, -1], [1760497560000, -2]],
                "bodyBatteryValuesArray": [[1760497200000, "MEASURED", 77, 2.0], [1760497380000, "MEASURED", None, 2.0]]}

    def get_respiration_data(self, d):
        return {"respirationValuesArray": [[1760497200000, 13.0], [1760497320000, -1.0]]}

    def get_spo2_data(self, d):
        raise KeyError("schema changed")

    def get_steps_data(self, d):
        return [{"startGMT": "2026-10-15T12:00:00.0", "endGMT": "2026-10-15T12:15:00.0", "steps": 300}]

    def get_hrv_data(self, d):
        return {"hrvSummary": {"calendarDate": d, "lastNightAvg": 44, "weeklyAvg": 48, "status": "UNBALANCED"},
                "endTimestampGMT": "2026-10-15T09:00:00.0",
                "hrvReadings": [{"hrvValue": 40, "readingTimeGMT": "2026-10-15T04:00:00.0"},
                                {"hrvValue": 47, "readingTimeGMT": "2026-10-15T04:05:00.0"}]}

    def get_sleep_data(self, d):
        return {"dailySleepDTO": {"sleepStartTimestampGMT": 1760490000000, "sleepEndTimestampGMT": 1760515200000,
                                  "deepSleepSeconds": 3600, "sleepScores": {"overall": {"value": 71}}},
                "sleepLevels": [{"startGMT": "2026-10-15T01:00:00.0", "endGMT": "2026-10-15T01:03:00.0", "activityLevel": 1.0},
                                {"startGMT": "2026-10-15T01:03:00.0", "endGMT": "2026-10-15T01:05:00.0", "activityLevel": 0.0}]}


def test_sync_parses_and_tolerates_breakage():
    conn = store.connect(":memory:")
    rep = gc.sync(conn, days=1, client=FakeGarmin(), pause_s=0, log=lambda m: None)
    day = next(iter(rep.values()))
    assert any("spo2" in p for p in day["problems"])  # broken endpoint reported, not fatal
    assert store.series(conn, M.HR, 0, 2**62)[1].tolist() == [60]
    assert store.series(conn, M.STRESS_GARMIN, 0, 2**62)[1].tolist() == [30]
    assert store.series(conn, M.BODY_BATTERY, 0, 2**62)[1].tolist() == [77]
    assert store.series(conn, M.RESP_RATE, 0, 2**62)[1].tolist() == [13.0]
    steps = store.series(conn, M.STEPS_PM, 0, 2**62)[1]
    assert steps.size == 15 and steps[0] == 20
    assert store.series(conn, M.HRV_GARMIN, 0, 2**62)[1].tolist() == [40, 47]
    assert store.series(conn, M.HRV_GARMIN_NIGHT, 0, 2**62)[1].tolist() == [44]
    stages = [r["stage"] for r in conn.execute("SELECT stage FROM sleep_epochs ORDER BY ts_ms")]
    assert stages == ["light"] * 3 + ["deep"] * 2
    sl = store.events(conn, 0, 2**62, kinds=["sleep"])[0]
    assert sl.attrs["score"] == 71


def test_backfill_is_resumable_and_cold_aware():
    from datetime import date, timedelta

    calls = []

    class Counting(FakeGarmin):
        def get_heart_rates(self, d):
            calls.append(d)
            if d < (date.today() - timedelta(days=200)).isoformat():
                return {"heartRateValues": []}  # cold storage looks like an empty day
            return super().get_heart_rates(d)

        def get_stress_data(self, d):
            if d < (date.today() - timedelta(days=200)).isoformat():
                return {}
            return super().get_stress_data(d)

        def request_reload(self, d):
            calls.append(("reload", d))
            return {}

    conn = store.connect(":memory:")
    until = date.today() - timedelta(days=10)
    gc.sync(conn, client=Counting(), since=until - timedelta(days=4), until=until, pause_s=0, log=lambda m: None)
    assert len(calls) == 5
    calls.clear()
    gc.sync(conn, client=Counting(), since=until - timedelta(days=4), until=until, pause_s=0, log=lambda m: None)
    assert calls == []  # everything already synced
    old = date.today() - timedelta(days=300)
    rep = gc.sync(conn, client=Counting(), since=old, until=old, pause_s=0, reload_cold=True, log=lambda m: None)
    assert ("reload", old.isoformat()) in calls
    assert "frio" in " ".join(rep[old.isoformat()]["problems"])
    # cold days are not marked as synced, so a later run fetches them again
    assert conn.execute("SELECT 1 FROM artefacts WHERE kind = 'gc_synced' AND key = ?", (old.isoformat(),)).fetchone() is None


def test_day_synced_while_filling_in_is_fetched_again(monkeypatch):
    """F12: a sync on day D (say at 18:00) must not mark D as synced, or a later backfill skips the
    rest of that day (and its wellness FIT zip) for good."""
    from datetime import date

    calls = []

    class Counting(FakeGarmin):
        def get_heart_rates(self, d):
            calls.append(d)
            return super().get_heart_rates(d)

    clock = {"today": date(2026, 10, 14)}

    class Day(date):
        @classmethod
        def today(cls):
            return clock["today"]

    monkeypatch.setattr(gc, "date", Day)
    conn = store.connect(":memory:")
    gc.sync(conn, days=1, client=Counting(), pause_s=0, log=lambda m: None)
    assert calls == ["2026-10-14"]
    clock["today"] = date(2026, 10, 17)
    calls.clear()
    gc.sync(conn, days=7, client=Counting(), pause_s=0, log=lambda m: None)
    assert "2026-10-14" in calls and len(calls) == 7
    calls.clear()
    gc.sync(conn, days=7, client=Counting(), pause_s=0, log=lambda m: None)
    assert calls == ["2026-10-17", "2026-10-16"]  # complete days are skipped now, the last 2 never


def test_login_failure_is_an_error_not_an_exit(monkeypatch):
    """F1: SystemExit from the client stopped the daemon's loop (and silently killed the notebook's
    jobs thread). Expired tokens without credentials, or a missing extra, are a plain error."""
    import sys
    from types import SimpleNamespace

    import pytest

    class Garmin:
        def __init__(self, *a, **k):
            pass

        def login(self, tokens):
            raise ConnectionError("offline")

    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.delenv("GARMIN_PASSWORD", raising=False)
    monkeypatch.setitem(sys.modules, "garminconnect", SimpleNamespace(Garmin=Garmin))
    with pytest.raises(gc.GarminLoginError) as e:
        gc._client()
    assert not isinstance(e.value, SystemExit) and "GARMIN_EMAIL" in str(e.value) and "offline" in str(e.value)
    monkeypatch.setitem(sys.modules, "garminconnect", None)  # the `garmin` extra is not installed
    with pytest.raises(gc.GarminLoginError):
        gc._client()
