"""Health summary documents for other assistants (Gemini Notebook, ex-NotebookLM, and the Gemini app).

Three self-describing Markdown documents, each regenerated in full and published over the SAME
Google Doc every time (Drive-imported Docs auto-sync in Gemini Notebook; deleting/recreating a Doc
drops it from the notebook):

    hoje      small: 'Atualizado em', a machine-readable 'ALERTA:' line, today, last night, the next
              meetings, recent alerts, the last 7 days and the last verified report
    dias      rolling last 60 days, one '## AAAA-MM-DD' section per day (citations point to a day)
    semanas   compact aggregates per ISO week and per month + the meeting analysis (kept forever,
              stays small for years)

Only computed numbers leave the machine — no raw series, no transcript text — and colleagues'
names are replaced by stable pseudonyms unless `names=True` (`ai.names`: the same Pessoa A, B...
the report uses with Claude). Besides the fields known to carry names (titles, speaker labels,
alert texts, the report), every finished document gets a last pass, so a new free-text field
cannot bypass it; the 'Atualizado em' line is ours and skips it ("America/Sao_Paulo" is a time zone,
not a colleague named Paulo).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from pulso import alerts, config, jobs, store
from pulso.ai.facts import fmt, hhmm, pct
from pulso.ai.names import Pseudonyms

WEEKDAYS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]

GUIDE = """## Como ler

- **Ativação (z)**: estresse fisiológico estimado pela central Pulso (batimento a batimento, frequência
  cardíaca não explicada pelo movimento e estresse da Garmin, fundidos por filtro de Kalman), em desvios do
  **normal desta pessoa naquele horário**: 0 = normal, 1 z = perceptível, 2 z = forte.
- **Episódio**: ativação acima de 1,5 z por pelo menos 3 min. *mental* = parado; *físico* = em movimento.
- **HRV** (RMSSD, ms): mais alta costuma indicar mais recuperação; o status compara 7 noites com a faixa
  normal dos últimos 60 dias.
- **Reunião vs. mesmo horário sem reunião**: p < 0,05 e IC que não cruza zero indicam efeito da reunião
  além do horário. Correlação não é causa. Não é diagnóstico médico.
