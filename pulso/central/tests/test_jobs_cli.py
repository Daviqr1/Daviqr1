import math
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pulso import analysis, cli, config, jobs, store
from pulso import metrics as M
from pulso import scheduler as S
from pulso.store import Event

from test_analysis import TZ, _fill_days

MIN = 60_000
ZONE = ZoneInfo(TZ)


def test_jobs_meetings_and_sleep(tmp_path):
    conn = store.connect(":memory:")
    first, target = date(2026, 9, 20), date(2026, 10, 5)
    _fill_days(conn, first, 16, target)
    # transcript segments for the meeting so segment effects run
    ev = store.events(conn, 0, 2**62, kinds=["meeting"])[0]
    rows = []
    for i in range(30):
        s = ev.start_ms + i * 90_000
        rows.append((ev.id, i, s, s + 90_000, ["Ana", "Bruno", "eu"][i % 3], "texto", '["prazo"]' if i % 3 == 1 else '["tecnico"]'))
    conn.executemany("INSERT INTO transcript_segments VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    res = jobs.run_range(conn, date(2026, 10, 3), target, TZ)
    assert len(res["days"]) == 3
    assert res["meetings"]["n_meetings"] == 1
    worst = res["meetings"]["worst"][0]
    assert worst["event_id"] == "meet-1" and worst["during"] > 0.5
    assert res["meetings"]["by_speaker"]  # segment effects computed
    assert res["nights"], "nights from sleep epochs"
    last = store.get_artefact(conn, "sleep:last", "latest")
    assert last["total_sleep_min"] > 300 and last["hrv_status"] in {"insufficient", "normal", "below", "above"}
    # the face now has something to say about the past
    from pulso.face import face_state

    a, b = analysis.local_day_bounds(target, TZ)
    st = face_state(conn, a + 16 * 60 * MIN, TZ)
    assert st["ep"] >= 1 and st["slp"] > 300


def test_cli_end_to_end(tmp_path, capsys):
    db = str(tmp_path / "p.sqlite")
    assert cli.main(["--db", db, "token", "create", "fr255"]) == 0
    tok = capsys.readouterr().out.strip()
    assert tok.startswith("pulso_dev_")
    vtt = tmp_path / "r.vtt"
    vtt.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Ana>oi\n", encoding="utf-8")
    assert cli.main(["--db", db, "import", "transcript", str(vtt), "--start", "2026-10-16T14:00"]) == 0
    assert capsys.readouterr().out.startswith("meeting:")
    assert cli.main(["--db", db, "analyze", "--date", "2026-10-16"]) == 0
    out = capsys.readouterr().out
    assert '"day": "2026-10-16"' in out
    assert cli.main(["--db", db, "inventory"]) == 0


def test_back_to_back_meetings_and_mark_validation():
    from pulso.ai import facts as F

    conn = store.connect(":memory:")
    d = date(2026, 10, 7)
    a, b = analysis.local_day_bounds(d, TZ)
    z = np.zeros((b - a) // MIN)
    slots = [(9 * 60, 30, False), (9 * 60 + 35, 30, True), (10 * 60 + 10, 30, True), (10 * 60 + 45, 30, True),
             (14 * 60, 30, False), (16 * 60, 30, False), (17 * 60, 30, False), (8 * 60, 20, False)]
    for k, (m0, dur, b2b) in enumerate(slots):
        z[m0 : m0 + dur] = 1.5 if b2b else 0.3
        store.upsert_event(conn, Event(f"m{k}", "meeting", a + m0 * MIN, a + (m0 + dur) * MIN, f"reunião {k}", "t", {}))
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(a + i * MIN, float(v)) for i, v in enumerate(z)])
    store.upsert_event(conn, Event("mark1", "note", a + (9 * 60 + 40) * MIN, a + (9 * 60 + 40) * MIN, "estresse", "watch:cap", {}))
    store.put_artefact(conn, "analysis:day", d.isoformat(), {
        "day": d.isoformat(), "arousal_mean_awake": 0.2, "arousal_minutes_above_1_5": 0,
        "episodes": [{"start_ms": a + 9 * 60 * MIN, "end_ms": a + (10 * 60 + 40) * MIN, "peak_ms": a + 9 * 60 * MIN,
                      "peak_z": 1.5, "mean_z": 1.5, "auc_z_min": 90, "movement": 0, "kind": "mental", "overlaps": []}]})
    s = jobs.meeting_job(conn, d, d, TZ)
    bb = s["back_to_back"]
    # 09:35, 10:10 and 10:45 follow another meeting after 5 min; 09:00 follows the 08:00 one after 40 min
    assert bb["n_b2b"] == 3 and bb["n_break"] == 5
    assert bb["diff"] > 0.8
    book = F.build(conn, d, d, TZ)
    assert any("1 de 1 marcações" in f.text for f in book.facts)


