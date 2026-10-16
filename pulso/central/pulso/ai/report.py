"""Daily / weekly report written by Claude from verified facts, then passed through the numbers gate.

Colleagues' names never reach the API: the facts go out with stable pseudonyms (Pessoa A, B...,
`ai.names`, the same ones the Google Docs use), the gate checks the answer against exactly what was
sent, and the pseudonyms are mapped back to names locally, in the stored report and on the watch.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pulso import config, store
from pulso.ai import facts as F
from pulso.ai.names import Pseudonyms

FACE_CHARS = 48

SYSTEM = """Você é o analista de saúde e performance do Pulso, o sistema pessoal de um engenheiro de software
que usa um Garmin Forerunner 255 e quer entender de onde vem o estresse dele.

Você recebe uma lista numerada de FATOS calculados por um pipeline determinístico (sinais do relógio, agenda,
transcrições de reunião, commits). Regras:
- Use apenas os fatos. Cada insight cita os ids dos fatos em que se apoia (campo evidence).
- Todo número, toda data e todo horário que você escrever precisa aparecer em um dos fatos citados por aquele
  insight, com a mesma formatação (vírgula decimal). Vale também para contagens pequenas (2 reuniões, três
  episódios), números por extenso (zero, onze, vinte), números com unidade colada (4h, 25ms) e limiares. Não calcule
  números novos, não arredonde de outro jeito, não some, não conte fatos. Datas: escreva no formato AAAA-MM-DD
  ou pelo dia da semana que o fato traz entre parênteses (quinta-feira); não deduza o dia da semana.
- Pessoas aparecem como "Pessoa A", "Pessoa B"…; escreva exatamente assim ("Pessoa A/B" é uma de duas pessoas
  com o mesmo nome).
- "z" é desvio em relação ao normal da própria pessoa naquele horário: 1 z é perceptível, 2 z é forte.
- Correlação não é causa. Quando a comparação com horários equivalentes sem reunião não for significativa
  (p ≥ 0,05 ou o fato diz que o IC cruza o zero), diga que não dá para separar o efeito da reunião do efeito do horário.
- Seja direto e útil, em português do Brasil, sem tom de autoajuda. Recomendações concretas e verificáveis
  (ex.: mover a 1:1 de terça para depois do almoço), sempre ligadas a fatos.
