"""Colleagues' names: what reaches Claude (report) and Google (Docs export) is pseudonymised."""

import json
import re
from datetime import date

import pytest

from pulso import analysis, config, jobs, store
from pulso.ai import report
from pulso.ai.names import Pseudonyms
from pulso.export import summary
from pulso.store import Event

from test_ai_mcp import FakeClient

TZ = "America/Sao_Paulo"
MIN = 60_000
DAY = date(2026, 10, 7)


@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setenv("PULSO_ME", "eu,davi")
    c = store.connect(":memory:")
    a = analysis.local_day_bounds(DAY, TZ)[0]
    store.upsert_event(c, Event("c1", "calendar", a + 600 * MIN, a + 630 * MIN, "1:1 Ana Souza", "ics",
                                {"attendees": ["Ana Souza", "Ana Lima", "Davi Becam", "bruno.costa@empresa.com"]}))
    store.upsert_event(c, Event("m1", "meeting", a + 700 * MIN, a + 730 * MIN, "1-1-ana-lima", "transcript:vtt",
                                {"speakers": ["Ana Lima", "eu"]}))
    return c


def test_no_surname_leak_and_no_merge_of_people_sharing_a_first_name(conn):
    T = Pseudonyms(conn)
    souza, lima = T("Ana Souza"), T("Ana Lima")
    assert souza.startswith("Pessoa ") and lima.startswith("Pessoa ") and souza != lima
    assert T("1:1 Ana Lima") == f"1:1 {lima}"                                   # was "Pessoa A Lima"
    assert T("Revisão com Ana Lima e Ana Souza") == f"Revisão com {lima} e {souza}"
    assert T("1-1-ana-souza") == f"1-1-{souza}"                                 # separators in file stems
    assert T("Souza pediu o relatório") == f"{souza} pediu o relatório"         # a surname alone
    shared = T("Ana confirmou")                                                 # which Ana? neither is named
    assert "Ana" not in shared and souza.removeprefix("Pessoa ") in shared and lima.removeprefix("Pessoa ") in shared
    assert T("ANA SOUZA e ána lima") == f"{souza} e {lima}"                     # case and accents
    assert T("Bruno Costa entrou") == f"{T('Bruno Costa')} entrou" != "Bruno Costa entrou"  # from the e-mail
    assert T("Davi apresentou; eu respondi") == "Davi apresentou; eu respondi"  # you stay yourself
    assert T("Anabela e Analista") == "Anabela e Analista"                      # whole words only


def test_everyday_words_and_diarisation_labels_are_not_names(conn):
    store.upsert_event(conn, Event("m2", "meeting", 0, MIN, "x", "t", {"speakers": ["A", "B", "Bruno Dias"]}))
    T = Pseudonyms(conn)
    dias = T("Bruno Dias")
    assert T("Últimos 7 dias; dias com dados") == "Últimos 7 dias; dias com dados"   # a surname that is also a word
    assert T("Alinhamento com Dias") == f"Alinhamento com {dias}"                     # ...counts when capitalised
    assert T("a pauta e a ata") == "a pauta e a ata" and T("A").startswith("Pessoa ")  # speaker "A" only as a whole label
    docs = summary.documents(conn, TZ, today=DAY, days=1)
    assert "Últimos 60 dias" in docs[1].markdown and "Pessoa" not in docs[1].markdown.splitlines()[0]


def test_pseudonyms_are_stable_across_runs_and_new_people(conn):
    before = Pseudonyms(conn).map
    assert Pseudonyms(conn).map == before
    # someone who showed up earlier in time (a back-filled calendar) does not reshuffle the letters
    store.upsert_event(conn, Event("c0", "calendar", 0, MIN, "kickoff", "ics", {"attendees": ["Zé Alves"]}))
    after = Pseudonyms(conn).map
    assert {k: after[k] for k in before} == before and after["Zé Alves"] not in before.values()