- Pessoas aparecem como “Pessoa A, B…”; “eu” é o dono destes dados.
"""


@dataclass
class Doc:
    key: str
    title: str
    markdown: str


TITLES = {
    "hoje": "Pulso · Saúde — Hoje",
    "dias": "Pulso · Saúde — Últimos 60 dias",
    "semanas": "Pulso · Saúde — Semanas e meses",
}


def _t(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def _stamp(tz: str) -> str:
    return f"Atualizado em: {datetime.now(ZoneInfo(tz)).strftime('%Y-%m-%d %H:%M')} ({tz})"


def _ident(x: str) -> str:
    return x


def _final(T, md: str) -> str:
    """The last pseudonym pass over a finished document, line by line, sparing the stamp line."""
    if T is _ident:
        return md
    return "\n".join(line if line.startswith("Atualizado em:") else T(line) for line in md.split("\n"))


def _titles(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["id"]: r["title"] or "" for r in conn.execute("SELECT id, title FROM events")}


def _episode_lines(conn, d: date, tz: str, T, titles) -> list[str]:
    day = store.get_artefact(conn, "analysis:day", d.isoformat()) or {}
    out = []
    for e in day.get("episodes", []):
        links = [T(titles.get(i) or i) for i in e.get("overlaps", [])]
        out.append(f"- {hhmm(e['start_ms'], tz)}–{hhmm(e['end_ms'], tz)}: episódio {e['kind']}, pico {fmt(e['peak_z'], 1)} z às "
                   f"{hhmm(e['peak_ms'], tz)}" + (f"; coincide com: {', '.join(str(x) for x in links)}" if links else "; sem evento na agenda"))
    return out


def _night_line(n: dict | None) -> str:
    if not n:
        return "sem noite registrada"
    eff, nadir_at = pct(n.get("efficiency")), pct(n.get("hr_nadir_at_frac"))  # missing stays missing, never 0 %
    return (f"{fmt(n['total_sleep_min'] / 60, 1)} h de sono, " + (f"eficiência {fmt(eff, 0)} %, " if eff is not None else "")
            + f"{fmt(n.get('awakenings'), 0)} despertares, FC mínima {fmt(n.get('hr_nadir'), 0)} bpm"
            + (f" aos {fmt(nadir_at, 0)} % da noite" if nadir_at is not None else "")
            + f", HRV {fmt(n.get('hrv_ms'), 0)} ms ({n.get('hrv_status') or '—'}), débito de 14 dias {fmt(n.get('debt_min'), 0)} min")


def alert_level(conn: sqlite3.Connection, now_ms: int) -> tuple[str, str]:
    """('NENHUM'|'ATENÇÃO'|'ALTO', motivo) from the last 24 h of alerts and last night."""
    recent = alerts.recent(conn, now_ms - 24 * 3600_000)
    night = store.get_artefact(conn, "sleep:last", "latest") or {}
    reasons = [a["text"] for a in recent]
    debt = night.get("debt_min") or 0
    below = night.get("hrv_status") == "below"
    if any(a["kind"] == "agora" for a in recent) or (below and debt > 300):
        return "ALTO", "; ".join(reasons) or "HRV abaixo do normal e débito de sono alto"
    if reasons or below or debt > 300:
        extra = (["HRV noturna abaixo do normal"] if below else []) + ([f"débito de sono de {debt / 60:.0f} h"] if debt > 300 else [])
        return "ATENÇÃO", "; ".join(reasons + extra)
    return "NENHUM", "sem sinais fora do normal nas últimas 24 h"


def _table(rows: list[dict]) -> list[str]:
    out = ["| dia | ativação (z) | min > 1,5 z | episódios (mentais) | sono (h) | débito (min) | HRV (ms) | reuniões | min reunião | commits 22h–5h |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        ep = "—" if r["episodes"] is None else f"{r['episodes']} ({r['episodes_mental']})"
        out.append(f"| {r['day']} | {fmt(r['arousal_mean'], 2)} | {fmt(r['arousal_min_above_1_5'], 0)} | {ep} | {fmt(r['sleep_h'], 1)} | "
                   f"{fmt(r['sleep_debt_min'], 0)} | {fmt(r['hrv_ms'], 0)} | {r['meetings']} | {r['meeting_minutes']} | {r['late_commits']} |")
    return out


def _last_report(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT body FROM artefacts WHERE kind IN ('report:daily', 'report:weekly') ORDER BY created_ms DESC LIMIT 1").fetchone()
    return json.loads(row["body"]) if row else None


# --------------------------------------------------------------------------------- docs


def build_today(conn: sqlite3.Connection, tz: str, today: date, T=_ident) -> str:
    from pulso.analysis import local_day_bounds

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    titles = _titles(conn)
    level, why = alert_level(conn, now_ms)
    out = [f"# {TITLES['hoje']}", "", _stamp(tz), "", f"ALERTA: {level} — {T(why)}", ""]

    rows = jobs.daily_table(conn, today - timedelta(days=6), today, tz)
    t = rows[-1]
    out += [f"## Hoje ({today.isoformat()}, {WEEKDAYS[today.weekday()]})", "",
            f"- Ativação média acordado: {fmt(t['arousal_mean'], 2)} z; {fmt(t['arousal_min_above_1_5'], 0)} min acima de 1,5 z.",
            f"- Reuniões: {t['meetings']} ({t['meeting_minutes']} min). Commits: {t['commits']} ({t['late_commits']} entre 22h e 5h)."]
    out += _episode_lines(conn, today, tz, T, titles) or ["- nenhum episódio de estresse até agora"]
    out += ["", "## Última noite", "", f"- {_night_line(store.get_artefact(conn, 'sleep:night', today.isoformat()))}.", ""]

    _, end = local_day_bounds(today, tz)
    nxt = [e for e in store.events(conn, now_ms, end, kinds=["calendar", "meeting"]) if e.start_ms >= now_ms]
    out += ["## Próximos compromissos de hoje", ""]
    out += [f"- {hhmm(e.start_ms, tz)}–{hhmm(e.end_ms, tz)} {T(e.title or 'compromisso')}" for e in nxt[:8]] or ["- nenhum"]
    out += ["", "## Alertas (48 h)", ""]
    out += [f"- {_t(x['at_ms'], tz)} — {T(x['text'])} ({T(x['detail'])})" for x in alerts.recent(conn, now_ms - 48 * 3600_000)] or ["- nenhum"]
    out += ["", "## Últimos 7 dias", "", *_table(rows), ""]

    rep = _last_report(conn)
    out += ["## Último relatório (números verificados)", ""]
    if rep:
        if rep.get("headline"):
            out += [f"**{T(rep['headline'])}**", ""]
        out += [f"- **{T(i['title'])}** — {T(i['body'])} (confiança {i['confidence']})" for i in rep.get("insights", [])]
        out += [f"- Recomendação: {T(r['action'])}" for r in rep.get("recommendations", [])]
    else:
        out.append("- ainda não gerado")
    out += ["", GUIDE]
    return "\n".join(out)


def build_days(conn: sqlite3.Connection, tz: str, today: date, days: int = 60, T=_ident) -> str:
    titles = _titles(conn)
    out = [f"# {TITLES['dias']}", "", _stamp(tz), "",
           "Um bloco por dia, do mais recente para o mais antigo. Unidades explicadas no fim.", ""]
    rows = {r["day"]: r for r in jobs.daily_table(conn, today - timedelta(days=days - 1), today, tz)}
    responses: dict[str, list] = defaultdict(list)
    for r in conn.execute("SELECT body FROM artefacts WHERE kind = 'meeting_response'"):
        m = json.loads(r["body"])
        responses[m.get("day")].append(m)
    for k in range(days):
        d = today - timedelta(days=k)
        r = rows.get(d.isoformat())
        night = store.get_artefact(conn, "sleep:night", d.isoformat())
        if not r or (r["arousal_mean"] is None and not r["meetings"] and not night):
            continue
        out += [f"## {d.isoformat()} ({WEEKDAYS[d.weekday()]})", "",
                f"- Ativação média {fmt(r['arousal_mean'], 2)} z, {fmt(r['arousal_min_above_1_5'], 0)} min acima de 1,5 z; "
                f"reuniões {r['meetings']} ({r['meeting_minutes']} min); commits {r['commits']} ({r['late_commits']} entre 22h e 5h); "
                f"marcações {r['marks']}.",
                f"- Noite anterior: {_night_line(night)}."]
        out += _episode_lines(conn, d, tz, T, titles)
        for m in sorted(responses.get(d.isoformat(), []), key=lambda m: m["start_ms"]):
            rec = f"recuperou em {fmt(m['recovery_min'], 0)} min" if m.get("recovery_min") is not None else "não voltou ao normal em 60 min"
            out.append(f"- Reunião {hhmm(m['start_ms'], tz)} “{T(m.get('title') or '?')}”: durante {fmt(m.get('during'), 2)} z sobre o basal, "
                       f"pico {fmt(m.get('peak'), 2)} z, {rec}.")
        out.append("")
    out.append(GUIDE)
    return "\n".join(out)


def build_aggregates(conn: sqlite3.Connection, tz: str, today: date, T=_ident) -> str:
    out = [f"# {TITLES['semanas']}", "", _stamp(tz), ""]
    first = conn.execute("SELECT MIN(key) FROM artefacts WHERE kind = 'analysis:day'").fetchone()[0]
    start = date.fromisoformat(first) if first else today
    rows = jobs.daily_table(conn, start, today, tz)

    def agg(group: list[dict]) -> str:
        def mean(k):
            v = [r[k] for r in group if r.get(k) is not None]
            return float(np.mean(v)) if v else None

        def tot(k):
            return int(sum(r[k] or 0 for r in group))

        return (f"ativação média {fmt(mean('arousal_mean'), 2)} z; episódios {tot('episodes')}; sono médio {fmt(mean('sleep_h'), 1)} h; "
                f"HRV média {fmt(mean('hrv_ms'), 0)} ms; reuniões {tot('meetings')} ({tot('meeting_minutes')} min); "
                f"commits 22h–5h {tot('late_commits')}; dias com dados {sum(1 for r in group if r['arousal_mean'] is not None)}")

    weeks: dict[str, list] = defaultdict(list)
    months: dict[str, list] = defaultdict(list)
    for r in rows:
        d = date.fromisoformat(r["day"])
        y, w, _ = d.isocalendar()
        weeks[f"{y}-S{w:02d}"].append(r)
        months[d.strftime("%Y-%m")].append(r)
    out += ["## Semanas (ISO)", ""]
    out += [f"- {k} ({v[0]['day']} a {v[-1]['day']}): {agg(v)}." for k, v in sorted(weeks.items(), reverse=True)] or ["- sem dados"]
    out += ["", "## Meses", ""]
    out += [f"- {k}: {agg(v)}." for k, v in sorted(months.items(), reverse=True)] or ["- sem dados"]

    out += ["", "## Reuniões (últimos 90 dias)", ""]
    try:
        s = jobs.meeting_job(conn, today - timedelta(days=89), today, tz)
    except Exception:
        s = None
    if s and s.get("n_meetings"):
        vc = s["vs_matched_controls"]
        out.append(f"- {s['n_meetings']} reuniões com dados. Contra os mesmos horários sem reunião: {fmt(vc['mean_dz'], 2)} z "
                   f"(IC95 {fmt(vc['ci'][0], 2)} a {fmt(vc['ci'][1], 2)}, p = {fmt(vc['p'], 3)}, n = {vc['n']}).")
        bb = s.get("back_to_back")
        if bb:
            out.append(f"- Coladas na anterior (< 10 min): {fmt(bb['mean_b2b'], 2)} z (n = {bb['n_b2b']}); com pausa: "
                       f"{fmt(bb['mean_break'], 2)} z (n = {bb['n_break']}); p = {fmt(bb['p'], 3)}.")
        out += [f"- Quando {T(r['label'])} fala: {fmt(r['mean_dz'], 2)} z (IC95 {fmt(r['ci_low'], 2)} a {fmt(r['ci_high'], 2)}, n = {r['n']})."
                for r in s.get("by_speaker", [])[:6]]
        out += [f"- Trechos “{r['label']}”: {fmt(r['mean_dz'], 2)} z (IC95 {fmt(r['ci_low'], 2)} a {fmt(r['ci_high'], 2)}, n = {r['n']})."
                for r in s.get("by_tag", [])[:8]]
        out += [f"- Pesou: “{T(r.get('title') or '?')}” em {r['day']} — {fmt(r.get('during'), 2)} z sobre o basal." for r in s.get("worst", [])[:5]]
    else:
        out.append("- sem reuniões analisadas")
    out += ["", GUIDE]
    return "\n".join(out)


def documents(conn: sqlite3.Connection, tz: str, today: date | None = None, names: bool = False, days: int = 60) -> list[Doc]:
    today = today or datetime.now(ZoneInfo(tz)).date()
    T = _ident if names else Pseudonyms(conn)
    return [
        Doc("hoje", TITLES["hoje"], _final(T, build_today(conn, tz, today, T))),
        Doc("dias", TITLES["dias"], _final(T, build_days(conn, tz, today, days, T))),
        Doc("semanas", TITLES["semanas"], _final(T, build_aggregates(conn, tz, today, T))),
    ]


def build(conn: sqlite3.Connection, tz: str, today: date | None = None, days: int = 60, names: bool = False) -> str:
    """All three documents concatenated (one local file)."""
    return "\n\n---\n\n".join(d.markdown for d in documents(conn, tz, today, names, days))


def write_local(docs: list[Doc] | str, tz: str, folder: Path | None = None) -> Path:
    folder = folder or (config.home() / "resumos")
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(ZoneInfo(tz)).date().isoformat()
    if isinstance(docs, str):
        docs = [Doc("tudo", "Pulso", docs)]
    for d in docs:
        (folder / f"pulso-{d.key}.md").write_text(d.markdown, encoding="utf-8")
        (folder / f"pulso-{d.key}-{stamp}.md").write_text(d.markdown, encoding="utf-8")
    return folder
