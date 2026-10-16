"""The health summary that leaves the central for other assistants (NotebookLM / Gemini).

One self-describing Markdown document: it explains its own units, carries only computed numbers
(no raw series, no transcript text), and is regenerated in full every day so the document that
NotebookLM indexes is always the current picture. Dated copies are kept locally.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from pulso import alerts, config, jobs, store
from pulso.ai.facts import fmt, hhmm

GUIDE = """## Como ler este documento

- **Ativação (z)**: estado de estresse fisiológico estimado a partir de batimento a batimento, frequência
  cardíaca não explicada pelo movimento e estresse da Garmin, fundidos por filtro de Kalman. É medida em
  desvios do **normal desta pessoa naquele horário**: 0 = normal, 1 z = perceptível, 2 z = forte.
- **Episódio**: período em que a ativação passou de 1,5 z por pelo menos 3 min. *Mental* = parado;
  *físico* = em movimento; *misto* = parte de cada.
- **FC adicional**: batimentos por minuto acima do que o movimento e o horário explicam.
- **HRV** (RMSSD, ms): variabilidade da frequência cardíaca; mais alta costuma indicar mais recuperação.
  O status compara a média de 7 noites com a faixa normal dos últimos 60 dias.
- **Comparação com horários sem reunião**: cada reunião é comparada com o mesmo horário em dias sem
  compromisso; p < 0,05 e intervalo que não cruza zero indicam efeito da reunião além do horário.
