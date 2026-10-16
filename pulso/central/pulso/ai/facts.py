"""Facts in, verified prose out.

The report model never sees raw series. It receives a numbered list of *facts* — sentences
with numbers already computed by the deterministic pipeline — and must cite fact ids for every
claim. Then the numbers gate checks that every number and clock time it wrote exists in the facts
it cited (tolerating rounding). An insight that fails the gate is not published; it is listed
under "rejected" so you can see what the model tried to say.
"""

from __future__ import annotations

import math
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from pulso import analysis, store


def fmt(x: float | int | None, nd: int = 1) -> str:
    """pt-BR number: decimal comma, no thousands separator."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if isinstance(x, int) or (isinstance(x, float) and x.is_integer() and nd == 0):
        return str(int(round(x)))
    return f"{x:.{nd}f}".replace(".", ",")


def hhmm(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%H:%M")


@dataclass
class Fact:
    id: str
    text: str
    topic: str
    numbers: list[float] = field(default_factory=list)
    times: list[str] = field(default_factory=list)


class FactBook:
    def __init__(self) -> None:
        self.facts: list[Fact] = []

    def add(self, topic: str, text: str) -> str:
        fid = f"F{len(self.facts) + 1}"
        self.facts.append(Fact(fid, text, topic, extract_numbers(text), extract_times(text)))
        return fid

    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}

    def render(self) -> str:
        return "\n".join(f"[{f.id}] ({f.topic}) {f.text}" for f in self.facts)


_TIME = re.compile(r"\b([01]?\d|2[0-3])[:h]([0-5]\d)\b")
_NUM = re.compile(r"(?<![\w.,])[-−]?\d+(?:[.,]\d+)?(?![\w])")


def extract_times(text: str) -> list[str]:
    return [f"{int(h):02d}:{m}" for h, m in _TIME.findall(text)]


def extract_numbers(text: str) -> list[float]:
    t = _TIME.sub(" ", text)
    t = re.sub(r"\bF\d+\b", " ", t)               # fact ids are not numbers
    t = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", t)   # ISO dates handled as tokens, not values
    out = []
    for m in _NUM.findall(t):
        try:
            out.append(float(m.replace("−", "-").replace(",", ".")))
        except ValueError:
            pass
    return out


def _matches(x: float, pool: list[float]) -> bool:
    for v in pool:
        if abs(x - v) <= max(0.051, abs(v) * 0.02):  # one-decimal rounding or 2 %
            return True
        if abs(abs(x) - abs(v)) <= 0.051:  # "caiu 0,4" vs a fact stored as -0,4
            return True
    return False


SMALL_OK = {0.0, 1.0, 2.0, 3.0, 7.0, 24.0, 30.0, 60.0, 100.0}  # counts/units prose uses freely


def gate(text: str, cited: list[str], book: FactBook) -> list[str]:
    """Return the list of problems (empty = passes)."""
    facts = book.by_id()
    missing = [c for c in cited if c not in facts]
    if missing:
        return [f"cita fatos inexistentes: {', '.join(missing)}"]
    nums = [n for c in cited for n in facts[c].numbers]
    times = {t for c in cited for t in facts[c].times}
    probs = []
    for x in extract_numbers(text):
        if x in SMALL_OK:
            continue
        if not _matches(x, nums):
            probs.append(f"número {fmt(x, 2)} não está nos fatos citados")
    for t in extract_times(text):
        if t not in times:
            probs.append(f"horário {t} não está nos fatos citados")
    return probs


# --------------------------------------------------------------------------- fact building


def build(conn: sqlite3.Connection, start: date, end: date, tz: str) -> FactBook:
    book = FactBook()
    a, _ = analysis.local_day_bounds(start, tz)
    _, b = analysis.local_day_bounds(end, tz)
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM events WHERE start_ms < ? AND end_ms > ?", (b + 86_400_000, a - 86_400_000))}

    d = start
    while d <= end:
        day = store.get_artefact(conn, "analysis:day", d.isoformat())
        if day:
            if day.get("arousal_mean_awake") is not None:
                book.add("dia", f"{d.isoformat()}: ativação média acordado {fmt(day['arousal_mean_awake'], 2)} z; "
                                 f"{day['arousal_minutes_above_1_5']} minutos acima de 1,5 z; {len(day['episodes'])} episódios.")
            for ep in day["episodes"]:
                links = [titles.get(i) or i for i in ep.get("overlaps", [])]
                book.add("episódio", f"{d.isoformat()} episódio {ep['kind']} das {hhmm(ep['start_ms'], tz)} às {hhmm(ep['end_ms'], tz)}, "
                                     f"pico {fmt(ep['peak_z'], 1)} z às {hhmm(ep['peak_ms'], tz)}, área {fmt(ep['auc_z_min'], 1)} z·min"
                                     + (f"; coincide com: {', '.join(str(x) for x in links)}" if links else "; sem evento na agenda"))
        night = store.get_artefact(conn, "sleep:night", d.isoformat())
        if night:
            book.add("sono", f"noite que terminou em {d.isoformat()}: {fmt(night['total_sleep_min'] / 60, 1)} h de sono, "
                             f"eficiência {fmt((night.get('efficiency') or 0) * 100, 0)} %, profundo {fmt(night.get('deep_min'), 0)} min, "
                             f"REM {fmt(night.get('rem_min'), 0)} min, {night.get('awakenings', 0)} despertares, "
                             f"FC mínima {fmt(night.get('hr_nadir'), 0)} bpm a {fmt((night.get('hr_nadir_at_frac') or 0) * 100, 0)} % da noite, "
                             f"débito acumulado {fmt(night.get('debt_min'), 0)} min, HRV {fmt(night.get('hrv_ms'), 0)} ms ({night.get('hrv_status')}).")
        d += timedelta(days=1)

    for r in conn.execute("SELECT key, body FROM artefacts WHERE kind = 'meeting_response'"):
        import json

        m = json.loads(r["body"])
        if not (a <= m["start_ms"] < b):
            continue
        rec = f"recuperou em {fmt(m['recovery_min'], 0)} min" if m.get("recovery_min") is not None else "não voltou ao basal em 60 min"
        book.add("reunião", f"reunião \"{m.get('title') or '?'}\" ({m['day']} {hhmm(m['start_ms'], tz)}): antecipação {fmt(m.get('anticipation'), 2)} z, "
                            f"durante {fmt(m.get('during'), 2)} z, pico {fmt(m.get('peak'), 2)} z, {rec}.")

    summ = store.get_artefact(conn, "meetings:summary", f"{start.isoformat()}..{end.isoformat()}")
    if summ:
        vc = summ["vs_matched_controls"]
        if vc.get("mean_dz") is not None:
            book.add("reuniões", f"reuniões vs. mesmos horários sem reunião ({vc['n']} reuniões): diferença {fmt(vc['mean_dz'], 2)} z, "
                                 f"IC95 {fmt(vc['ci'][0], 2)} a {fmt(vc['ci'][1], 2)}, p = {fmt(vc['p'], 3)}.")
        bb = summ.get("back_to_back")
        if bb:
            book.add("reuniões", f"reuniões coladas na anterior (< 10 min de pausa, n = {bb['n_b2b']}): {fmt(bb['mean_b2b'], 2)} z durante; "
                                 f"com pausa (n = {bb['n_break']}): {fmt(bb['mean_break'], 2)} z; diferença {fmt(bb['diff'], 2)} z, p = {fmt(bb['p'], 3)}.")
        for row in summ.get("by_speaker", [])[:5]:
            book.add("falante", f"quando {row['label']} fala: {fmt(row['mean_dz'], 2)} z (IC95 {fmt(row['ci_low'], 2)} a {fmt(row['ci_high'], 2)}, n = {row['n']} trechos, defasagem {summ['lag_s']} s).")
        for row in summ.get("by_tag", [])[:8]:
            book.add("tema", f"trechos marcados \"{row['label']}\": {fmt(row['mean_dz'], 2)} z (IC95 {fmt(row['ci_low'], 2)} a {fmt(row['ci_high'], 2)}, n = {row['n']}).")

    # validation against your own labels: how many "estresse" marks fall inside a detected episode
    marks = [e for e in store.events(conn, a, b, kinds=["note"]) if "estress" in (e.title or "").lower()]
    if marks:
        eps = []
        d = start
        while d <= end:
            day = store.get_artefact(conn, "analysis:day", d.isoformat())
            if day:
                eps += day.get("episodes", [])
            d += timedelta(days=1)
        hit = sum(1 for mk in marks if any(e["start_ms"] - 10 * 60_000 <= mk.start_ms <= e["end_ms"] + 10 * 60_000 for e in eps))
        book.add("validação", f"{hit} de {len(marks)} marcações de estresse feitas no relógio caíram dentro (±10 min) de episódios detectados.")

    commits = conn.execute(
        "SELECT start_ms FROM events WHERE kind = 'commit' AND start_ms >= ? AND start_ms < ?", (a, b)
    ).fetchall()
    if commits:
        late = sum(1 for c in commits if int(hhmm(c["start_ms"], tz)[:2]) >= 22 or int(hhmm(c["start_ms"], tz)[:2]) < 5)
        book.add("código", f"{len(commits)} commits no período, {late} entre 22h e 5h.")
    return book
