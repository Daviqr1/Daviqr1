"""Facts in, verified prose out.

The report model never sees raw series. It receives a numbered list of *facts* — sentences
with numbers already computed by the deterministic pipeline — and must cite fact ids for every
claim. Then the numbers gate checks that every number, date, weekday and clock time it wrote exists
in the facts it cited (tolerating rounding only), with or without a unit glued to it ("25ms", "14h"),
small counts and number words ("onze") included. An insight that fails the gate is not published; it is listed under "rejected" so
you can see what the model tried to say. Colleagues' names are pseudonymised before the facts leave
the machine (see `ai.names` and `report.generate`).
"""

from __future__ import annotations

import json
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


def pct(frac: float | None) -> float | None:
    """0..1 -> percent; missing stays missing (never an invented 0 %)."""
    return None if frac is None or (isinstance(frac, float) and math.isnan(frac)) else frac * 100


def hhmm(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%H:%M")


WEEKDAYS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]


def day_label(d: date | str) -> str:
    """'2026-10-15 (quinta-feira)': the model copies the weekday instead of working it out."""
    d = date.fromisoformat(d) if isinstance(d, str) else d
    return f"{d.isoformat()} ({WEEKDAYS[d.weekday()]})"


@dataclass
class Fact:
    id: str
    text: str
    topic: str
    numbers: list[float] = field(default_factory=list)
    times: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)
    weekdays: list[int] = field(default_factory=list)   # 0 = Monday: of its full dates, and any it names


class FactBook:
    def __init__(self) -> None:
        self.facts: list[Fact] = []

    def add(self, topic: str, text: str) -> str:
        fid = f"F{len(self.facts) + 1}"
        self.facts.append(_fact(fid, text, topic))
        return fid

    def by_id(self) -> dict[str, Fact]:
        return {f.id: f for f in self.facts}

    def render(self) -> str:
        return "\n".join(f"[{f.id}] ({f.topic}) {f.text}" for f in self.facts)

    def masked(self, T) -> FactBook:
        """Same ids and topics with every text passed through T (names -> pseudonyms): what is sent
        to Claude, and what its answer is checked against."""
        out = FactBook()
        out.facts = [_fact(f.id, T(f.text), f.topic) for f in self.facts]
        return out


def _fact(fid: str, text: str, topic: str) -> Fact:
    dates = extract_dates(text)
    days = set(extract_weekdays(text))
    for d in dates:
        try:
            days.add(date.fromisoformat(d).weekday())
        except ValueError:  # '--10-15' (no year) or an impossible day
            pass
    return Fact(fid, text, topic, extract_numbers(text), extract_times(text), dates, sorted(days))