def test_reveal_maps_pseudonyms_back(conn):
    T = Pseudonyms(conn)
    assert T.reveal(T("1:1 Ana Souza e Ana Lima")) == "1:1 Ana Souza e Ana Lima"
    assert T.reveal({"a": [T("Ana Lima")]}) == {"a": ["Ana Lima"]}


def test_report_sends_pseudonyms_and_maps_names_back(conn, monkeypatch):
    a = analysis.local_day_bounds(DAY, TZ)[0]
    store.put_artefact(conn, "analysis:day", DAY.isoformat(), {
        "arousal_mean_awake": 0.42, "arousal_minutes_above_1_5": 35, "episodes": [
            {"start_ms": a + 600 * MIN, "end_ms": a + 620 * MIN, "peak_ms": a + 610 * MIN, "peak_z": 2.1, "auc_z_min": 30.0,
             "kind": "mental", "overlaps": ["c1"]}]})
    # the report computes the meeting analysis for its range: stand in for one with a per-speaker effect
    summ = {"vs_matched_controls": {"n": 4, "mean_dz": 0.31, "ci": [0.05, 0.6], "p": 0.04}, "back_to_back": None, "lag_s": 20,
            "by_speaker": [{"label": "Ana Lima", "mean_dz": 0.9, "ci_low": 0.4, "ci_high": 1.3, "n": 12}], "by_tag": []}
    monkeypatch.setattr(jobs, "meeting_job", lambda *a, **k: summ)
    T = Pseudonyms(conn)
    lima, souza = T("Ana Lima"), T("Ana Souza")

    def answer(kw):
        sent = kw["messages"][0]["content"]
        assert "Ana" not in sent and "Lima" not in sent and "Souza" not in sent
        assert f"quando {lima} fala: 0,90 z" in sent and f"coincide com: 1:1 {souza}" in sent
        f_ep = next(line.split("]")[0][1:] for line in sent.splitlines() if "coincide com" in line)
        f_spk = next(line.split("]")[0][1:] for line in sent.splitlines() if "fala:" in line)
        return {"headline": f"{lima} pesou",
                "insights": [{"title": f"Quando {lima} fala", "body": f"Ativação sobe 0,90 z quando {lima} fala.",
                              "evidence": [f_spk], "confidence": "média"},
                             {"title": "Inventado", "body": f"Com {souza} a ativação passa de 3,5 z.", "evidence": [f_ep],
                              "confidence": "alta"}],
                "recommendations": [{"action": f"Pausa antes da 1:1 com {souza}", "evidence": [f_ep]}],
                "missing_data": [], "face_line": f"Fala de {lima}: 0,90 z"}

    rep = report.generate(conn, DAY, DAY, TZ, settings=config.Settings(), client=FakeClient([answer]), today=DAY)
    # the numbers gate still checks everything (against what was sent)
    assert [i["title"] for i in rep["insights"]] == ["Quando Ana Lima fala"]
    assert rep["rejected"][0]["title"] == "Inventado"
    # names are back in the local report and on the watch
    assert rep["headline"] == "Ana Lima pesou" and "Pausa antes da 1:1 com Ana Souza" in rep["markdown"]
    assert store.get_artefact(conn, "insight:face", DAY.isoformat())["text"] == "Fala de Ana Lima: 0,90 z"
    stored = store.get_artefact(conn, "report:daily", f"{DAY}..{DAY}")
    assert stored["insights"][0]["body"] == "Ativação sobe 0,90 z quando Ana Lima fala."
    # ...and pseudonymised again in the Google Doc
    hoje = summary.documents(conn, TZ, today=DAY, days=1)[0].markdown
    assert "Ana" not in hoje and f"Ativação sobe 0,90 z quando {lima} fala." in hoje


