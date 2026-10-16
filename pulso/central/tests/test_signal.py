import math

import numpy as np
import pytest

from pulso.signal import hrv
from pulso.signal.rr import FLAG_OK, clean_rr, detect_artefacts, segment_by_gaps

from synth import inject_ectopic, inject_extra, inject_missed, rmssd, rr_series


def test_clean_series_is_untouched():
    ts, rr = rr_series(n=500, seed=1)
    res = clean_rr(ts, rr)
    assert res.artefacts.total == 0
    assert res.quality == 1.0
    np.testing.assert_allclose(res.rr_ms, rr)


@pytest.mark.parametrize(
    "inject,kind",
    [(inject_missed, "missed"), (inject_extra, "extra"), (inject_ectopic, "ectopic")],
)
def test_artefacts_are_detected_and_rmssd_recovered(inject, kind):
    ts, rr = rr_series(n=600, seed=2)
    true = rmssd(rr)
    bad_ts, bad_rr = inject(ts, rr, [100, 250, 400])
    corrupted = rmssd(bad_rr)
    res = clean_rr(bad_ts, bad_rr)
    found = getattr(res.artefacts, kind)
    assert len(found) >= 3, (kind, res.artefacts)
    fixed = rmssd(res.rr_ms)
    # corruption inflates RMSSD a lot; cleaning must bring it back within 10%
    assert abs(corrupted - true) / true > 0.3
    assert abs(fixed - true) / true < 0.10, (true, corrupted, fixed)


def test_cleaning_preserves_timeline_for_ectopic_and_extra():
    ts, rr = rr_series(n=400, seed=3)
    bad_ts, bad_rr = inject_extra(ts, rr, [150])
    res = clean_rr(bad_ts, bad_rr)
    # merges keep the timeline; only the odd long/short replacement may shift it slightly
    assert abs(res.rr_ms.sum() - rr.sum()) < 0.005 * rr.sum()


def test_physiological_gate_and_gaps():
    ts, rr = rr_series(n=300, seed=4)
    rr = rr.copy()
    rr[10] = 5000  # impossible
    ts2 = ts.copy()
    ts2[200:] += 60_000  # one minute with no beats -> new segment
    seg = segment_by_gaps(ts2, rr)
    assert seg[199] != seg[200]
    res = clean_rr(ts2, rr)
    assert res.n_rejected >= 1
    assert len(np.unique(res.segment)) == 2


def test_short_segments_rejected():
    ts, rr = rr_series(n=10)
    res = clean_rr(ts, rr)
    assert res.rr_ms.size == 0 and res.n_rejected == 10


def test_time_domain_matches_definitions():
    rr = np.array([800, 810, 790, 805, 800, 820, 780.0])
    td = hrv.time_domain(rr)
    d = np.diff(rr)
    assert td["rmssd"] == pytest.approx(np.sqrt(np.mean(d**2)))
    assert td["sdnn"] == pytest.approx(np.std(rr, ddof=1))
    assert td["mean_hr"] == pytest.approx(60000 / rr.mean())
    assert td["sd1"] == pytest.approx(td["rmssd"] / np.sqrt(2), rel=0.1)


def test_hf_peak_recovers_breathing_rate():
    # 0.25 Hz respiration = 15 breaths/min
    ts, rr = rr_series(n=400, hf_amp=40, hf_hz=0.25, noise_sd=2, seed=5)
    fd = hrv.frequency_domain(ts, rr)
    assert fd["resp_rate_rsa"] == pytest.approx(15.0, abs=1.0)
    assert fd["hf"] > fd["lf"]


def test_band_power_scale_matches_variance():
    # a pure sinusoid of amplitude A has variance A^2/2; HF power should be close to it
    A = 40.0
    ts, rr = rr_series(n=600, hf_amp=A, hf_hz=0.25, noise_sd=0.0, seed=6)
    fd = hrv.frequency_domain(ts, rr)
    assert fd["hf"] == pytest.approx(A**2 / 2, rel=0.25)


def test_lf_detected_when_present():
    ts, rr = rr_series(n=600, hf_amp=5, lf_amp=40, lf_hz=0.1, noise_sd=2, seed=7)
    fd = hrv.frequency_domain(ts, rr)
    assert fd["lf"] > fd["hf"] * 5


def test_dfa_alpha1_white_vs_brown():
    rng = np.random.default_rng(0)
    white = 900 + rng.normal(0, 30, 2000)
    brown = 900 + np.cumsum(rng.normal(0, 3, 2000))
    a_white = hrv.dfa_alpha1(white)
    a_brown = hrv.dfa_alpha1(brown)
    assert a_white == pytest.approx(0.5, abs=0.12)
    assert a_brown == pytest.approx(1.5, abs=0.2)


def test_dfa_needs_enough_beats():
    assert math.isnan(hrv.dfa_alpha1(np.full(20, 800.0)))


def test_rolling_windows_respect_quality_gates():
    ts, rr = rr_series(n=1200, seed=8)
    res = clean_rr(ts, rr)
    wins = hrv.rolling(res, window_s=120, step_s=60)
    assert len(wins) > 5
    assert all(w.coverage >= 0.8 and w.quality >= 0.9 for w in wins)
    d = wins[0].as_dict()
    assert d["rmssd"] > 0 and d["n_beats"] > 100


def test_matches_neurokit2_rmssd_if_available():
    nk = pytest.importorskip("neurokit2")
    ts, rr = rr_series(n=600, seed=9)
    peaks = np.concatenate([[0], np.cumsum(rr)]).astype(int)  # ms at 1000 Hz
    ref = nk.hrv_time(peaks, sampling_rate=1000)["HRV_RMSSD"].iloc[0]
    assert hrv.time_domain(rr)["rmssd"] == pytest.approx(ref, rel=0.02)