# Tokens the gate checks, in this order (each one is blanked out before the next pattern runs):
#   dates   2026-10-15, 15/10, 15/10/2026, "15 de outubro"   -> must be a date of a cited fact
#   times   14:32, 9h05, 7h30                                 -> must be a time of a cited fact
#   hours   14h, 4h (no minutes: a clock hour or a duration)  -> a cited number or a cited time's hour
#   numbers 2,4  -0,5  25ms  2,8z  90min  15%                 -> a cited number (rounding tolerated)
#           dois..dez, onze..dezenove, vinte..noventa (e um..nove), cem
#   weekday quinta, quinta-feira, sábado (bare "segunda" is also an ordinal: not checked)
#                                                             -> the weekday of a cited date (or named in it)
# A unit glued to the number ("25ms") does not hide it, and no number is exempt for being small.
_FID = re.compile(r"\bF\d+\b")                                   # fact ids are not numbers
_ISO = re.compile(r"(?<![\d-])(\d{4})-(\d{2})-(\d{2})(?!\d)")
_MONTHS = {"janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5, "junho": 6, "julho": 7,
           "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12}
_DMONTH = re.compile(r"(?<![\w.,])(\d{1,2})[º°]?\s+de\s+(" + "|".join(_MONTHS) + r")(?:\s+de\s+(\d{4}))?\b", re.IGNORECASE)
_DMY = re.compile(r"(?<![\w.,/])(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?![\d/])")
_TIME = re.compile(r"(?<![\d.,:])([01]?\d|2[0-3])[:h]([0-5]\d)(?![\d:])")
_HOUR = re.compile(r"(?<![\w.,:])([01]?\d|2[0-3])h(?!\w)")
_NUM = re.compile(r"(?<![\w.,])[-−]?\d+(?:[.,]\d+)?(?!\d)")
_UNITS = {"um": 1, "uma": 1, "dois": 2, "duas": 2, "três": 3, "tres": 3, "quatro": 4, "cinco": 5, "seis": 6,
          "sete": 7, "oito": 8, "nove": 9}
_TENS = {"vinte": 20, "trinta": 30, "quarenta": 40, "cinquenta": 50, "cinqüenta": 50, "sessenta": 60,
         "setenta": 70, "oitenta": 80, "noventa": 90}
_WORDNUM = {k: v for k, v in _UNITS.items() if v > 1} | {   # "um/uma" alone are also articles: not checked
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "catorze": 14, "quatorze": 14, "quinze": 15,
    "dezesseis": 16, "dezasseis": 16, "dezessete": 17, "dezassete": 17, "dezoito": 18, "dezenove": 19,
    "dezanove": 19, "cem": 100} | _TENS
_NUMWORD = re.compile(r"\b(?:(" + "|".join(_TENS) + r")\s+e\s+(" + "|".join(_UNITS) + r")|(" + "|".join(_WORDNUM) + r"))\b",
                      re.IGNORECASE)                       # "vinte e cinco" is 25, not 20 and 5
_WEEKDAY = re.compile(r"\b(?:(segunda|terça|terca|quarta|quinta|sexta)s?-feiras?|(terça|terca|quarta|quinta|sexta)s?"
                      r"(?!\s+(?:vez|vezes|parte)\b)|(sábado|sabado|domingo)s?)\b", re.IGNORECASE)
_WEEKDAY_N = {"segunda": 0, "terça": 1, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sábado": 5, "sabado": 5, "domingo": 6}


@dataclass
class _Tok:
    pos: int
    kind: str            # date | time | hour | num | weekday
    value: float | str
    raw: str
    nd: int = 0          # decimals written (num)
    ratio: tuple[float, float] | None = None   # "3/5" may be a ratio rather than a date


def _date(y: int | None, m: int, d: int) -> str | None:
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    return f"{y:04d}-{m:02d}-{d:02d}" if y is not None else f"--{m:02d}-{d:02d}"


def _scan(text: str) -> list[_Tok]:
    toks: list[_Tok] = []
    t = _FID.sub(lambda m: " " * len(m.group(0)), text)

    def take(rx: re.Pattern, make) -> None:
        nonlocal t
        out, last = [], 0
        for m in rx.finditer(t):
            tok = make(m)
            if tok is None:
                continue
            toks.append(tok)
            out += [t[last : m.start()], " " * (m.end() - m.start())]
            last = m.end()
        t = "".join(out) + t[last:]

    def year(s: str | None) -> int | None:
        return None if s is None else int(s) + (2000 if len(s) == 2 else 0)

    def iso(m):
        d = _date(int(m[1]), int(m[2]), int(m[3]))
        return d and _Tok(m.start(), "date", d, m[0])

    def dmonth(m):
        d = _date(year(m[3]), _MONTHS[m[2].lower()], int(m[1]))
        return d and _Tok(m.start(), "date", d, m[0])

    def dmy(m):
        d = _date(year(m[3]), int(m[2]), int(m[1]))
        return d and _Tok(m.start(), "date", d, m[0], ratio=None if m[3] else (float(m[1]), float(m[2])))

    take(_ISO, iso)
    take(_DMONTH, dmonth)
    take(_DMY, dmy)
    take(_TIME, lambda m: _Tok(m.start(), "time", f"{int(m[1]):02d}:{m[2]}", m[0]))
    take(_HOUR, lambda m: _Tok(m.start(), "hour", float(m[1]), m[0]))
    for m in _NUM.finditer(t):
        raw = m.group(0)
        frac = re.split(r"[.,]", raw)
        toks.append(_Tok(m.start(), "num", float(raw.replace("−", "-").replace(",", ".")), raw,
                         nd=len(frac[1]) if len(frac) > 1 else 0))
    for m in _NUMWORD.finditer(t):
        v = _TENS[m[1].lower()] + _UNITS[m[2].lower()] if m[1] else _WORDNUM[m[3].lower()]
        toks.append(_Tok(m.start(), "num", float(v), m[0]))
    for m in _WEEKDAY.finditer(t):
        toks.append(_Tok(m.start(), "weekday", _WEEKDAY_N[(m[1] or m[2] or m[3]).lower()], m[0]))
    return sorted(toks, key=lambda k: k.pos)


def extract_times(text: str) -> list[str]:
    return [str(k.value) for k in _scan(text) if k.kind == "time"]


def extract_numbers(text: str) -> list[float]:
    """Numbers as written, clock-free hours ('22h') included; dates and times are not values."""
    return [float(k.value) for k in _scan(text) if k.kind in ("num", "hour")]


def extract_dates(text: str) -> list[str]:
    return [str(k.value) for k in _scan(text) if k.kind == "date"]


def extract_weekdays(text: str) -> list[int]:
    return [int(k.value) for k in _scan(text) if k.kind == "weekday"]


def _matches(x: float, nd: int, pool: list[float]) -> bool:
    """x, written with nd decimals, is one of pool rounded to nd decimals (sign-insensitive:
    "caiu 0,4" for a fact stored as -0,4). An integer never stands for a fact with decimals
    ("7 h" is not "6,5 h"), and nothing is exempt for being small."""
    tol = 0.5 * 10.0 ** -nd + 1e-9
    for v in pool:
        if nd == 0 and abs(v - round(v)) > 1e-9:
            continue
        if abs(abs(x) - abs(v)) <= tol:
            return True
    return False


def gate(text: str, cited: list[str], book: FactBook) -> list[str]:
    """Return the list of problems (empty = passes)."""
    facts = book.by_id()
    missing = [c for c in cited if c not in facts]
    if missing:
        return [f"cita fatos inexistentes: {', '.join(missing)}"]
    nums = [n for c in cited for n in facts[c].numbers]
    times = {t for c in cited for t in facts[c].times}
    hours = {float(t[:2]) for t in times}
    dates = {d for c in cited for d in facts[c].dates}
    weekdays = {w for c in cited for w in facts[c].weekdays}
    probs = []
    for k in _scan(text or ""):
        if k.kind == "num" and not _matches(float(k.value), k.nd, nums):
            probs.append(f"número {fmt(float(k.value), 2)} não está nos fatos citados")
        elif k.kind == "hour" and not (k.value in hours or _matches(float(k.value), 0, nums)):
            probs.append(f"horário {k.raw} não está nos fatos citados")
        elif k.kind == "time" and k.value not in times:
            probs.append(f"horário {k.value} não está nos fatos citados")
        elif k.kind == "date":
            d = str(k.value)
            ok = d in dates if not d.startswith("--") else any(x.endswith(d[1:]) for x in dates)
            if not ok and k.ratio is not None:
                ok = all(_matches(v, 0, nums) for v in k.ratio)
            if not ok:
                probs.append(f"data {k.raw} não está nos fatos citados")
        elif k.kind == "weekday" and k.value not in weekdays:
            probs.append(f"dia da semana {k.raw} não corresponde às datas dos fatos citados")
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
                book.add("dia", f"{day_label(d)}: ativação média acordado {fmt(day['arousal_mean_awake'], 2)} z; "
                                 f"{day['arousal_minutes_above_1_5']} minutos acima de 1,5 z; {len(day['episodes'])} episódios.")
            for ep in day["episodes"]:
                links = [titles.get(i) or i for i in ep.get("overlaps", [])]
                book.add("episódio", f"{day_label(d)} episódio {ep['kind']} das {hhmm(ep['start_ms'], tz)} às {hhmm(ep['end_ms'], tz)}, "
                                     f"pico {fmt(ep['peak_z'], 1)} z às {hhmm(ep['peak_ms'], tz)}, área {fmt(ep['auc_z_min'], 1)} z·min"
                                     + (f"; coincide com: {', '.join(str(x) for x in links)}" if links else "; sem evento na agenda"))
        night = store.get_artefact(conn, "sleep:night", d.isoformat())
        if night:
            # a value the night does not have is left out, never written as 0 (it would become a "verified" number)
            eff, nadir_at = pct(night.get("efficiency")), pct(night.get("hr_nadir_at_frac"))
            book.add("sono", f"noite que terminou em {day_label(d)}: {fmt(night['total_sleep_min'] / 60, 1)} h de sono, "
                             + (f"eficiência {fmt(eff, 0)} %, " if eff is not None else "")
                             + f"profundo {fmt(night.get('deep_min'), 0)} min, "
                             f"REM {fmt(night.get('rem_min'), 0)} min, {fmt(night.get('awakenings'), 0)} despertares, "
                             f"FC mínima {fmt(night.get('hr_nadir'), 0)} bpm"
                             + (f" a {fmt(nadir_at, 0)} % da noite" if nadir_at is not None else "")
                             + f", débito acumulado {fmt(night.get('debt_min'), 0)} min, HRV {fmt(night.get('hrv_ms'), 0)} ms ({night.get('hrv_status')}).")
        d += timedelta(days=1)

    # the meeting analysis for exactly this range, computed now: the scheduler stores other ranges (last
    # 3 days), and a stored one may predate meetings or data that arrived since (a mid-day `fatos` must
    # not stand for the whole day). It also refreshes the per-meeting responses read just below.
    summ = None
    if conn.execute("SELECT 1 FROM events WHERE kind IN ('meeting', 'calendar') AND start_ms >= ? AND start_ms < ? LIMIT 1",
                    (a, b)).fetchone():
        from pulso import jobs

        summ = jobs.meeting_job(conn, start, end, tz)

    for r in conn.execute("SELECT key, body FROM artefacts WHERE kind = 'meeting_response'"):
        m = json.loads(r["body"])
        if not (a <= m["start_ms"] < b):
            continue
        rec = f"recuperou em {fmt(m['recovery_min'], 0)} min" if m.get("recovery_min") is not None else "não voltou ao basal em 60 min"
        book.add("reunião", f"reunião \"{m.get('title') or '?'}\" em {day_label(m['day'])} às {hhmm(m['start_ms'], tz)}: antecipação {fmt(m.get('anticipation'), 2)} z, "
                            f"durante {fmt(m.get('during'), 2)} z, pico {fmt(m.get('peak'), 2)} z, {rec}.")

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
    if book.facts:  # lets "nos 7 dias" and the period's dates be cited like any other number
        n = (end - start).days + 1
        book.add("período", f"período do relatório: {start.isoformat()} a {end.isoformat()} ({n} {'dia' if n == 1 else 'dias'}).")
    return book
