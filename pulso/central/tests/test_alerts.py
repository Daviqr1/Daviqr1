from datetime import date

import numpy as np

from pulso import alerts, analysis, store
from pulso import metrics as M
from pulso.face import face_state
from pulso.store import Event

TZ = "America/Sao_Paulo"
MIN = 60_000


def _day(d=date(2026, 10, 7)):
    a, _ = analysis.local_day_bounds(d, TZ)
    return a


def test_sustained_arousal_alert_with_cooldown():
    conn = store.connect(":memory:")
    a = _day()
    now = a + 15 * 60 * MIN
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 2.6) for k in range(10)])
    fired = alerts.evaluate(conn, now, TZ)
    assert [f.kind for f in fired] == ["agora"]
    st = face_state(conn, now, TZ)
    assert st["al"]["m"].startswith("Ativação alta") and 0 <= st["al"]["i"] < 2**31
    # cooldown: 10 min later nothing new
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now + 10 * MIN - k * MIN, 2.6) for k in range(10)])
    assert alerts.evaluate(conn, now + 10 * MIN, TZ) == []


def test_no_arousal_alert_when_moving():
    conn = store.connect(":memory:")
    now = _day() + 15 * 60 * MIN
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 2.6) for k in range(10)])
    store.insert_samples(conn, M.STEPS_PM, "fit", [(now - k * MIN, 120.0) for k in range(10)])
    assert alerts.evaluate(conn, now, TZ) == []


def test_heavy_meeting_and_back_to_back_alerts():
    conn = store.connect(":memory:")
    a = _day()
    now = a + 13 * 60 * MIN
    # history: "Revisão de prazo" meetings always weighed a lot
    for k in range(3):
        store.put_artefact(conn, "meeting_response", f"old{k}", {"title": "Revisão de prazo", "during": 1.4, "attendees": []})
    store.upsert_event(conn, Event("c1", "calendar", now + 10 * MIN, now + 40 * MIN, "Revisão de prazo", "ics", {}))
    store.upsert_event(conn, Event("c2", "calendar", now + 45 * MIN, now + 75 * MIN, "Daily", "ics", {}))
    store.upsert_event(conn, Event("c3", "calendar", now + 80 * MIN, now + 110 * MIN, "1:1", "ics", {}))
    kinds = sorted(f.kind for f in alerts.evaluate(conn, now, TZ))
    assert kinds == ["reuniao", "sequencia"]
    # same meeting does not alert twice
    assert all(f.kind != "reuniao" for f in alerts.evaluate(conn, now + 2 * MIN, TZ))


def test_morning_sleep_alert_once():
    conn = store.connect(":memory:")
    d = date(2026, 10, 7)
    now = _day(d) + 7 * 60 * MIN
    store.put_artefact(conn, "sleep:last", "latest", {"wake_day": d.isoformat(), "debt_min": 420, "hrv_status": "below"})
    fired = alerts.evaluate(conn, now, TZ)
    assert [f.kind for f in fired] == ["sono"] and "HRV" in fired[0].detail
    assert alerts.evaluate(conn, now + 30 * MIN, TZ) == []
    assert np.isfinite(len(alerts.recent(conn, 0)))


def test_summary_markdown(tmp_path):
    import sys

    from pulso import jobs
    from pulso.export import summary

    sys.path.insert(0, "tests")
    from test_analysis import _fill_days

    conn = store.connect(":memory:")
    _fill_days(conn, date(2026, 9, 20), 16, date(2026, 10, 5))
    jobs.run_range(conn, date(2026, 10, 3), date(2026, 10, 5), TZ)
    md = summary.build(conn, TZ, today=date(2026, 10, 5), days=7)
    assert "# Pulso — resumo de saúde e estresse" in md and "## Como ler" in md
    assert "| 2026-10-05 |" in md and "Revisão de prazo" in md
    assert "texto" not in md.lower().split("## como ler")[0]  # no transcript text leaks into the summary
    p = summary.write_local(md, TZ, folder=tmp_path)
    assert p.read_text(encoding="utf-8") == md and len(list(tmp_path.glob("pulso-saude-*.md"))) == 1


def test_summary_pseudonymises_people(tmp_path):
    from pulso.export import summary

    conn = store.connect(":memory:")
    a = _day()
    store.upsert_event(conn, Event("m1", "meeting", a + 600 * MIN, a + 630 * MIN, "1:1 com Ana Souza", "t",
                                   {"speakers": ["Ana Souza", "eu"], "attendees": ["Ana Souza", "Bruno Lima"]}))
    store.put_artefact(conn, "analysis:day", "2026-10-07", {"episodes": [
        {"start_ms": a + 600 * MIN, "end_ms": a + 620 * MIN, "peak_ms": a + 610 * MIN, "peak_z": 2.1, "kind": "mental",
         "overlaps": ["m1"]}]})
    md = summary.build(conn, TZ, today=date(2026, 10, 7), days=1)
    assert "Ana" not in md and "Pessoa" in md
    md2 = summary.build(conn, TZ, today=date(2026, 10, 7), days=1, names=True)
    assert "Ana Souza" in md2