def test_exported_docs_carry_no_names(conn):
    a = analysis.local_day_bounds(DAY, TZ)[0]
    now = int(store.now_ms())
    store.put_artefact(conn, "alert", "reuniao:c1", {"id": "reuniao:c1", "kind": "reuniao", "at_ms": now,
                                                     "text": "1:1 Ana Souza às 10:00: costuma pesar",
                                                     "detail": "Reuniões parecidas com '1:1 Ana Souza' tiveram carga 3/3."})
    store.put_artefact(conn, "meeting_response", "m1", {"title": "1-1-ana-lima", "day": DAY.isoformat(), "start_ms": a + 700 * MIN,
                                                        "during": 0.8, "peak": 1.9, "recovery_min": 12, "attendees": ["Ana Lima"]})
    store.put_artefact(conn, "analysis:day", DAY.isoformat(), {"episodes": [
        {"start_ms": a + 600 * MIN, "end_ms": a + 620 * MIN, "peak_ms": a + 610 * MIN, "peak_z": 2.1, "kind": "mental",
         "overlaps": ["c1", "m1"]}]})
    store.put_artefact(conn, "report:daily", "x", {"headline": "Ana Lima e Souza pesaram", "insights": [], "recommendations": []})
    docs = summary.documents(conn, TZ, today=DAY, days=1)
    text = "\n".join(d.markdown for d in docs)
    for name in ("Ana", "Souza", "Lima", "Bruno", "Costa", "ana-lima"):
        assert name not in text, name
    assert "costuma pesar" in text and "Pessoa" in text
    with_names = "\n".join(d.markdown for d in summary.documents(conn, TZ, today=DAY, days=1, names=True))
    assert "1:1 Ana Souza às 10:00" in with_names
    assert json.loads(json.dumps(Pseudonyms(conn).map))  # mapping stays local and serialisable


def test_lowercase_surname_in_file_stems_and_typographic_apostrophe(conn):
    store.upsert_event(conn, Event("c2", "calendar", 0, MIN, "x", "ics",
                                   {"attendees": ["João D'Ávila", "Bruno Dias", "Speaker 1"]}))
    T = Pseudonyms(conn)
    souza, lima, dias = T("Ana Souza"), T("Ana Lima"), T("Bruno Dias")
    assert T("1-1-souza") == f"1-1-{souza}"                         # transcript file stems are lowercase
    assert T("sync_lima.vtt") == f"sync_{lima}.vtt"
    assert T("nos últimos 7 dias.") == "nos últimos 7 dias."          # a full stop does not glue a word
    assert T("dias com dados: 3; dias. Depois") == "dias com dados: 3; dias. Depois"
    assert T("1on1-dias") == f"1on1-{dias}"
    avila = T("João D'Ávila")
    assert avila.startswith("Pessoa ") and T("João D’Ávila") == avila and T("D’Ávila") == avila   # macOS/iOS apostrophe
    assert "Ávila" not in T("Revisão com João D’Ávila e D´Ávila")
    assert T("Speaker 1 falou") == f"{T('Speaker 1')} falou" != "Speaker 1 falou"
    assert T("speaker prep") == "speaker prep"                     # a diarisation label's word is nobody


def test_docs_keep_the_time_zone_with_a_colleague_named_paulo(conn):
    store.upsert_event(conn, Event("c3", "calendar", 0, MIN, "1:1 Paulo", "ics", {"attendees": ["Paulo Mendes"]}))
    docs = summary.documents(conn, TZ, today=DAY, days=1)
    for d in docs:
        assert "(America/Sao_Paulo)" in d.markdown, d.key
        assert "Paulo" not in d.markdown.replace("(America/Sao_Paulo)", ""), d.key
    assert all("Pessoa" not in line for line in summary.build(conn, TZ, today=DAY, days=1).splitlines()
               if line.startswith("Atualizado em:"))


