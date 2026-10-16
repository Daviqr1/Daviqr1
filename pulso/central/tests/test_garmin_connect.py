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
    rep = gc.sync(conn, days=1, client=FakeGarmin())
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