# ------------------------------------------------------------------------------ sleep job


def _nights(conn, end: date, n: int, src: str = "gc", hrv=lambda k: 60.0, skip=(), hours: int = 7):
    """`n` nights (23:00 onset, `hours` long) waking on the n days up to `end`, with Garmin's 5-min
    overnight HRV readings; nights waking on a day in `skip` are missing."""
    for k in range(n):
        d = end - timedelta(days=n - 1 - k)
        if d in skip:
            continue
        on = int(datetime.combine(d - timedelta(days=1), time(23, 0), tzinfo=ZONE).timestamp() * 1000)
        conn.executemany("INSERT OR IGNORE INTO sleep_epochs VALUES (?, ?, 60, ?)",
                         [(src, on + i * MIN, "deep" if i % 3 == 0 else "light") for i in range(hours * 60)])
        store.insert_samples(conn, M.HRV_GARMIN, "fit:hrv", [(on + j * 5 * MIN, hrv(k)) for j in range(hours * 12)])


END = date(2026, 10, 16)


@pytest.fixture()
def after_end(monkeypatch):
    """The wall clock at noon after END: every night up to END is final."""
    monkeypatch.setattr(store, "now_ms", lambda: int(datetime.combine(END, time(12, 0), tzinfo=ZONE).timestamp() * 1000))


def test_short_sleep_runs_score_each_night_with_its_own_context(after_end):
    """F2: the daemon's 4-day range job, `analyze --days 1` and the morning refresh must give a night
    the same debt / HRV status as a long run, and must not rewrite (or clip) the nights before them."""
    conn = store.connect(":memory:")
    end = END
    _nights(conn, end, 30, hrv=lambda k: 60.0 if k < 23 else 35.0)  # HRV low in the last week
    jobs.sleep_job(conn, end - timedelta(days=29), end, TZ)
    keys = ("hrv_status", "debt_min", "ln_rmssd_7d", "total_sleep_min")
    full = {k: store.get_artefact(conn, "sleep:night", end.isoformat())[k] for k in keys}
    assert full["hrv_status"] == "below" and full["debt_min"] == 14 * 30
    assert full["ln_rmssd_7d"] == pytest.approx(math.log(35), abs=1e-3)
    for days in (4, 1):
        jobs.sleep_job(conn, end - timedelta(days=days - 1), end, TZ)
        assert {k: store.get_artefact(conn, "sleep:night", end.isoformat())[k] for k in keys} == full
        assert store.get_artefact(conn, "sleep:last", "latest")["hrv_status"] == "below"
        before = (end - timedelta(days=days)).isoformat()  # straddles the range start: context, kept whole
        assert store.get_artefact(conn, "sleep:night", before)["total_sleep_min"] == 420
    # an old range never moves sleep:last back
    jobs.sleep_job(conn, end - timedelta(days=20), end - timedelta(days=18), TZ)
    assert store.get_artefact(conn, "sleep:last", "latest")["wake_day"] == end.isoformat()


def test_sleep_debt_counts_calendar_days_not_list_entries(after_end):
    conn = store.connect(":memory:")
    end = END
    _nights(conn, end, 40, skip={end - timedelta(days=k) for k in (2, 4, 6, 8, 10)})
    night = jobs.sleep_job(conn, end, end, TZ)[-1]
    assert night["wake_day"] == end.isoformat()
    assert night["debt_min"] == 9 * 30  # 14 calendar days, 5 of them without data (not 14 nights reaching back)


def test_each_night_takes_its_own_staging_source(after_end):
    """F13: nights with only Garmin Connect epochs are kept next to nights with FIT staging, and a
    night with both uses the FIT one."""
    conn = store.connect(":memory:")
    end = END
    _nights(conn, end - timedelta(days=2), 2, src="fit:sleep")
    _nights(conn, end, 2, src="gc")  # wellness zip failed for the last two nights
    _nights(conn, end - timedelta(days=3), 1, src="gc", hours=5)  # a shorter GC copy of a FIT night
    nights = jobs.sleep_job(conn, end - timedelta(days=3), end, TZ)
    assert [n["wake_day"] for n in nights] == [(end - timedelta(days=k)).isoformat() for k in (3, 2, 1, 0)]
    assert [n["total_sleep_min"] for n in nights] == [420] * 4
    assert store.get_artefact(conn, "sleep:last", "latest")["wake_day"] == end.isoformat()