- Números vêm de um pipeline determinístico; correlação não é causa; não é diagnóstico médico.
"""


def _t(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def build(conn: sqlite3.Connection, tz: str, today: date | None = None, days: int = 14, names: bool = False) -> str:
    """`names=False` (default) replaces colleagues' names by stable pseudonyms (Pessoa A, B...):
    this document leaves your machine."""
    today = today or datetime.now(ZoneInfo(tz)).date()
    start = today - timedelta(days=days - 1)
    anon = _Anonymizer(conn) if not names else None
    T = (lambda x: anon(x)) if anon else (lambda x: x)  # noqa: E731
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    out = [f"# Pulso — resumo de saúde e estresse",
           "",
           f"Atualizado em {datetime.now(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M')} ({tz}). Período: {start.isoformat()} a "
           f"{today.isoformat()}. Fontes: Garmin Forerunner 255, Garmin Connect, agenda, transcrições de reuniões e "
           "commits, processados pela central Pulso.",
           "", GUIDE]

    # alerts
    al = alerts.recent(conn, now_ms - 7 * 24 * 3600_000)
    out += ["## Alertas dos últimos 7 dias", ""]
    out += [f"- {_t(a['at_ms'], tz)} — {a['text']} ({a['detail']})" for a in al] or ["- nenhum"]
    out.append("")

    # daily table
    rows = jobs.daily_table(conn, start, today, tz)
    out += ["## Dia a dia", "",
            "| dia | ativação média (z) | min > 1,5 z | episódios (mentais) | sono (h) | débito de sono (min) | HRV noturna (ms) | reuniões | min em reunião | commits | commits 22h–5h |",
            "|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ep = "—" if r["episodes"] is None else f"{r['episodes']} ({r['episodes_mental']})"
        out.append(f"| {r['day']} | {fmt(r['arousal_mean'], 2)} | {fmt(r['arousal_min_above_1_5'], 0)} | {ep} | "
                   f"{fmt(r['sleep_h'], 1)} | {fmt(r['sleep_debt_min'], 0)} | {fmt(r['hrv_ms'], 0)} | {r['meetings']} | "
                   f"{r['meeting_minutes']} | {r['commits']} | {r['late_commits']} |")
    out.append("")

    # episodes, last 7 days
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM events")}
    out += ["## Episódios de estresse (últimos 7 dias)", ""]
    n_ep = 0
    d = today - timedelta(days=6)
    while d <= today:
        day = store.get_artefact(conn, "analysis:day", d.isoformat()) or {}
        for e in day.get("episodes", []):
            links = [T(titles.get(i) or i) for i in e.get("overlaps", [])]
            out.append(f"- {d.isoformat()} {hhmm(e['start_ms'], tz)}–{hhmm(e['end_ms'], tz)}: {e['kind']}, pico "
                       f"{fmt(e['peak_z'], 1)} z às {hhmm(e['peak_ms'], tz)}"
                       + (f"; coincide com: {', '.join(str(x) for x in links)}" if links else "; sem evento na agenda"))
            n_ep += 1
        d += timedelta(days=1)
    if not n_ep:
        out.append("- nenhum")
    out.append("")

    # meetings, last 30 days
    m_start = today - timedelta(days=29)
    summ = store.get_artefact(conn, "meetings:summary", f"{m_start.isoformat()}..{today.isoformat()}")
    if summ is None:
        try:
            summ = jobs.meeting_job(conn, m_start, today, tz)
        except Exception:
            summ = None
    out += ["## Reuniões (últimos 30 dias)", ""]
    if summ and summ.get("n_meetings"):
        vc = summ["vs_matched_controls"]
        out.append(f"- {summ['n_meetings']} reuniões com dados. Contra os mesmos horários sem reunião: diferença "
                   f"{fmt(vc['mean_dz'], 2)} z (IC95 {fmt(vc['ci'][0], 2)} a {fmt(vc['ci'][1], 2)}, p = {fmt(vc['p'], 3)}, n = {vc['n']}).")
        bb = summ.get("back_to_back")
        if bb:
            out.append(f"- Reuniões coladas na anterior (< 10 min de pausa): {fmt(bb['mean_b2b'], 2)} z (n = {bb['n_b2b']}); "
                       f"com pausa: {fmt(bb['mean_break'], 2)} z (n = {bb['n_break']}); p = {fmt(bb['p'], 3)}.")
        for r in summ.get("worst", [])[:5]:
            rec = f"recuperação {fmt(r['recovery_min'], 0)} min" if r.get("recovery_min") is not None else "sem recuperação em 60 min"
            out.append(f"- Pesou: \"{T(r.get('title') or '?')}\" em {r['day']} {hhmm(r['start_ms'], tz)} — durante {fmt(r.get('during'), 2)} z, "
                       f"pico {fmt(r.get('peak'), 2)} z, {rec}.")
        for r in summ.get("by_speaker", [])[:5]:
            label = T(r["label"])
            out.append(f"- Quando {label} fala: {fmt(r['mean_dz'], 2)} z (IC95 {fmt(r['ci_low'], 2)} a {fmt(r['ci_high'], 2)}, n = {r['n']}).")
        for r in summ.get("by_tag", [])[:6]:
            out.append(f"- Trechos \"{r['label']}\": {fmt(r['mean_dz'], 2)} z (IC95 {fmt(r['ci_low'], 2)} a {fmt(r['ci_high'], 2)}, n = {r['n']}).")
    else:
        out.append("- sem reuniões analisadas no período")
    out.append("")

    # last verified report
    rep = None
    for kind in ("report:weekly", "report:daily"):
        row = conn.execute("SELECT body FROM artefacts WHERE kind = ? ORDER BY created_ms DESC LIMIT 1", (kind,)).fetchone()
        if row:
            rep = json.loads(row["body"])
            break
    out += ["## Último relatório (números verificados)", ""]
    if rep:
        if rep.get("headline"):
            out += [f"**{rep['headline']}**", ""]
        for ins in rep.get("insights", []):
            out += [f"- **{ins['title']}** — {ins['body']} (confiança {ins['confidence']})"]
        for r in rep.get("recommendations", []):
            out += [f"- Recomendação: {r['action']}"]
    else:
        out.append("- ainda não gerado (`pulso report`)")
    out.append("")
    return "\n".join(out)


class _Anonymizer:
    """Stable pseudonyms (Pessoa A, B, ...) for every speaker/attendee name the store knows, applied
    to speaker labels and meeting titles. 'eu'/your aliases (PULSO_ME) stay as they are."""

    def __init__(self, conn: sqlite3.Connection):
        import os
        import re

        me = {a.strip().lower() for a in os.environ.get("PULSO_ME", "eu,me").split(",") if a.strip()}
        people: set[str] = set()
        for r in conn.execute("SELECT attrs FROM events WHERE kind IN ('meeting', 'calendar')"):
            a = json.loads(r["attrs"])
            for k in ("speakers", "attendees"):
                for n in a.get(k) or []:
                    if isinstance(n, str) and n.strip() and n.strip().lower() not in me and "@" not in n:
                        people.add(n.strip())
        for r in conn.execute("SELECT DISTINCT speaker FROM transcript_segments WHERE speaker IS NOT NULL"):
            if r["speaker"].strip().lower() not in me:
                people.add(r["speaker"].strip())
        self.map: dict[str, str] = {}
        for i, n in enumerate(sorted(people, key=lambda x: (-len(x), x))):
            self.map[n] = f"Pessoa {chr(ord('A') + i % 26)}{'' if i < 26 else i // 26}"
        # longest names first so "Ana Souza" wins over "Ana"; also replace first names alone
        pats = []
        for full, alias in self.map.items():
            pats.append((re.compile(rf"\b{re.escape(full)}\b", re.IGNORECASE), alias))
            first = full.split()[0]
            if len(first) > 2 and first.lower() not in me:
                pats.append((re.compile(rf"\b{re.escape(first)}\b", re.IGNORECASE), alias))
        self.pats = pats

    def __call__(self, text: str) -> str:
        if text in self.map:
            return self.map[text]
        for p, alias in self.pats:
            text = p.sub(alias, text)
        return text


def write_local(md: str, tz: str, folder: Path | None = None) -> Path:
    folder = folder or (config.home() / "resumos")
    folder.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo(tz)).date().isoformat()
    (folder / f"pulso-saude-{today}.md").write_text(md, encoding="utf-8")
    current = folder / "pulso-saude.md"
    current.write_text(md, encoding="utf-8")
    return current
