from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso import analysis, cli, jobs, store
from pulso import metrics as M
from pulso.store import Event

from test_analysis import TZ, _fill_days

MIN = 60_000


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
