import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest

from pulso import config, jobs, store
from pulso.ai import facts as F
from pulso.ai import report, tagger

from test_analysis import TZ, _fill_days


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
            "recommendations": [{"action": "Proteger 30 min depois da revisão", "evidence": [ep.id]}],
            "missing_data": ["transcrição etiquetada"],
            "face_line": "Revisão de prazo pesou hoje",
        }

    client = FakeClient([answer])
    rep = report.generate(analysed, date(2026, 10, 5), date(2026, 10, 5), TZ, settings=config.Settings(), client=client)
    assert [i["title"] for i in rep["insights"]] == ["Pico na revisão de prazo"]
    assert rep["rejected"] and rep["rejected"][0]["title"] == "Inventado"
    assert "Descartado pela verificação" in rep["markdown"]
    assert store.get_artefact(analysed, "insight:face", "2026-10-05")["text"] == "Revisão de prazo pesou hoje"


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
