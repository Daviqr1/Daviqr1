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
