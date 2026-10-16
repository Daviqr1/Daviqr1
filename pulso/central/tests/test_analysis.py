from datetime import date

import numpy as np
import pytest

from pulso import analysis, store
from pulso import metrics as M
from pulso.models import additional_hr, baseline, sleep, training
from pulso.store import Event

from synth import rr_series

TZ = "America/Sao_Paulo"
MIN = 60_000


@pytest.fixture()
def conn():
    return store.connect(":memory:")


def _fill_days(conn, first: date, n_days: int, meeting_day: date, seed=0):
    rng = np.random.default_rng(seed)
    for k in range(n_days):
        d = date.fromordinal(first.toordinal() + k)
        a, b = analysis.local_day_bounds(d, TZ)
        ts = np.arange(a, b, MIN)
        hours, _ = baseline.local_hours(ts, TZ)
        asleep = (hours < 7) | (hours >= 23.5)
        steps = np.zeros(ts.size)
        for s in rng.integers(7 * 60, 22 * 60, 10):
            steps[s : s + rng.integers(5, 30)] = rng.uniform(70, 140)
        la = np.log1p(steps)
        hr = 60 + 6 * additional_hr.ema(la, 1) + 3 * additional_hr.ema(la, 3) + 3 * np.cos(2 * np.pi * (hours - 15) / 24)
        hr -= np.where(asleep, 8, 0)
        stress = 25 + rng.normal(0, 6, ts.size)
        if d == meeting_day:
            i0 = 14 * 60
            hr[i0 : i0 + 50] += 14
            stress[i0 : i0 + 50] += 35
            store.upsert_event(conn, Event("meet-1", "meeting", int(ts[i0]), int(ts[i0 + 45]), "Revisão de prazo", "test", {}))
            # RR captured during the afternoon, suppressed HRV during the meeting
            rts, rr = rr_series(n=8000, mean_rr=850, hf_amp=35, noise_sd=4, t0_ms=int(ts[i0 - 60]), seed=3)
            span = (rts >= ts[i0]) & (rts < ts[i0 + 50])
            rr = np.where(span, 700 + (rr - 850) * 0.25, rr)
            store.insert_rr(conn, M.SRC_CIQ_CAPTURE, zip(rts.tolist(), rr.round().astype(int).tolist()))
        hr += rng.normal(0, 2, ts.size)
        stress[(steps > 0) | asleep] = np.nan  # Garmin blanks stress while moving
        store.insert_samples(conn, M.HR, M.SRC_FIT, zip(ts.tolist(), hr.tolist()))
        store.insert_samples(conn, M.STEPS_PM, M.SRC_FIT, zip(ts.tolist(), steps.tolist()))
        store.insert_samples(conn, M.STRESS_GARMIN, M.SRC_GC, ((int(t), float(v)) for t, v in zip(ts[::3], stress[::3]) if np.isfinite(v)))
        sl = ts[asleep]
        conn.executemany("INSERT INTO sleep_epochs (source, ts_ms, duration_s, stage) VALUES (?, ?, 60, 'light')",
                         [(M.SRC_FIT, int(t)) for t in sl])


def test_day_pipeline_finds_meeting_episode(conn):
    first = date(2026, 9, 20)
    target = date(2026, 10, 5)
    _fill_days(conn, first, 16, target)
    res = analysis.analyze_day(conn, target, TZ)
    s = res.summary()
    assert s["minutes_with_hr"] > 1000
    assert "trailing" in " ".join(res.notes)
    assert {"additional_hr", "garmin_stress", "rr_hrv"} <= set(res.channel_r)
    mental = [e for e in res.episodes if "meet-1" in e.overlaps]
    assert mental, [e.as_dict() for e in res.episodes]
    assert mental[0].kind == "mental"
    # derived series persisted and recomputation is idempotent
    ts, z = store.series(conn, M.AROUSAL_Z, int(res.ts_ms[0]), int(res.ts_ms[-1]) + MIN, source=M.DERIVED)
    assert ts.size > 500
    analysis.analyze_day(conn, target, TZ)
    ts2, _ = store.series(conn, M.AROUSAL_Z, int(res.ts_ms[0]), int(res.ts_ms[-1]) + MIN, source=M.DERIVED)
    assert ts2.size == ts.size
    assert store.get_artefact(conn, "analysis:day", target.isoformat())["day"] == "2026-10-05"
    # RR-derived HRV dropped during the meeting
    i0 = 14 * 60
    ln = res.hrv[M.LN_RMSSD]
    assert np.nanmean(ln[i0 + 5 : i0 + 45]) < np.nanmean(ln[i0 - 50 : i0 - 5]) - 0.5


def test_day_pipeline_survives_empty_store(conn):
    res = analysis.analyze_day(conn, date(2026, 10, 5), TZ)
    assert res.episodes == [] and res.summary()["minutes_with_hr"] == 0


def test_sleep_summary_and_sri():
    t0 = 0
    ep_ts = t0 + np.arange(480) * MIN
    stages = np.array(["light"] * 100 + ["deep"] * 80 + ["awake"] * 10 + ["rem"] * 90 + ["light"] * 200)
    nights = sleep.nights_from_epochs(ep_ts, np.full(480, 60), stages)
    assert len(nights) == 1
    hr_ts = ep_ts
    hr = 60 - 8 * np.sin(np.linspace(0, np.pi, 480))  # nadir mid-night
    n = sleep.summarize_night(*nights[0], ep_ts, np.full(480, 60), stages, hr_ts, hr)
    assert n.total_sleep_min == pytest.approx(470)
    assert n.awakenings == 1
    assert n.hr_nadir == pytest.approx(52, abs=0.5)
    assert n.hr_nadir_at_frac == pytest.approx(0.5, abs=0.05)
    day = np.r_[np.ones(480), np.zeros(960)]
    assert sleep.sleep_regularity_index(np.tile(day, 3)) == pytest.approx(100)
    shifted = np.r_[day, np.roll(day, 240), day]
    assert sleep.sleep_regularity_index(shifted) < 70
    debt = sleep.sleep_debt([400, 420, float("nan"), 480])
    assert debt[-1] == pytest.approx(50 + 30 - 30)


def test_training_load_and_dfa_thresholds():
    hr = np.full(60, 150.0)
    assert training.trimp(hr, 60, 50, 190) > training.trimp(hr - 30, 60, 50, 190)
    ff = training.fitness_fatigue(np.r_[np.full(30, 100.0), np.zeros(14)])
    assert ff.form[-1] > ff.form[29]  # tapering raises form
    hrs = np.linspace(120, 175, 30)
    a1 = 1.1 - (hrs - 120) * (0.7 / 55)
    th = training.dfa_thresholds(hrs, a1)
    assert th.hr_at_075 == pytest.approx(120 + (1.1 - 0.75) / (0.7 / 55), abs=0.5)
    assert th.hr_at_050 > th.hr_at_075


def test_moving_rr_windows_do_not_feed_stress(conn):
    first = date(2026, 9, 20)
    target = date(2026, 10, 5)
    _fill_days(conn, first, 16, target)
    a, _ = analysis.local_day_bounds(target, TZ)
    # accelerometer says the wearer was walking during the first half of the meeting
    t0 = a + 14 * 60 * MIN
    rows = [(t0 + k * 1000, 150.0) for k in range(20 * 60)]
    store.insert_samples(conn, M.ENMO, M.SRC_CIQ_CAPTURE, rows)
    res = analysis.analyze_day(conn, target, TZ, write=False)
    assert any("moving" in n for n in res.notes)
