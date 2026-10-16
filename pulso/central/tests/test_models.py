import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from pulso.models import additional_hr, baseline, episodes, fusion, meetings
from pulso.store import Event

TZ = "America/Sao_Paulo"
MIN = 60_000


def day_start_ms(y=2026, m=10, d=5, tz=TZ):
    return int(datetime(y, m, d, tzinfo=ZoneInfo(tz)).astimezone(timezone.utc).timestamp() * 1000)


def test_cosinor_finds_acrophase():
    hours = np.tile(np.arange(0, 24, 0.25), 10)
    y = 60 + 8 * np.cos(2 * np.pi * (hours - 16) / 24) + np.random.default_rng(0).normal(0, 1, hours.size)
    c = baseline.cosinor(hours, y)
    assert c.acrophase_h == pytest.approx(16, abs=0.5)
    assert c.amplitude == pytest.approx(8, rel=0.15)
    assert c.r2 > 0.9


def test_hour_profile_and_expected():
    t0 = day_start_ms()
    ts = t0 + np.arange(0, 14 * 24 * 60) * MIN
    hours, weekend = baseline.local_hours(ts, TZ)
    vals = 50 + 2 * hours  # value depends only on hour
    prof = baseline.hour_profile(ts, vals, TZ)
    med, sc = prof.expected(np.array([10.5, 20.0]), np.array([False, True]))
    assert med[0] == pytest.approx(50 + 2 * 10.5, abs=1.1)
    assert med[1] == pytest.approx(50 + 2 * 20.5, abs=1.1)


def test_readiness_flags_suppressed_week():
    rng = np.random.default_rng(1)
    x = 4.0 + rng.normal(0, 0.05, 60)
    x[-7:] -= 0.25  # a bad week (e.g. crunch + bad sleep)
    days = [f"d{i}" for i in range(60)]
    r = baseline.hrv_readiness(days, x)
    assert r.status[10] == "insufficient"
    assert r.status[-1] == "below"
    assert r.status[40] == "normal"


def _synthetic_day(seed=0, mental=((600, 640), (900, 930))):
    """One awake day on a 1-min grid: activity bouts raise HR (metabolic); 'mental' windows add
    HR while sitting still. Returns ts, hr, steps, hours, truth_additional."""
    rng = np.random.default_rng(seed)
    n = 16 * 60
    t0 = day_start_ms() + 7 * 60 * MIN
    ts = t0 + np.arange(n) * MIN
    steps = np.zeros(n)
    for s in rng.integers(0, n - 30, 12):
        steps[s : s + rng.integers(5, 25)] = rng.uniform(60, 150)
    hours, _ = baseline.local_hours(ts, TZ)
    la = np.log1p(steps)
    metabolic = 6.0 * additional_hr.ema(la, 1.0) + 3.0 * additional_hr.ema(la, 3.0)
    circ = 3 * np.cos(2 * np.pi * (hours - 15) / 24)
    truth = np.zeros(n)
    for a, b in mental:
        truth[a:b] = 12.0
    hr = 62 + metabolic + circ + truth + rng.normal(0, 2.0, n)
    return ts, hr, steps, hours, truth


def test_additional_hr_isolates_mental_load():
    ts, hr, steps, hours, truth = _synthetic_day()
    model = additional_hr.fit(hr, steps, hours)
    add = model.additional(hr, steps, hours)
    m = truth > 0
    # inside the mental windows additional HR is ~ +12 bpm above the rest of the day
    assert np.median(add[m]) - np.median(add[~m]) == pytest.approx(12, abs=3)
    # movement alone does not produce additional HR
    moving = (steps > 0) & ~m
    assert abs(np.median(add[moving]) - np.median(add[~m & ~moving])) < 3


def test_fusion_recovers_latent_and_ranks_noise():
    rng = np.random.default_rng(3)
    n = 2000
    x = np.zeros(n)
    phi = np.exp(-1 / 20)
    for t in range(1, n):
        x[t] = phi * x[t - 1] + rng.normal(0, np.sqrt(1 - phi**2))
    good = x + rng.normal(0, 0.4, n)
    noisy = x + rng.normal(0, 1.5, n)
    sparse = np.where(np.arange(n) % 3 == 0, x + rng.normal(0, 0.5, n), np.nan)
    ts = np.arange(n) * MIN
    res = fusion.fuse(ts, [fusion.Channel("good", good), fusion.Channel("noisy", noisy), fusion.Channel("sparse", sparse)])
    corr = np.corrcoef(res.mean, x)[0, 1]
    assert corr > 0.9
    assert res.r["good"] < res.r["noisy"]
    assert np.all(res.sd > 0) and np.all(res.sd < 1)
    # the fused estimate beats every single channel
    for ch in (good, noisy):
        assert np.mean((res.mean - x) ** 2) < np.nanmean((ch - x) ** 2)


def test_fusion_handles_all_missing_steps():
    ts = np.arange(100) * MIN
    z = np.full(100, np.nan)
    z[40:60] = 3.0
    res = fusion.fuse(ts, [fusion.Channel("a", z)])
    assert res.mean[50] > 1.5
    assert abs(res.mean[0]) < 0.5 and res.sd[0] > res.sd[50]