- Se os fatos forem poucos, diga o que falta medir em vez de inventar padrão.
- missing_data: o que falta medir, sem números que não estejam nos fatos.
- face_line: uma frase de no máximo 48 caracteres para o mostrador do relógio, sem números que não estejam nos fatos."""

SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "insights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["alta", "média", "baixa"]},
                },
                "required": ["title", "body", "evidence", "confidence"],
                "additionalProperties": False,
            },
        },
        "recommendations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"action": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                "required": ["action", "evidence"],
                "additionalProperties": False,
            },
        },
        "missing_data": {"type": "array", "items": {"type": "string"}},
        "face_line": {"type": "string"},
    },
    "required": ["headline", "insights", "recommendations", "missing_data", "face_line"],
    "additionalProperties": False,
}


def fit(text: str | None, n: int = FACE_CHARS) -> str:
    """At most n chars, cut between words (a cut never splits "2,4" into an unverified "2")."""
    text = (text or "").strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0] if " " in text[: n - 1] else text[: n - 1]
    return cut.rstrip(" ,;:-–") + "…"


def verify(out: dict, book: F.FactBook) -> dict:
    """Apply the numbers gate to everything that is published: headline, insights, recommendations,
    missing data and the face line (checked as it will be shown, after the cut to 48 chars)."""
    kept, rejected = [], []
    for ins in out.get("insights", []):
        probs = F.gate(ins["title"] + " " + ins["body"], ins.get("evidence", []), book)
        (rejected if probs else kept).append(ins | ({"problems": probs} if probs else {}))
    recs_ok, recs_bad = [], []
    for r in out.get("recommendations", []):
        probs = F.gate(r["action"], r.get("evidence", []), book)
        (recs_bad if probs else recs_ok).append(r | ({"problems": probs} if probs else {}))
    all_ids = [f.id for f in book.facts]
    head_probs = F.gate(out.get("headline", ""), all_ids, book)
    miss_ok, miss_bad = [], []
    for m in out.get("missing_data", []):
        probs = F.gate(m, all_ids, book)
        (miss_bad if probs else miss_ok).append({"missing_data": m, "problems": probs} if probs else m)
    face = fit(out.get("face_line"))
    face_probs = F.gate(face, all_ids, book)
    return {
        "headline": out.get("headline") if not head_probs else None,
        "insights": kept,
        "recommendations": recs_ok,
        "missing_data": miss_ok,
        "face_line": (face or None) if not face_probs else None,
        "rejected": rejected + recs_bad + miss_bad
        + ([{"headline": out.get("headline"), "problems": head_probs}] if head_probs else [])
        + ([{"face_line": face, "problems": face_probs}] if face_probs else []),
    }


def to_markdown(rep: dict, book: F.FactBook, start: date, end: date) -> str:
    facts = book.by_id()
    lines = [f"# Pulso — {start.isoformat()}" + (f" a {end.isoformat()}" if end != start else ""), ""]
    if rep.get("headline"):
        lines += [f"**{rep['headline']}**", ""]
    for ins in rep["insights"]:
        lines += [f"## {ins['title']}", "", ins["body"], "",
                  "<sub>evidência: " + "; ".join(f"[{e}] {facts[e].text}" for e in ins["evidence"] if e in facts)
                  + f" · confiança {ins['confidence']}</sub>", ""]
    if rep["recommendations"]:
        lines += ["## O que fazer", ""] + [f"- {r['action']} ({', '.join(r['evidence'])})" for r in rep["recommendations"]] + [""]
    if rep["missing_data"]:
        lines += ["## O que falta medir", ""] + [f"- {m}" for m in rep["missing_data"]] + [""]
    if rep["rejected"]:
        lines += ["<details><summary>Descartado pela verificação de números "
                  f"({len(rep['rejected'])})</summary>", ""]
        lines += [f"- {json.dumps(r, ensure_ascii=False)}" for r in rep["rejected"]] + ["", "</details>"]
    return "\n".join(lines)


def face_day(end: date, today: date) -> date:
    """The day the face shows a report's line: the morning after a period that ended yesterday (the
    scheduler's daily and weekly reports), the same day for a period that ends today or later. Older
    periods land on a past day, so a re-run of last month never replaces today's line."""
    return end + timedelta(days=1) if end < today else end


def generate(conn: sqlite3.Connection, start: date, end: date, tz: str, kind: str = "daily",
             dry_run: bool = False, settings: config.Settings | None = None, client=None,
             today: date | None = None) -> dict:
    settings = settings or config.get()
    book = F.build(conn, start, end, tz)
    if not book.facts:
        return {"markdown": "Sem dados analisados nesse período. Rode `pulso analyze` primeiro.", "facts": []}
    if dry_run:
        return {"markdown": "```\n" + book.render() + "\n```", "facts": [f.__dict__ for f in book.facts]}
    from pulso.ai.claude import structured

    names = Pseudonyms(conn)
    sent = book.masked(names)  # what leaves the machine, and what the answer is checked against
    user = (f"Período: {start.isoformat()} a {end.isoformat()} ({'relatório diário' if kind == 'daily' else 'relatório semanal'}).\n\n"
            f"FATOS:\n{sent.render()}")
    raw, usage = structured(SYSTEM, user, SCHEMA, effort="high" if kind == "daily" else "xhigh",
                            settings=settings, client=client)
    rep = names.reveal(verify(raw, sent))
    if rep.get("face_line"):
        # names are back: re-fit to the face and re-check the line as it will be shown
        line = fit(rep["face_line"])
        rep["face_line"] = line if not F.gate(line, [f.id for f in book.facts], book) else None
    md = to_markdown(rep, book, start, end)
    key = f"{start.isoformat()}..{end.isoformat()}"
    store.put_artefact(conn, f"report:{kind}", key, rep | {"markdown": md, "usage": usage.__dict__})
    if rep.get("face_line"):
        shown = face_day(end, today or datetime.now(ZoneInfo(tz)).date())
        store.put_artefact(conn, "insight:face", shown.isoformat(), {"text": rep["face_line"]})
    return rep | {"markdown": md}
