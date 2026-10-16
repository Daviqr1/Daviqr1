"""Transcript segment tagging with Claude.

One request per meeting (chunked for very long ones). Speaker names are pseudonymised before
leaving the machine (S1, S2, ... and EU for you) and mapped back on return — the text itself is
sent, so use this only for meetings where that is acceptable (see docs/privacidade.md).
Tags feed `models.meetings.segment_effects`, which estimates the physiological response to each
kind of moment; the model only labels, it never sees physiology.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date

from pulso import analysis, config, store
from pulso.ai.names import me_aliases  # shared with the report/export pseudonyms
from pulso.importers.transcripts import segments_for

TAGS = [
    "prazo", "cobrança", "conflito", "crítica_recebida", "incerteza", "bloqueio", "incidente",
    "mudança_de_escopo", "decisão", "planejamento", "técnico", "status", "reconhecimento",
    "pergunta_para_mim", "eu_apresentando", "interrupção", "social",
]

SYSTEM = (
    "Você classifica trechos de transcrição de reuniões de trabalho de um engenheiro de software (EU). "
    "Para cada trecho, escolha de 0 a 3 etiquetas da lista fixa que descrevem o que está acontecendo do ponto de "
    "vista de EU (pressão, conflito, foco técnico, etc.), uma intensidade emocional 0-3 e se o trecho é dirigido a EU. "
    "Considere o contexto dos trechos vizinhos. Não invente etiquetas fora da lista. Responda só com o JSON pedido."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
                    "intensity": {"type": "integer", "enum": [0, 1, 2, 3]},
                    "to_me": {"type": "boolean"},
                },
                "required": ["i", "tags", "intensity", "to_me"],
                "additionalProperties": False,
            },
        },
        "topics": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["segments", "topics", "summary"],
    "additionalProperties": False,
}

CHUNK = 250


def pseudonymise(segs: list[dict]) -> tuple[list[str], dict[str, str]]:
    me = me_aliases()
    mapping: dict[str, str] = {}
    lines = []
    for s in segs:
        spk = (s.get("speaker") or "").strip() or "?"
        if spk.lower() in me or spk.lower().split()[0] in me:
            alias = "EU"
        else:
            alias = mapping.setdefault(spk, f"S{len(mapping) + 1}")
        lines.append(f"{s['idx']}|{alias}|{s['text']}")
    return lines, {v: k for k, v in mapping.items()}


def _unmask(text: str, back: dict[str, str]) -> str:
    """S1, S2... in Claude's topics/summary back to the speakers' names (kept locally)."""
    return re.sub(r"\bS\d+\b", lambda m: back.get(m.group(0), m.group(0)), text)


def tag_meeting(conn: sqlite3.Connection, event_id: str, settings: config.Settings | None = None, client=None) -> dict:
    from pulso.ai.claude import structured

    settings = settings or config.get()
    segs = segments_for(conn, event_id)
    if not segs:
        return {"event_id": event_id, "tagged": 0}
    topics: list[str] = []
    summaries: list[str] = []
    tagged = 0
    for k in range(0, len(segs), CHUNK):
        part = segs[k : k + CHUNK]
        lines, back = pseudonymise(part)
        user = "Etiquetas permitidas: " + ", ".join(TAGS) + "\n\nTrechos (idx|falante|texto):\n" + "\n".join(lines)
        out, _usage = structured(SYSTEM, user, SCHEMA, model=settings.tagger_model, effort="low",
                                 max_tokens=16000, settings=settings, client=client)
        by_i = {s["i"]: s for s in out["segments"]}
        with store.transaction(conn):
            for s in part:
                r = by_i.get(s["idx"])
                if r is None:
                    continue
                tags = [t for t in r["tags"] if t in TAGS]
                if r.get("to_me") and "pergunta_para_mim" not in tags and "?" in s["text"]:
                    tags.append("pergunta_para_mim")
                conn.execute("UPDATE transcript_segments SET tags = ? WHERE event_id = ? AND idx = ?",
                             (json.dumps(tags + [f"intensidade_{r['intensity']}"], ensure_ascii=False), event_id, s["idx"]))
                tagged += 1
        topics += [_unmask(t, back) for t in out.get("topics", [])]
        summaries.append(_unmask(out.get("summary", ""), back))
    ev = conn.execute("SELECT attrs FROM events WHERE id = ?", (event_id,)).fetchone()
    attrs = json.loads(ev["attrs"]) if ev else {}
    attrs["ai"] = {"topics": sorted(set(topics))[:12], "summary": " ".join(summaries)[:1500]}
    conn.execute("UPDATE events SET attrs = ? WHERE id = ?", (json.dumps(attrs, ensure_ascii=False), event_id))
    return {"event_id": event_id, "tagged": tagged, "topics": attrs["ai"]["topics"]}


def tag_range(conn: sqlite3.Connection, start: date, end: date, tz: str, settings: config.Settings | None = None,
              client=None, only_untagged: bool = True) -> list[dict]:
    a, _ = analysis.local_day_bounds(start, tz)
    _, b = analysis.local_day_bounds(end, tz)
    out = []
    for ev in store.events(conn, a, b, kinds=["meeting"]):
        if only_untagged:
            n = conn.execute("SELECT COUNT(*) FROM transcript_segments WHERE event_id = ? AND tags IS NULL", (ev.id,)).fetchone()[0]
            if n == 0:
                continue
        out.append(tag_meeting(conn, ev.id, settings=settings, client=client))
    return out