def test_a_colleague_sharing_your_first_name_is_still_pseudonymised(conn, monkeypatch):
    """Only a WHOLE PULSO_ME alias is you: "Davi Costa" is a colleague even with "davi" in the list."""
    from pulso.ai import names, tagger

    monkeypatch.setenv("PULSO_ME", "eu,davi,gabi,davi becam")
    store.upsert_event(conn, Event("c4", "calendar", 0, MIN, "1:1 Davi Costa", "ics",
                                   {"attendees": ["Davi Costa", "Gabi Martins", "Davi Becam"]}))
    T = Pseudonyms(conn)
    assert "Costa" not in T("1:1 Davi Costa") and T("1:1 Davi Costa").startswith("1:1 Pessoa ")
    assert "Martins" not in T("Revisão com Gabi Martins") and "Costa" not in T("Costa pediu o relatório")
    assert T("Davi Becam") == "Davi Becam" and T("Becam e Davi") == "Becam e Davi"   # you, listed in full
    assert T("Davi apresentou") == "Davi apresentou"                                 # the bare alias stays
    assert names.is_me("DAVI  BÉCAM") and names.is_me("Davi") and not names.is_me("Davi Costa") and not names.is_me("?")
    _, back = tagger.pseudonymise([{"idx": 0, "speaker": "Davi Costa", "text": "x"},
                                   {"idx": 1, "speaker": "Davi Becam", "text": "y"},
                                   {"idx": 2, "speaker": "davi", "text": "z"}])
    assert back == {"S1": "Davi Costa"}                                              # only you are EU
    text = "\n".join(d.markdown for d in summary.documents(conn, TZ, today=DAY, days=1))
    assert "Costa" not in text and "Martins" not in text
    # without your full name in the list, your own name is pseudonymised too: nothing leaks
    monkeypatch.setenv("PULSO_ME", "eu,davi")
    assert "Becam" not in Pseudonyms(conn)("1:1 Davi Becam")


def test_pseudonymising_twice_changes_nothing_with_a_colleague_surnamed_pessoa(conn):
    """summary._final runs T over text that already has pseudonyms: "Pessoa A" must not become
    "Pessoa B A" because Carlos Pessoa is now a person."""
    store.upsert_event(conn, Event("c5", "calendar", 0, MIN, "kickoff", "ics", {"attendees": ["Carlos Pessoa"]}))
    T = Pseudonyms(conn)
    carlos = T("Carlos Pessoa")
    assert carlos.startswith("Pessoa ") and T("Pessoa pediu o prazo") == f"{carlos} pediu o prazo"
    for x in ("1:1 Ana Souza", "Revisão com Ana Lima e Ana Souza", "Ana confirmou", "Carlos Pessoa e Pessoa",
              "Pessoas aparecem como “Pessoa A, B…”", "1-1-ana-souza", "Pessoa pediu; Ana Souza respondeu"):
        once = T(x)
        assert T(once, final=True) == once, (x, once, T(once, final=True))
    assert T("uma pessoa a mais") == "uma pessoa a mais"
    docs = summary.documents(conn, TZ, today=DAY, days=1)
    for d in docs:
        assert "Pessoas aparecem como “Pessoa A, B…”" in d.markdown, d.key
        assert not re.search(r"Pessoa [A-Z]\d* [A-Z]\b", d.markdown), d.key