def test_open_night_is_left_for_a_later_run(monkeypatch):
    """A night is written only once final: 90 min without a new epoch, or closed by Garmin's own
    sleep record. Written early, a partial night would stop the morning refresh and could fire the
    'sono' alert on half a night."""
    end = END
    wake = int(datetime.combine(end, time(6, 0), tzinfo=ZONE).timestamp() * 1000)
    for closed_by_garmin in (False, True):
        conn = store.connect(":memory:")
        _nights(conn, end, 3)
        monkeypatch.setattr(store, "now_ms", lambda: wake + 30 * MIN)  # a later epoch could still join it
        assert [n["wake_day"] for n in jobs.sleep_job(conn, end, end, TZ)] == []
        assert jobs.last_night(conn, end, TZ) is None
        if closed_by_garmin:  # dailySleepDTO: the main sleep ended there
            store.upsert_event(conn, Event("gc:sleep:1", "sleep", wake - 7 * 60 * MIN, wake - 2 * MIN, "sono (Garmin)",
                                           "gc", {}))
        else:
            monkeypatch.setattr(store, "now_ms", lambda: wake + 2 * 60 * MIN)
        assert jobs.last_night(conn, end, TZ)["wake_day"] == end.isoformat()


# ---------------------------------------------------------------------------- daily table


def test_daily_table_counts_a_transcribed_meeting_once():
    """F9: a calendar entry and the transcript anchored to it are one meeting."""
    conn = store.connect(":memory:")
    d = date(2026, 10, 7)
    a, _ = analysis.local_day_bounds(d, TZ)
    store.upsert_event(conn, Event("cal:x@g:1", "calendar", a + 600 * MIN, a + 660 * MIN, "Sync", "ics", {}))
    store.upsert_event(conn, Event("meeting:abc", "meeting", a + 600 * MIN, a + 659 * MIN, "Sync", "transcript:vtt",
                                   {"start_source": "calendar:cal:x@g:1"}))
    store.upsert_event(conn, Event("cal:y@g:1", "calendar", a + 900 * MIN, a + 930 * MIN, "1:1", "ics", {}))
    row = jobs.daily_table(conn, d, d, TZ)[0]
    assert row["meetings"] == 2 and row["meeting_minutes"] == 59 + 30


# ------------------------------------------------------------------------------ scheduler


def _drive(monkeypatch, settings, start: datetime, until: datetime, execute=()) -> list[tuple[str, str]]:
    """Run scheduler.loop on a fake clock (30-s passes) from `start` until `until`, then Ctrl+C.
    Returns [("DD HH:MM", job)] for every job started; only jobs named in `execute` really run."""
    clock = {"t": start}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["t"].astimezone(tz) if tz else clock["t"].replace(tzinfo=None)

    def sleep(s):
        clock["t"] += timedelta(seconds=s)
        if clock["t"] >= until:
            raise KeyboardInterrupt

    monkeypatch.setattr(S, "datetime", Clock)
    monkeypatch.setattr(S, "time", SimpleNamespace(time=lambda: clock["t"].timestamp(), sleep=sleep))
    monkeypatch.setattr(store, "now_ms", lambda: int(clock["t"].timestamp() * 1000))
    runs: list[tuple[str, str]] = []
    real = S._run

    def run(conn, name, fn):
        runs.append((clock["t"].strftime("%d %H:%M"), name))
        return real(conn, name, fn) if name in execute else True

    monkeypatch.setattr(S, "_run", run)
    with pytest.raises(KeyboardInterrupt):  # Ctrl+C still stops the loop
        S.loop(settings)
    return runs


@pytest.fixture()
def no_garmin(monkeypatch, tmp_path):
    monkeypatch.setenv("PULSO_GARMIN_TOKENS", str(tmp_path / "no-tokens"))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)


