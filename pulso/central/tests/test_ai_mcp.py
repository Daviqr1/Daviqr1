import asyncio
import json
from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from pulso import analysis, config, jobs, store
from pulso import metrics as M
from pulso.ai import facts as F
from pulso.ai import report, tagger
from pulso.face import face_state
from pulso.store import Event

from test_analysis import TZ, _fill_days

MIN = 60_000


class FakeClient:
    """Stands in for anthropic.Anthropic(): records requests, returns canned JSON."""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        body = self.payloads.pop(0)
        if callable(body):
            body = body(kw)
        return SimpleNamespace(
            stop_reason="end_turn", model=kw["model"], stop_details=None,
            content=[SimpleNamespace(type="text", text=json.dumps(body, ensure_ascii=False))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50, cache_read_input_tokens=0),
        )


@pytest.fixture(scope="module")
def analysed():
    conn = store.connect(":memory:")
    _fill_days(conn, date(2026, 9, 20), 16, date(2026, 10, 5))
    jobs.run_range(conn, date(2026, 10, 4), date(2026, 10, 5), TZ)
    return conn


def test_number_extraction_ptbr():
    assert F.extract_numbers("pico 2,4 z às 14:32, 3 episódios, -0,5") == [2.4, 3.0, -0.5]
    assert F.extract_times("às 14:32 e 9h05") == ["14:32", "09:05"]
    assert F.extract_numbers("[F12] em 2026-10-05") == []


def test_gate_accepts_cited_numbers_and_rejects_invented():
    book = F.FactBook()
    f1 = book.add("episódio", "episódio mental das 14:00 às 14:50, pico 2,4 z às 14:32")
    assert F.gate("Pico de 2,4 z às 14:32.", [f1], book) == []
    assert F.gate("Pico de 2,4 z às 14:32.", [], book)  # not cited -> fails
    probs = F.gate("Pico de 3,1 z às 15:10.", [f1], book)
    assert any("3,10" in p for p in probs) and any("15:10" in p for p in probs)
    assert F.gate("algo", ["F99"], book)


@pytest.fixture
def adversarial_book():
    book = F.FactBook()
    ids = [book.add("sono", "noite que terminou em 2026-10-15: 6,5 h de sono, HRV 42 ms (normal)."),
           book.add("dia", "2026-10-15: ativação média acordado 0,42 z; 35 minutos acima de 1,5 z; 2 episódios."),
           book.add("episódio", "2026-10-15 episódio mental das 14:00 às 14:50, pico 2,4 z às 14:32, área 45,3 z·min"),
           book.add("código", "1 entre 22h e 5h; sono de 7h30 na noite anterior.")]
    return book, ids


@pytest.mark.parametrize("text", [
    "A HRV caiu para 43 ms.",                       # hallucinated value with a unit
    "A HRV caiu para 25ms.",                        # unit glued to the number used to hide it
    "Você dormiu 4h.",                              # glued hour/duration
    "Ativação de 2,8z por 90min.",
    "Às 16h a ativação subiu.",                     # clock hour without minutes
    "Em 2026-10-11 você dormiu 6,5 h.",             # wrong ISO date
    "No dia 16/10 você dormiu 6,5 h.",              # wrong dd/mm date
    "Em 16 de outubro você dormiu 6,5 h.",
    "Você teve 3 episódios.",                       # wrong small count (was whitelisted)
    "Você teve três episódios.",
    "Você dormiu 7 h.",                             # an integer is not a rounding of 6,5
    "Área de 45 z·min.",                            # nor of 45,3
    "Pico de 2,43 z.",                              # more precision than the fact
    "Dormiu 6h45min.",
    "Na terça você dormiu 6,5 h.",                  # 2026-10-15 is a Thursday
    "Na quarta-feira a HRV foi 42 ms.",
    "Aos sábados você dorme 6,5 h.",
    "Você teve onze episódios.",                    # number words beyond "dez"
    "Vinte minutos acima de 1,5 z.",
    "Quinze minutos acima de 1,5 z.",
    "Trinta e seis minutos acima de 1,5 z.",        # 36, not 30 and 6
    "Cem minutos acima de 1,5 z.",
])
def test_gate_rejects_units_dates_and_small_counts(adversarial_book, text):
    book, ids = adversarial_book
    assert F.gate(text, ids, book), text