def test_episodes_detect_and_classify():
    n = 300
    ts = np.arange(n) * MIN
    z = np.zeros(n)
    z[50:70] = 2.5      # mental (still)
    z[150:175] = 2.2    # physical (walking)
    z[200:201] = 4.0    # single-minute blip -> ignored
    act = np.zeros(n)
    act[150:175] = 110
    eps = episodes.detect(ts, z, act)
    assert len(eps) == 2
    assert eps[0].kind == "mental" and eps[1].kind == "physical"
    assert eps[0].start_ms == 50 * MIN and eps[0].end_ms == 70 * MIN
    episodes.link_events(eps, [Event("m1", "meeting", 55 * MIN, 80 * MIN, "1:1", "test", {})])
    assert eps[0].overlaps == ["m1"] and eps[1].overlaps == []


def test_changepoints_optional():
    z = np.concatenate([np.zeros(60), np.full(60, 2.0)])
    cps = episodes.changepoints(z)
    if cps:  # ruptures installed
        assert any(abs(c - 60) <= 2 for c in cps)


def test_meeting_response_and_matched_controls():
    rng = np.random.default_rng(5)
    days = 30
    t0 = day_start_ms(2026, 9, 1)
    ts = t0 + np.arange(days * 24 * 60) * MIN
    z = rng.normal(0, 0.3, ts.size)
    hours, weekend = baseline.local_hours(ts, TZ)
    z += 0.5 * np.sin(2 * np.pi * (hours - 9) / 24)  # time-of-day effect present everywhere
    meets, busy = [], []
    for d in range(days):
        day0 = t0 + d * 24 * 60 * MIN
        if d % 3 == 0 and not weekend[d * 24 * 60 + 600]:
            s = day0 + 14 * 60 * MIN
            e = s + 45 * MIN
            i0 = (s - t0) // MIN
            z[i0 : i0 + 45] += 1.0  # true meeting effect
            ev = Event(f"m{d}", "meeting", s, e, "planning", "test", {})
            meets.append(ev)
            busy.append(ev)
    r = meetings.response(ts, z, meets[0].id, meets[0].start_ms, meets[0].end_ms)
    assert r.during == pytest.approx(1.0, abs=0.35)
    assert r.coverage == pytest.approx(1.0)
    cmp = meetings.matched_controls(ts, z, meets, busy, TZ)
    assert cmp.n_meetings >= 5
    assert cmp.mean_diff == pytest.approx(1.0, abs=0.3)
    assert cmp.p_value < 0.05 and cmp.ci_low > 0.5


def test_no_effect_when_meeting_is_just_the_hour():
    rng = np.random.default_rng(6)
    t0 = day_start_ms(2026, 9, 1)
    ts = t0 + np.arange(21 * 24 * 60) * MIN
    hours, _ = baseline.local_hours(ts, TZ)
    z = rng.normal(0, 0.3, ts.size) + np.where((hours >= 14) & (hours < 15), 1.0, 0.0)
    meets = [Event(f"m{d}", "meeting", t0 + d * 1440 * MIN + 14 * 60 * MIN, t0 + d * 1440 * MIN + 14 * 60 * MIN + 45 * MIN,
                   "x", "t", {}) for d in range(0, 21, 4)]
    cmp = meetings.matched_controls(ts, z, meets, meets, TZ)
    assert abs(cmp.mean_diff) < 0.3 and cmp.p_value > 0.05


def test_segment_effects_and_lag():
    rng = np.random.default_rng(7)
    step = 10_000  # 10-s grid for this test
    n = 6 * 60 * 6
    ts = np.arange(n) * step
    z = rng.normal(0, 0.2, n)
    segs = []
    t = 0
    i = 0
    while t < ts[-1] - 120_000:
        spk = ["Ana", "Bruno", "eu"][i % 3]
        dur = 60_000
        tags = ["prazo"] if spk == "Bruno" else ["tecnico"]
        segs.append({"event_id": "m1", "start_ms": t, "end_ms": t + dur, "speaker": spk, "tags": tags})
        if spk == "Bruno":
            a = (t + 30_000) // step
            z[a : a + dur // step] += 1.0  # response lags 30 s
        t += dur
        i += 1
    lag = meetings.estimate_lag(ts, z, [s for s in segs if s["speaker"] == "Bruno"], step_ms=step)
    assert lag == pytest.approx(30, abs=10)
    eff = meetings.segment_effects(ts, z, segs, lag_s=lag)
    top = eff["speaker"][0]
    assert top["label"] == "Bruno" and top["ci_low"] > 0.3
    assert eff["tags"][0]["label"] == "prazo"


def test_nonmetabolic_hrv_explains_movement():
    from pulso.models import nonmetabolic_hrv

    rng = np.random.default_rng(9)
    n = 400
    enmo = rng.uniform(0, 40, n)
    hours = rng.uniform(8, 20, n)
    ln = 3.8 - 0.25 * np.log1p(enmo) + rng.normal(0, 0.05, n)
    m = nonmetabolic_hrv.fit(ln, enmo, hours)
    assert m is not None and m.beta[1] == pytest.approx(-0.25, abs=0.05)
    # same movement, RMSSD 0.3 lower than expected -> clearly positive (aroused) z
    z = m.z(np.array([3.8 - 0.25 * np.log1p(10) - 0.3]), np.array([10.0]), np.array([14.0]))
    assert z[0] > 3
    assert nonmetabolic_hrv.fit(ln[:10], enmo[:10], hours[:10]) is None
