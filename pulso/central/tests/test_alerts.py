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


def _steps_counter(conn, now, spm, minutes=40):
    """What live ingest gives: the face's cumulative daily step counter every 5 min (no steps_pm)."""
    t0 = now - minutes * MIN
    store.insert_samples(conn, "steps_day_total", M.SRC_CIQ_FACE,
                         [(t0 + k * 5 * MIN, 3000.0 + spm * 5 * k) for k in range(minutes // 5 + 1)])


def test_sustained_arousal_alert_with_cooldown():
    conn = store.connect(":memory:")
    a = _day()
    now = a + 15 * 60 * MIN
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 2.6) for k in range(10)])
    _steps_counter(conn, now + 15 * MIN, 0)  # sitting: the counter does not move
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


def test_no_arousal_alert_while_running_with_only_live_data():
    """F6: live uploads carry no steps_pm, only the cumulative counter (and ENMO from the capture
    app); a run is movement, not stillness, and no movement data at all is not stillness either."""
    conn = store.connect(":memory:")
    now = _day() + 18 * 60 * MIN + 25 * MIN
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 14.0) for k in range(10)])
    assert alerts.evaluate(conn, now, TZ) == []  # no movement data at all
    _steps_counter(conn, now, 165)
    assert alerts.evaluate(conn, now, TZ) == []  # running, seen through the daily counter

    conn = store.connect(":memory:")
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 14.0) for k in range(10)])
    store.insert_samples(conn, M.ENMO, M.SRC_CIQ_CAPTURE, [(now - 600_000 + k * 1000, 180.0) for k in range(600)])
    assert alerts.evaluate(conn, now, TZ) == []  # capture app's accelerometer says moving

    conn = store.connect(":memory:")
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 14.0) for k in range(10)])
    _steps_counter(conn, now, 0)
    store.upsert_event(conn, Event("mark:w:1:0", "note", now - 30 * MIN, now - 30 * MIN, "start:treino", "watch:cap",
                                   {"kind": "cap"}))
    assert alerts.evaluate(conn, now, TZ) == []  # TREINO session open (strength work: still, but exercise)
    store.upsert_event(conn, Event("mark:w:2:0", "note", now - 15 * MIN, now - 15 * MIN, "stop:user", "watch:cap",
                                   {"kind": "cap"}))
    assert [f.kind for f in alerts.evaluate(conn, now, TZ)] == ["agora"]


def test_switch_to_treino_in_the_same_second_counts_as_exercise():
    """verify N2: a mode switch on the watch (FOCO -> TREINO) writes 'stop:switch' and 'start:treino'
    with the same timestamp, the stop stored first; the session that is open is the TREINO one."""
    conn = store.connect(":memory:")
    now = _day() + 18 * 60 * MIN + 25 * MIN
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 14.0) for k in range(10)])
    _steps_counter(conn, now, 0)  # strength / indoor bike: the counter barely moves
    t = now - 30 * MIN
    for eid, at, title in (("mark:w:1:0", t - 3600_000, "start:foco"), ("mark:w:7:0", t, "stop:switch rr=1 ok=1"),
                           ("mark:w:8:0", t, "start:treino")):
        store.upsert_event(conn, Event(eid, "note", at, at, title, "watch:cap", {"kind": "cap"}))
    assert alerts._exercising(conn, now)
    assert alerts.evaluate(conn, now, TZ) == []
    # switching back to FOCO the same way closes the exercise
    store.upsert_event(conn, Event("mark:w:9:0", "note", now - 5 * MIN, now - 5 * MIN, "stop:switch rr=1 ok=1",
                                   "watch:cap", {"kind": "cap"}))
    store.upsert_event(conn, Event("mark:w:10:0", "note", now - 5 * MIN, now - 5 * MIN, "start:foco", "watch:cap",
                                   {"kind": "cap"}))
    assert not alerts._exercising(conn, now)


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
    docs = summary.documents(conn, TZ, today=date(2026, 10, 5), days=7)
    assert [d.key for d in docs] == ["hoje", "dias", "semanas"]
    hoje, dias, semanas = (d.markdown for d in docs)
    assert hoje.splitlines()[2].startswith("Atualizado em:") and "\nALERTA: " in hoje
    assert "| 2026-10-05 |" in hoje and "## Como ler" in hoje
    assert "## 2026-10-05 (seg)" in dias and "Revisão de prazo" in dias
    assert "## Semanas (ISO)" in semanas and "2026-S41" in semanas
    p = summary.write_local(docs, TZ, folder=tmp_path)
    assert (p / "pulso-hoje.md").read_text(encoding="utf-8") == hoje
    assert len(list(tmp_path.glob("pulso-*-*.md"))) == 3


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
    assert "Ana" not in md and "Bruno" not in md and "Pessoa" in md
    md2 = summary.build(conn, TZ, today=date(2026, 10, 7), days=1, names=True)
    assert "Ana Souza" in md2


def test_alert_level_line():
    from pulso.export import summary

    conn = store.connect(":memory:")
    now = _day() + 15 * 60 * MIN
    assert summary.alert_level(conn, now)[0] == "NENHUM"
    store.put_artefact(conn, "sleep:last", "latest", {"debt_min": 400, "hrv_status": "normal"})
    assert summary.alert_level(conn, now)[0] == "ATENÇÃO"
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(now - k * MIN, 2.6) for k in range(10)])
    _steps_counter(conn, now, 0)
    alerts.evaluate(conn, now, TZ)
    lvl, why = summary.alert_level(conn, now + MIN)
    assert lvl == "ALTO" and "Ativação alta" in why


def test_meeting_alert_title_is_cut_between_words():
    """review: "Feedback com Fernanda Albuquerque"[:28] left "Albuqu", which the pseudonymizer no longer
    recognises, in the alert text that goes to the exported Docs."""
    assert alerts._short("Feedback com Fernanda Albuquerque") == "Feedback com Fernanda…"
    assert alerts._short("weekly-sync-fernanda-albuquerque-q4") == "weekly-sync-fernanda…"
    assert alerts._short("Curta") == "Curta"
    assert alerts._short("Supercalifragilisticexpialidocious") == "…"