@pytest.mark.parametrize("text", [
    "A HRV ficou em 42 ms.", "HRV de 42ms.", "Você dormiu 6,5 h em 2026-10-15.", "Dormiu 6,5h em 15/10.",
    "Em 15 de outubro: 2 episódios.", "Dois episódios, pico de 2,4 z às 14:32.", "Pico por volta das 14h.",
    "Entre 22h e 5h.", "Sono de 7h30.", "Ativação caiu 0,4 z.", "35 minutos acima de 1,5 z.",
    "Na quinta-feira você dormiu 6,5 h.", "Na quinta, dois episódios.", "Trinta e cinco minutos acima de 1,5 z.",
    "Pela quarta vez, 2 episódios.",
])
def test_gate_keeps_legit_formatting_when_in_the_facts(adversarial_book, text):
    book, ids = adversarial_book
    assert F.gate(text, ids, book) == [], text


def test_gate_checks_against_the_cited_facts_only(adversarial_book):
    book, (sono, dia, ep, cod) = adversarial_book
    assert F.gate("2 episódios", [dia], book) == []
    assert F.gate("2 episódios", [sono], book)            # 2 is not in the cited fact
    assert F.gate("em 2026-10-15", [cod], book)          # the date is in other facts, not in the cited one
    assert F.gate("3/5 das marcações", [book.add("validação", "3 de 5 marcações caíram em episódios.")], book) == []
    assert F.gate("na quinta", [cod], book)                # no date in the cited fact: no weekday either
    weekly = book.add("reunião", 'reunião "1:1 de terça" em 2026-10-13 (terça-feira) às 10:00: durante 0,80 z.')
    assert F.gate("A 1:1 de terça pesou 0,80 z.", [weekly], book) == []
    assert F.gate("A 1:1 de quinta pesou 0,80 z.", [weekly], book)


def test_facts_from_pipeline(analysed):
    book = F.build(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ)
    topics = {f.topic for f in book.facts}
    assert {"dia", "episódio", "reunião"} <= topics
    assert any("Revisão de prazo" in f.text for f in book.facts)


def test_report_gate_filters_hallucination(analysed):
    book = F.build(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ)
    ep = next(f for f in book.facts if f.topic == "episódio" and "Revisão" in f.text)
    t = ep.times[0]

    def answer(kw):
        assert kw["fallbacks"] == "default" and kw["output_config"]["format"]["type"] == "json_schema"
        assert kw["model"] == "claude-opus-5-5"
        return {
            "headline": "A revisão de prazo foi o pico do dia",
            "insights": [
                {"title": "Pico na revisão de prazo", "body": f"O episódio começou às {t}.", "evidence": [ep.id], "confidence": "alta"},
                {"title": "Inventado", "body": "Sua ativação subiu 7,7 z às 03:33.", "evidence": [ep.id], "confidence": "alta"},
            ],
            "recommendations": [{"action": "Proteger um intervalo depois da revisão", "evidence": [ep.id]},
                                {"action": "Proteger 30 min depois da revisão", "evidence": [ep.id]}],
            "missing_data": ["transcrição etiquetada", "HRV de 19 noites"],
            "face_line": "Revisão de prazo pesou hoje",
        }

    client = FakeClient([answer])
    rep = report.generate(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ, settings=config.Settings(), client=client,
                          today=date(2026, 10, 6))
    assert [i["title"] for i in rep["insights"]] == ["Pico na revisão de prazo"]
    assert rep["rejected"] and rep["rejected"][0]["title"] == "Inventado"
    assert [r["action"] for r in rep["recommendations"]] == ["Proteger um intervalo depois da revisão"]  # "30 min" is not a fact
    assert rep["missing_data"] == ["transcrição etiquetada"]  # missing_data goes through the gate too
    assert "Descartado pela verificação" in rep["markdown"]
    # the scheduler's 06:30 report covers yesterday: its line is for today's face
    assert store.get_artefact(analysed, "insight:face", "2026-10-06")["text"] == "Revisão de prazo pesou hoje"
    assert store.get_artefact(analysed, "insight:face", "2026-10-05") is None