def test_job_that_exits_does_not_stop_the_scheduler(tmp_path, monkeypatch):
    """F1: the Garmin client used to raise SystemExit (no login, missing extra); a job failure of any
    kind is logged and the loop goes on, while Ctrl+C still stops it."""
    from pulso.importers import garmin_connect

    def boom(*a, **k):
        raise SystemExit("sem login")

    monkeypatch.setenv("PULSO_GARMIN_TOKENS", str(tmp_path))  # the token folder exists
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    monkeypatch.setattr(garmin_connect, "sync", boom)
    conn = store.connect(tmp_path / "p.sqlite")
    assert S._run(conn, "x", boom) is False

    def ctrl_c():
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        S._run(conn, "x", ctrl_c)
    settings = config.Settings(db_path=tmp_path / "p.sqlite", tz=TZ, anthropic_api_key="x")
    start = datetime(2026, 10, 16, 3, 0, tzinfo=ZONE)
    runs = _drive(monkeypatch, settings, start, start + timedelta(hours=4), execute={"garmin:sync"})
    names = [n for _, n in runs]
    assert names.count("garmin:sync") >= 3 and "analyze:range" in names and "report:daily" in names
    log = store.get_artefact(conn, "job", "garmin:sync")
    assert log["ok"] is False and "sem login" in log["detail"]


def test_range_job_catches_up_after_the_laptop_slept_through_0430(tmp_path, monkeypatch, no_garmin):
    """F7: woken at 08:50, the range job runs at once (before the report); next day at 04:30."""
    settings = config.Settings(db_path=tmp_path / "p.sqlite", tz=TZ, anthropic_api_key="x")
    runs = _drive(monkeypatch, settings, datetime(2026, 10, 16, 8, 50, tzinfo=ZONE),
                  datetime(2026, 10, 17, 5, 0, tzinfo=ZONE))
    names = [n for _, n in runs]
    assert [t for t, n in runs if n == "analyze:range"] == ["16 08:50", "17 04:30"]
    assert names.index("analyze:range") < names.index("report:daily")
    # restarted later the same day: what already ran fine is not redone
    conn = store.connect(tmp_path / "p.sqlite")
    ran = int(datetime(2026, 10, 17, 8, 0, tzinfo=ZONE).timestamp() * 1000)
    for job in ("analyze:range", "report:daily", "export:summary"):
        store.put_artefact(conn, "job", job, {"ok": True, "at": ran, "detail": ""})
    runs = _drive(monkeypatch, settings, datetime(2026, 10, 17, 9, 0, tzinfo=ZONE),
                  datetime(2026, 10, 17, 9, 30, tzinfo=ZONE))
    assert not {"analyze:range", "report:daily", "export:summary"} & {n for _, n in runs}


def test_morning_export_always_follows_the_report(tmp_path, monkeypatch, no_garmin):
    """F8/F11: a daemon running across midnight still exports all Docs at 06:45; the 2-hourly
    refresh is the 'Hoje' Doc alone and never stands in for the morning export."""
    settings = config.Settings(db_path=tmp_path / "p.sqlite", tz=TZ, anthropic_api_key="x")
    runs = _drive(monkeypatch, settings, datetime(2026, 10, 15, 20, 10, tzinfo=ZONE),
                  datetime(2026, 10, 16, 9, 0, tzinfo=ZONE))
    assert ("16 06:30", "report:daily") in runs
    assert [t for t, n in runs if n == "export:summary"] == ["15 20:10", "16 06:45"]
    assert [t for t, n in runs if n == "export:hoje"] == ["15 22:10", "16 00:10", "16 02:10", "16 04:10", "16 06:10",
                                                         "16 08:45"]


def test_export_publishes_only_hoje_every_two_hours(tmp_path, monkeypatch):
    from pulso.export import gdrive, summary

    docs = [summary.Doc(k, k, f"# {k}") for k in ("hoje", "dias", "semanas")]
    monkeypatch.setattr(summary, "documents", lambda *a, **k: list(docs))
    written = []
    monkeypatch.setattr(summary, "write_local", lambda d, tz, folder=None: written.append([x.key for x in d]) or tmp_path)
    monkeypatch.setattr(gdrive, "_paths", lambda: (None, tmp_path / "no-token", None))
    monkeypatch.setenv("PULSO_GARMIN_TOKENS", str(tmp_path / "no-tokens"))
    monkeypatch.delenv("GARMIN_EMAIL", raising=False)
    settings = config.Settings(db_path=tmp_path / "p.sqlite", tz=TZ, anthropic_api_key=None)
    _drive(monkeypatch, settings, datetime(2026, 10, 16, 5, 0, tzinfo=ZONE), datetime(2026, 10, 16, 7, 0, tzinfo=ZONE),
           execute={"export:summary", "export:hoje"})
    assert written == [["hoje"], ["hoje", "dias", "semanas"]]


