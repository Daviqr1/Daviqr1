"""Alerts: few, specific, actionable — or none.

Rules (each with a cooldown so the watch does not nag):

    agora      arousal of the last 10 min > 2 z while still (not exercise)       cooldown 60 min
    reunião    a meeting starts in <= 15 min and similar meetings weighed >= 2   once per meeting
    sequência  3+ meetings ahead today with < 10 min between them               once per day
    sono       last night: sleep debt > 5 h or HRV below your normal range        once per morning

Alerts are artefacts ('alert', id). The face state carries the newest unseen one (the watch shows
a native notification once per id), the MCP exposes them, and the NotebookLM summary lists them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import numpy as np

from pulso import metrics as M
from pulso import store
from pulso.face import predicted_meeting_load

MIN = 60_000


@dataclass
class Alert:
    id: str
    kind: str
    at_ms: int
    text: str          # <= 60 chars, shown on the watch
    detail: str        # longer, for the summary / MCP

    def as_dict(self) -> dict:
        return asdict(self)


def _hhmm(ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%H:%M")


def _recent(conn: sqlite3.Connection, kind: str, since_ms: int) -> bool:
    rows = conn.execute("SELECT body FROM artefacts WHERE kind = 'alert' AND created_ms >= ?", (since_ms,)).fetchall()
    import json

    return any(json.loads(r["body"]).get("kind") == kind for r in rows)


def evaluate(conn: sqlite3.Connection, now_ms: int, tz: str) -> list[Alert]:
    """Evaluate all rules at `now_ms`, persist and return the alerts that fired now."""
    fired: list[Alert] = []
    zone = ZoneInfo(tz)
    today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(zone).date().isoformat()

    # agora: sustained high arousal while still
    ts, z = store.series(conn, M.AROUSAL_Z, now_ms - 10 * MIN, now_ms + 1, source=M.DERIVED)
    if z.size >= 8 and float(np.nanmean(z)) > 2.0 and not _recent(conn, "agora", now_ms - 60 * MIN):
        _, steps = store.series(conn, M.STEPS_PM, now_ms - 10 * MIN, now_ms + 1)
        if steps.size == 0 or float(np.nanmean(steps)) < 20:
            fired.append(Alert(f"agora:{now_ms // MIN}", "agora", now_ms,
                               "Ativação alta há 10 min. 1 min de respiração lenta?",
                               f"Ativação média de {np.nanmean(z):.1f} z nos últimos 10 min, parado."))

    # reunião: heavy meeting coming up
    upcoming = [e for e in store.events(conn, now_ms, now_ms + 15 * MIN, kinds=["calendar", "meeting"]) if e.start_ms >= now_ms]
    for ev in upcoming:
        aid = f"reuniao:{ev.id}"
        if conn.execute("SELECT 1 FROM artefacts WHERE kind = 'alert' AND key = ?", (aid,)).fetchone():
            continue
        risk = predicted_meeting_load(conn, ev.title, ev.attrs.get("attendees"))
        if risk is not None and risk >= 2:
            fired.append(Alert(aid, "reuniao", now_ms, f"{(ev.title or 'Reunião')[:28]} às {_hhmm(ev.start_ms, tz)}: costuma pesar",
                               f"Reuniões parecidas com '{ev.title}' tiveram carga {risk}/3. Dois minutos de respiração antes ajudam."))

    # sequência: back-to-back block ahead today
    day_end = int(datetime.fromisoformat(today).replace(tzinfo=zone).timestamp() * 1000) + 24 * 60 * MIN
    ahead = sorted(store.events(conn, now_ms, day_end, kinds=["calendar", "meeting"]), key=lambda e: e.start_ms)
    run = best = 1
    for a, b in zip(ahead, ahead[1:]):
        run = run + 1 if b.start_ms - a.end_ms < 10 * MIN else 1
        best = max(best, run)
    if len(ahead) >= 3 and best >= 3 and not _recent(conn, "sequencia", now_ms - 18 * 3600_000):
        fired.append(Alert(f"sequencia:{today}", "sequencia", now_ms, f"{best} reuniões coladas hoje. Encaixe pausas de 10 min",
                           f"Há {best} reuniões em sequência com menos de 10 min entre elas."))

    # sono: once per morning
    night = store.get_artefact(conn, "sleep:last", "latest")
    hour = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(zone).hour
    if night and 5 <= hour <= 11 and night.get("wake_day") == today and not _recent(conn, "sono", now_ms - 12 * 3600_000):
        debt = night.get("debt_min") or 0
        if debt > 300 or night.get("hrv_status") == "below":
            why = []
            if debt > 300:
                why.append(f"débito de sono de {debt / 60:.0f} h em 14 dias")
            if night.get("hrv_status") == "below":
                why.append("HRV noturna abaixo da sua faixa normal")
            fired.append(Alert(f"sono:{today}", "sono", now_ms, "Recuperação baixa hoje: alivie a agenda se der",
                               (lambda t: t[:1].upper() + t[1:])("; ".join(why)) + "."))

    with store.transaction(conn):
        for a in fired:
            store.put_artefact(conn, "alert", a.id, a.as_dict())
    return fired


def recent(conn: sqlite3.Connection, since_ms: int) -> list[dict]:
    import json

    rows = conn.execute("SELECT body FROM artefacts WHERE kind = 'alert' AND created_ms >= ? ORDER BY created_ms DESC",
                        (since_ms,)).fetchall()
    return [json.loads(r["body"]) for r in rows]