def test_face_shows_the_morning_report_line():
    """The daily report runs for yesterday (scheduler); face_state reads today's key."""
    conn = store.connect(":memory:")
    y, today = date(2026, 10, 15), date(2026, 10, 16)
    store.put_artefact(conn, "analysis:day", y.isoformat(), {"arousal_mean_awake": 0.42, "arousal_minutes_above_1_5": 35, "episodes": []})
    answer = {"headline": "Dia calmo", "insights": [], "recommendations": [], "missing_data": [], "face_line": "Dia calmo: 0,42 z"}
    report.generate(conn, y, y, TZ, settings=config.Settings(), client=FakeClient([answer]), today=today)
    a, _ = analysis.local_day_bounds(today, TZ)
    assert face_state(conn, a + 7 * 60 * MIN, TZ)["msg"] == "Dia calmo: 0,42 z"
    # a re-run of an old period never replaces today's line
    old = date(2026, 9, 1)
    store.put_artefact(conn, "analysis:day", old.isoformat(), {"arousal_mean_awake": 0.1, "arousal_minutes_above_1_5": 0, "episodes": []})
    other = answer | {"face_line": "Setembro: 0,1 z"}
    report.generate(conn, old, old, TZ, settings=config.Settings(), client=FakeClient([other]), today=today)
    assert face_state(conn, a + 8 * 60 * MIN, TZ)["msg"] == "Dia calmo: 0,42 z"
    assert report.face_day(today, today) == today


def test_face_line_is_checked_as_shown():
    book = F.FactBook()
    book.add("sono", "6,5 h de sono, HRV 42 ms.")
    out = {"headline": "", "insights": [], "recommendations": [], "missing_data": [],
           "face_line": "Sono curto ontem, recupere hoje com calma: HRV 42 ms"}  # > 48 chars
    rep = report.verify(out, book)
    assert rep["face_line"] and len(rep["face_line"]) <= 48 and rep["face_line"].endswith("…")
    assert report.verify(out | {"face_line": "HRV 43 ms"}, book)["face_line"] is None


def test_sleep_fact_omits_missing_values_instead_of_zero():
    conn = store.connect(":memory:")
    d = date(2026, 10, 15)
    night = {"total_sleep_min": 400, "efficiency": None, "deep_min": 60, "rem_min": 80, "awakenings": 2, "hr_nadir": None,
             "hr_nadir_at_frac": None, "debt_min": 30, "hrv_ms": 40, "hrv_status": "normal"}
    store.put_artefact(conn, "sleep:night", d.isoformat(), night)
    text = next(f.text for f in F.build(conn, d, d, TZ).facts if f.topic == "sono")
    assert "0 %" not in text and "da noite" not in text and "eficiência" not in text
    assert "2026-10-15 (quinta-feira)" in text   # the weekday is in the fact: the model copies it, never works it out
    store.put_artefact(conn, "sleep:night", d.isoformat(), night | {"hr_nadir": 48, "hr_nadir_at_frac": 0.35, "efficiency": 0.9})
    text = next(f.text for f in F.build(conn, d, d, TZ).facts if f.topic == "sono")
    assert "FC mínima 48 bpm a 35 % da noite" in text and "eficiência 90 %" in text


def test_report_facts_compute_the_meeting_summary_for_their_range():
    """The scheduler only computes meetings:summary for the last 3 days; a report for any other
    range must still get the matched-control / back-to-back facts."""
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
    assert store.get_artefact(conn, "meetings:summary", f"{d}..{d}") is None
    book = F.build(conn, d, d, TZ)
    assert any("coladas na anterior" in f.text for f in book.facts)
    assert store.get_artefact(conn, "meetings:summary", f"{d}..{d}") is not None