def test_last_night_reaches_face_and_morning_alert_the_same_morning(tmp_path, monkeypatch):
    """contract:F4: Garmin publishes the night after the wake-up sync, hours after 04:30. The morning
    syncs and refreshes pick it up as soon as it is final, the 'sono' alert fires before 11:00 and the
    face shows this night, not the previous one."""
    from pulso.face import face_state
    from pulso.importers import garmin_connect

    db = tmp_path / "p.sqlite"
    conn = store.connect(db)
    today = date(2026, 10, 16)
    _nights(conn, today - timedelta(days=1), 30, hrv=lambda k: 60.0 if k < 24 else 35.0)
    syncs = []

    def fake_sync(c, days=2, tz=None):  # the watch syncs after waking at 06:00; GC has it from 07:30
        t = store.now_ms()
        syncs.append(datetime.fromtimestamp(t / 1000, tz=ZONE).strftime("%H:%M") + f"/{days}")
        if t >= int(datetime.combine(today, time(7, 30), tzinfo=ZONE).timestamp() * 1000):
            _nights(c, today, 1, hrv=lambda k: 35.0)
        return {}

    monkeypatch.setattr(garmin_connect, "sync", fake_sync)
    monkeypatch.setenv("PULSO_GARMIN_TOKENS", str(tmp_path))
    settings = config.Settings(db_path=db, tz=TZ, anthropic_api_key=None)
    runs = _drive(monkeypatch, settings, datetime(2026, 10, 16, 4, 0, tzinfo=ZONE),
                  datetime(2026, 10, 16, 10, 30, tzinfo=ZONE),
                  execute={"garmin:sync", "analyze:range", "sleep:last-night", "analyze:today"})
    # every 6 h (2 days), plus hourly for today only while the night is missing
    assert syncs == ["04:00/2", "05:00/1", "06:00/1", "07:00/1", "08:00/1", "10:00/2"]
    night = store.get_artefact(conn, "sleep:night", today.isoformat())
    assert night["total_sleep_min"] == 420 and night["hrv_status"] == "below"
    alert = store.get_artefact(conn, "alert", f"sono:{today.isoformat()}")
    assert alert and "HRV" in alert["detail"]
    fired_at = datetime.fromtimestamp(alert["at_ms"] / 1000, tz=ZONE)
    assert (fired_at.hour, fired_at.minute) <= (8, 31)
    st = face_state(conn, int(datetime.combine(today, time(9, 0), tzinfo=ZONE).timestamp() * 1000), TZ)
    assert st["slp"] == 420 and st["hrvs"] == "below"


def test_morning_start_syncs_garmin_once_not_twice(tmp_path, monkeypatch):
    """verify N3: opening the laptop at 08:00 with last night missing runs the 2-day sync once; the
    hourly today-only sync counts from it instead of fetching the same day again 30 s later."""
    from pulso.importers import garmin_connect

    db = tmp_path / "p.sqlite"
    conn = store.connect(db)
    _nights(conn, date(2026, 10, 15), 30)
    syncs = []

    def fake_sync(c, days=2, tz=None):  # the night never comes (watch not worn)
        syncs.append(datetime.fromtimestamp(store.now_ms() / 1000, tz=ZONE).strftime("%H:%M:%S") + f"/{days}")
        return {}

    monkeypatch.setattr(garmin_connect, "sync", fake_sync)
    monkeypatch.setenv("PULSO_GARMIN_TOKENS", str(tmp_path))
    settings = config.Settings(db_path=db, tz=TZ, anthropic_api_key=None)
    _drive(monkeypatch, settings, datetime(2026, 10, 16, 8, 0, tzinfo=ZONE), datetime(2026, 10, 16, 14, 30, tzinfo=ZONE),
           execute={"garmin:sync", "sleep:last-night"})
    assert syncs == ["08:00:00/2", "09:00:00/1", "10:00:00/1", "11:00:00/1", "14:00:00/2"]
    syncs.clear()  # a 6-h tick inside the morning window, the night still missing
    _drive(monkeypatch, settings, datetime(2026, 10, 16, 4, 0, tzinfo=ZONE), datetime(2026, 10, 16, 12, 0, tzinfo=ZONE),
           execute={"garmin:sync"})
    assert syncs == ["04:00:00/2", "05:00:00/1", "06:00:00/1", "07:00:00/1", "08:00:00/1", "09:00:00/1",
                     "10:00:00/2", "11:00:00/1"]