def test_what_looks_like_a_pseudonym_inside_a_name_is_still_the_name(conn):
    """"Pessoa D" in "Lia Pessoa D'Ávila" (an inner apostrophe does not end a word) or in "Caio Pessoa
    D. Lima" is part of a name, not a pseudonym to keep: the rest of the name must neither leak nor go
    to someone else, and T(T(x)) == T(x) still holds."""
    store.upsert_event(conn, Event("c7", "calendar", 0, MIN, "kickoff", "ics", {"attendees": [
        "Lia Pessoa D'Ávila", "Rui Pessoa O’Neill", "Caio Pessoa D. Lima", "Carlos Pessoa", "Bia d'Ávila"]}))
    T = Pseudonyms(conn)
    for x, name in (("1:1 Lia Pessoa D'Ávila", "Ávila"), ("Revisão com Rui Pessoa O’Neill", "Neill"),
                    ("d'Ávila confirmou", "Ávila"), ("Caio Pessoa D. Lima chegou", "Lima"),
                    ("pessoa d’Souza", "Souza"), ("dell'Ana", "Ana")):   # was "Pessoa D’Pessoa B" the 2nd time
        once = T(x)
        assert name not in once and T(once, final=True) == once, (x, once, T(once, final=True))
    assert T("1:1 Lia Pessoa D'Ávila") == "1:1 " + T("Lia Pessoa D'Ávila")
    assert T("Revisão com Rui Pessoa O’Neill") == "Revisão com " + T("Rui Pessoa O’Neill")
    assert T("Caio Pessoa D. Lima chegou") == T("Caio Pessoa D. Lima") + " chegou"   # not "Pessoa B Pessoa D. Pessoa B"
    assert T("Ana Souza's notes") == T("Ana Souza") + "'s notes"                     # a possessive still ends the word
    assert T(T("Ana Souza's notes"), final=True) == T("Ana Souza's notes")
    for x, name in (("Ana'll present", "Ana"), ("Souza'd come", "Souza"), ("Ana’ve", "Ana")):  # clitics end a name too
        assert name not in T(x), (x, T(x))
    assert T("d'ávila") == "d'ávila"                                                 # lowercase, like "dias"


def test_attendee_with_an_uppercase_mailto_scheme_is_recognised(conn):
    store.upsert_event(conn, Event("c6", "calendar", 0, MIN, "sync", "ics",
                                   {"attendees": ["MAILTO:rafael.reis@x.com", "Mailto:Bia.Nunes@x.com"]}))
    T = Pseudonyms(conn)
    for text in ("sync com Rafael Reis", "Bia Nunes entrou"):   # like any e-mail-only attendee: the full name
        out = T(text)
        assert not any(n in out for n in ("Rafael", "Reis", "Bia", "Nunes", "MAILTO")), out


def test_raw_fields_never_keep_a_pessoa_that_is_a_name(conn, monkeypatch):
    """Only T's own output (final=True) keeps "Pessoa X": in a raw calendar title, "Zé Pessoa E" or
    "Davi Pessoa" (first name = a PULSO_ME alias) is a colleague, not a pseudonym."""
    monkeypatch.setenv("PULSO_ME", "eu,davi")
    store.upsert_event(conn, Event("c8", "calendar", 0, MIN, "kickoff", "ics", {"attendees": [
        "Zé Pessoa", "Davi Pessoa", "Lu Pessoa Ramos", "Ana Souza"]}))
    T = Pseudonyms(conn)
    for x, leak in (("Zé Pessoa E Ana Souza", "Zé"), ("Davi Pessoa E Gabi", "Davi Pessoa"), ("Lu Pessoa R", "Lu")):
        once = T(x)
        assert leak not in once, (x, once)
        assert T(once, final=True) == once, (x, once)


def test_two_letter_names_are_pseudonymised_alone(conn):
    """review: "Zé", "Lu", "Sá", "Wu" are names; "a pauta e a ata", "Na segunda", "Em 2026" are not."""
    store.upsert_event(conn, Event("c9", "calendar", 0, MIN, "kickoff", "ics", {"attendees": [
        "Zé Pessoa", "Lu Sá", "Ed Wu"]}))
    T = Pseudonyms(conn)
    for x, names in (("Café com Zé", ["Zé"]), ("1:1 Lu", ["Lu"]), ("Sá: revisão", ["Sá"]), ("Wu / Ed", ["Wu", "Ed"])):
        once = T(x)
        assert not any(re.search(rf"\b{n}\b", once) for n in names), (x, once)
        assert T(once, final=True) == once
    for x in ("a pauta e a ata", "Na segunda", "Em 2026", "zero ruído", "luz"):
        assert T(x) == x, (x, T(x))
