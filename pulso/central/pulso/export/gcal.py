"""Real-time alerts to the phone through Google Calendar (optional, PULSO_GCAL_ALERTS=1).

No program can push into the Gemini app; a Calendar event with a popup reminder at minute 0 is the
Google-native push a program *can* trigger, and Gemini can also read it later. One event per alert
id (idempotent), 5 minutes long, starting a minute from now; `doc_url`, when a caller passes one,
adds a link (the scheduler and the ingest server do not). The text is the alert's as is: not
pseudonymised (a meeting alert carries the meeting title).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from pulso.export import gdrive


def push(alert: dict, doc_url: str | None = None, svc=None) -> str:
    from googleapiclient.discovery import build

    svc = svc or build("calendar", "v3", credentials=gdrive.credentials(), cache_discovery=False)
    start = datetime.now(timezone.utc) + timedelta(minutes=1)
    body = {
        "id": "pulso" + "".join(c for c in alert["id"].lower() if c in "abcdefghijklmnopqrstuv0123456789")[:900],
        "summary": f"⚠ Pulso: {alert['text']}",
        "description": alert.get("detail", "") + (f"\n\nResumo: {doc_url}" if doc_url else ""),
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": (start + timedelta(minutes=5)).isoformat()},
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 0}]},
        "transparency": "transparent",   # doesn't block your agenda
        "visibility": "private",
    }
    from googleapiclient.errors import HttpError

    try:
        ev = svc.events().insert(calendarId="primary", body=body).execute(num_retries=3)
    except HttpError as e:
        if getattr(getattr(e, "resp", None), "status", None) == 409:  # already pushed
            return body["id"]
        raise
    return ev["id"]