def test_report_facts_never_reuse_a_summary_from_earlier_in_the_day():
    """A mid-day `fatos` (MCP) or `pulso report --days 1` stores a 'D..D' summary with the morning
    meetings only; the next morning's report for D..D must see the afternoon ones too."""
    conn = store.connect(":memory:")
    d = date(2026, 10, 7)
    a, b = analysis.local_day_bounds(d, TZ)
    z = np.zeros((b - a) // MIN)
    slots = [(9 * 60, 30, False), (9 * 60 + 35, 30, True), (10 * 60 + 10, 30, True), (10 * 60 + 45, 30, True),
             (14 * 60, 30, False), (16 * 60, 30, False), (17 * 60, 30, False), (8 * 60, 20, False)]
    for m0, dur, b2b in slots:
        z[m0 : m0 + dur] = 1.5 if b2b else 0.3
    store.upsert_samples(conn, M.AROUSAL_Z, M.DERIVED, [(a + i * MIN, float(v)) for i, v in enumerate(z)])

    def meetings(morning: bool):
        for k, (m0, dur, _) in enumerate(slots):
            if (m0 < 11 * 60) == morning:
                store.upsert_event(conn, Event(f"m{k}", "meeting", a + m0 * MIN, a + (m0 + dur) * MIN, f"reunião {k}", "t", {}))

    meetings(morning=True)
    midday = F.build(conn, d, d, TZ)
    assert not any("coladas na anterior" in f.text for f in midday.facts)   # only 2 meetings with a break so far
    conn.execute("UPDATE artefacts SET created_ms = ? WHERE kind = 'meetings:summary'", (a + 11 * 60 * MIN,))  # stored at 11:00
    meetings(morning=False)
    book = F.build(conn, d, d, TZ)
    assert any("coladas na anterior" in f.text and "n = 3" in f.text and "(n = 5)" in f.text for f in book.facts)
    assert sum(1 for f in book.facts if f.topic == "reunião") == 8
    assert store.get_artefact(conn, "meetings:summary", f"{d}..{d}")["n_meetings"] == 8


def test_mcp_meetings_recompute_a_summary_stored_before_the_period_ended():
    pytest.importorskip("mcp")
    from pulso import mcp_server

    conn = store.connect(":memory:")
    d = date(2026, 10, 7)
    a, b = analysis.local_day_bounds(d, TZ)
    store.upsert_event(conn, Event("m1", "meeting", a + 9 * 60 * MIN, a + 10 * 60 * MIN, "reunião", "t", {}))
    stale = {"n_meetings": 0, "vs_matched_controls": {"n": 0}, "marca": "antigo"}
    store.put_artefact(conn, "meetings:summary", f"{d}..{d}", stale)
    conn.execute("UPDATE artefacts SET created_ms = ? WHERE kind = 'meetings:summary'", (a + 8 * 60 * MIN,))
    srv = mcp_server.build(conn, TZ)

    def call():
        return json.dumps(asyncio.run(srv.call_tool("reunioes", {"inicio": d.isoformat(), "fim": d.isoformat()})),
                          default=str, ensure_ascii=False)

    assert "antigo" not in call()                                     # stored at 08:00 of that day: recomputed
    store.put_artefact(conn, "meetings:summary", f"{d}..{d}", stale)  # stored after the day ended: reused
    assert "antigo" in call()


def test_report_dry_run_needs_no_client(analysed):
    out = report.generate(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ, dry_run=True)
    assert out["facts"] and out["markdown"].startswith("```")


def test_tagger_pseudonymises_and_stores(analysed, monkeypatch):
    monkeypatch.setenv("PULSO_ME", "eu,davi")
    ev = store.events(analysed, 0, 2**62, kinds=["meeting"])[0]
    analysed.execute("DELETE FROM transcript_segments WHERE event_id = ?", (ev.id,))
    analysed.executemany("INSERT INTO transcript_segments VALUES (?, ?, ?, ?, ?, ?, NULL)", [
        (ev.id, 0, ev.start_ms, ev.start_ms + 30_000, "Ana Souza", "Precisamos fechar sexta.", ),
        (ev.id, 1, ev.start_ms + 30_000, ev.start_ms + 60_000, "Davi", "Não fecha, tem bloqueio.", ),
        (ev.id, 2, ev.start_ms + 60_000, ev.start_ms + 90_000, "Ana Souza", "Davi, você consegue?", ),
    ])

    def answer(kw):
        user = kw["messages"][0]["content"]
        assert "Ana Souza" not in user and "|S1|" in user and "|EU|" in user
        assert kw["output_config"]["effort"] == "low"
        return {"segments": [{"i": 0, "tags": ["prazo", "cobrança"], "intensity": 2, "to_me": False},
                             {"i": 1, "tags": ["bloqueio"], "intensity": 1, "to_me": False},
                             {"i": 2, "tags": ["cobrança"], "intensity": 2, "to_me": True}],
                "topics": ["prazo da entrega"], "summary": "Cobrança de prazo."}

    res = tagger.tag_range(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ, settings=config.Settings(),
                           client=FakeClient([answer]))
    assert res[0]["tagged"] == 3
    rows = analysed.execute("SELECT idx, tags FROM transcript_segments WHERE event_id = ? ORDER BY idx", (ev.id,)).fetchall()
    assert json.loads(rows[0]["tags"]) == ["prazo", "cobrança", "intensidade_2"]
    assert "pergunta_para_mim" in json.loads(rows[2]["tags"])
    attrs = json.loads(analysed.execute("SELECT attrs FROM events WHERE id = ?", (ev.id,)).fetchone()[0])
    assert attrs["ai"]["topics"] == ["prazo da entrega"]


def _alert(conn, aid: str, text: str, created_ms: int) -> None:
    store.put_artefact(conn, "alert", aid, {"id": aid, "kind": aid.split(":")[0], "at_ms": created_ms, "text": text, "detail": ""})
    conn.execute("UPDATE artefacts SET created_ms = ? WHERE kind = 'alert' AND key = ?", (created_ms, aid))


def test_face_alert_notifies_every_new_alert_once():
    """The watch notifies when `i` changes and remembers only the last `i` it showed."""
    import zlib

    conn = store.connect(":memory:")
    now = analysis.local_day_bounds(date(2026, 10, 7), TZ)[0] + 13 * 60 * MIN
    _alert(conn, "reuniao:c1", "Revisão de prazo às 13:10: costuma pesar", now)
    _alert(conn, "sequencia:2026-10-07", "3 reuniões coladas hoje. Encaixe pausas de 10 min", now)  # same evaluation
    st = face_state(conn, now + MIN, TZ)["al"]
    assert st["m"].endswith(" (+1)") and len(st["m"]) <= 60
    first = st["i"]
    _alert(conn, "agora:1", "Ativação alta há 10 min. 1 min de respiração lenta?", now + 10 * MIN)
    st = face_state(conn, now + 11 * MIN, TZ)["al"]
    assert st["i"] != first and st["i"] == zlib.crc32(b"agora:1") & 0x7FFFFFFF and st["m"].endswith("(+2)")
    # the two older alerts leave the 30-min window: same newest alert, no second notification
    st = face_state(conn, now + 35 * MIN, TZ)["al"]
    assert st["i"] == zlib.crc32(b"agora:1") & 0x7FFFFFFF and st["m"] == "Ativação alta há 10 min. 1 min de respiração lenta?"
    assert "al" not in face_state(conn, now + 41 * MIN, TZ)


def test_daily_table_and_correlation(analysed):
    rows = jobs.daily_table(analysed, date(2026, 10, 4), date(2026, 10, 5), TZ)
    assert rows[-1]["meetings"] == 1 and rows[-1]["episodes"] >= 1
    synthetic = [{"a": i, "b": 2 * i + (i % 3)} for i in range(20)]
    r = jobs.correlate_daily(synthetic, "a", "b")
    assert r["rho"] > 0.9 and r["p"] < 0.01
    lagged = jobs.correlate_daily([{"a": i % 5, "b": (i - 1) % 5} for i in range(30)], "a", "b", lag_days=1)
    assert lagged["rho"] == pytest.approx(1.0)


def test_mcp_tools(analysed):
    pytest.importorskip("mcp")
    from pulso import mcp_server

    srv = mcp_server.build(analysed, TZ)

    async def go():
        names = {t.name for t in await srv.list_tools()}
        assert {"inventario", "serie", "dia", "tabela_diaria", "correlacionar", "reunioes", "reuniao", "sono",
                "fatos", "reanalisar", "marcar"} <= names
        out = {}
        for name, args in [("inventario", {}), ("dia", {"data": "2026-10-05"}),
                           ("serie", {"metrica": "arousal_z", "inicio": "2026-10-05", "fim": "2026-10-05", "resolucao_min": 30}),
                           ("reunioes", {"inicio": "2026-10-04", "fim": "2026-10-05"}),
                           ("reuniao", {"event_id": "meet-1"}),
                           ("tabela_diaria", {"inicio": "2026-10-04", "fim": "2026-10-05"}),
                           ("marcar", {"rotulo": "café", "quando": "2026-10-05 15:00"})]:
            out[name] = await srv.call_tool(name, args)
        return out

    out = asyncio.run(go())
    text = json.dumps(out, default=str, ensure_ascii=False)
    assert "arousal_z" in text
    assert "Revisão de prazo" in text
    assert "mark:mcp" in text
    assert "inicio" in text and "pontos" in text
