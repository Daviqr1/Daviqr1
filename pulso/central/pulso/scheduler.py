"""`pulso daemon`: the periodic jobs, in one small loop (no cron/Celery to operate).

    every 5 min        re-analyse today (derived series, episodes; the face reads this) and evaluate
                       the alerts
    every 6 h          Garmin Connect sync of the last 2 days (if credentials/tokens exist)
    from 04:30         range job for the last 3 days + today (sleep nights, meetings, controls), once a
                       day, on the first pass after 04:30 (a laptop that slept through 04:30 catches up)
    from 04:30         last night: summarised as soon as its staging is in and final (checked every
                       15 min and right after each Garmin sync); 05:00-12:00, while it is missing, the
                       Garmin sync also runs hourly (today only)
    06:30-12:00        daily report with Claude for yesterday, Monday also the weekly one (if
                       ANTHROPIC_API_KEY or a profile exists), once a day
    from 06:45         summary export, all three Docs, once a day; the 'Hoje' Doc alone every 2 h

"Once a day" survives restarts: a job that already ran fine today (its 'job' artefact) is not redone.
Each job is isolated: a failure (any exception, SystemExit included) is logged as an artefact and
the loop goes on; only Ctrl+C (KeyboardInterrupt) or the process being stopped ends it.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pulso import analysis, config, jobs, store


def _log(conn, name: str, ok: bool, detail: str = "") -> None:
    try:
        store.put_artefact(conn, "job", name, {"ok": ok, "at": store.now_ms(), "detail": detail[-2000:]})
    except Exception:  # a locked/broken store must not take the loop down with it
        pass
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {name}: {'ok' if ok else 'ERRO'} {detail[:200]}", flush=True)


def _run(conn, name: str, fn) -> bool:
    try:
        out = fn()
        _log(conn, name, True, str(out)[:500])
        return True
    except (Exception, SystemExit):  # libraries that "exit" (missing extra, no login) fail the job, not the loop
        _log(conn, name, False, traceback.format_exc())
        return False


def _done_since(conn, name: str, since: datetime) -> bool:
    """The job already ran fine at or after `since` (a restart must not redo the day's jobs)."""
    try:
        job = store.get_artefact(conn, "job", name)
    except Exception:
        return False
    return bool(job and job.get("ok") and job.get("at", 0) >= since.timestamp() * 1000)


def _night_missing(conn, stamp: str) -> bool:
    try:
        return store.get_artefact(conn, "sleep:night", stamp) is None
    except Exception:
        return False


def _garmin_configured() -> bool:
    return bool(os.environ.get("GARMIN_EMAIL") or os.path.exists(
        os.environ.get("PULSO_GARMIN_TOKENS", os.path.expanduser("~/.garminconnect"))))


def loop(settings: config.Settings | None = None, once: bool = False) -> None:
    settings = settings or config.get()
    tz = ZoneInfo(settings.tz)
    conn = store.connect(settings.db_path)
    last: dict[str, float] = {}
    done_today: set[str] = set()

    def due(name: str, every_s: float) -> bool:
        if time.time() - last.get(name, 0) >= every_s:
            last[name] = time.time()
            return True
        return False

    def export(only: set[str] | None = None) -> str:
        from pulso.export import gdrive, summary

        docs = summary.documents(conn, settings.tz)
        if only:
            docs = [d for d in docs if d.key in only]
        path = summary.write_local(docs, settings.tz)
        _, token, _ = gdrive._paths()
        if token.exists():
            return f"{path} + {list(gdrive.publish_all(docs).values())}"
        return str(path)

    while True:
        now = datetime.now(tz)
        today = now.date()
        stamp = today.isoformat()
        hm = (now.hour, now.minute)

        def at(h: int, m: int) -> datetime:
            return now.replace(hour=h, minute=m, second=0, microsecond=0)

        night_missing = hm >= (4, 30) and _night_missing(conn, stamp)

        sync_days = 0
        if _garmin_configured():
            if due("garmin", 6 * 3600):
                sync_days = 2
                last["garmin-am"] = last["garmin"]  # covers today too: the hourly one counts from here
            elif night_missing and 5 <= now.hour < 12 and due("garmin-am", 3600):
                sync_days = 1  # today only: Garmin files a night under the day you woke up
        synced = False
        if sync_days:
            from pulso.importers import garmin_connect

            synced = _run(conn, "garmin:sync", lambda: garmin_connect.sync(conn, days=sync_days, tz=settings.tz))

        # range job once a day, on the first pass after 04:30 (no fixed window: catch up after sleep)
        if hm >= (4, 30) and f"range:{stamp}" not in done_today:
            done_today.add(f"range:{stamp}")
            if not _done_since(conn, "analyze:range", at(4, 30)):
                _run(conn, "analyze:range", lambda: jobs.run_range(conn, today - timedelta(days=3), today,
                                                                   settings.tz)["meetings"]["n_meetings"])
                night_missing = _night_missing(conn, stamp)

        # last night is usually staged only after the wake-up sync, hours after 04:30: summarise it as
        # soon as it is in, before the alerts below (the morning 'sono' alert reads it)
        if night_missing and (synced or due("sleep", 15 * 60)):
            _run(conn, "sleep:last-night", lambda: (jobs.last_night(conn, today, settings.tz) or {}).get("wake_day"))
            if not _night_missing(conn, stamp):
                last.pop("today", None)  # evaluate the alerts in this pass: 'sono' reads this night

        if due("today", 300):
            def _today():
                from pulso import alerts

                n = analysis.analyze_day(conn, today, settings.tz).summary()["minutes_with_hr"]
                fired = alerts.evaluate(conn, int(time.time() * 1000), settings.tz)
                if fired and os.environ.get("PULSO_GCAL_ALERTS") == "1":
                    from pulso.export import gcal

                    for a in fired:
                        gcal.push(a.as_dict())
                return f"{n} min com FC; alertas: {[a.kind for a in fired]}"

            _run(conn, "analyze:today", _today)

        has_claude = bool(settings.anthropic_api_key or os.path.exists(os.path.expanduser("~/.config/anthropic")))
        # 06:30 report, 06:45 export: a Gemini Schedule at 08:00 prepares its answer during the hour
        # before delivery, so the Docs must already be fresh by ~06:45
        if (has_claude and hm >= (6, 30) and now.hour < 12 and f"daily:{stamp}" not in done_today
                and not _done_since(conn, "report:daily", at(6, 30))):
            done_today.add(f"daily:{stamp}")
            from pulso.ai import report

            y = today - timedelta(days=1)
            _run(conn, "report:daily", lambda: report.generate(conn, y, y, settings.tz, settings=settings).get("headline"))
            if today.weekday() == 0:
                _run(conn, "report:weekly", lambda: report.generate(conn, today - timedelta(days=7), y, settings.tz,
                                                                     kind="weekly", settings=settings).get("headline"))
        # health summary for Gemini Notebook / Gemini: all three Docs every morning after the report
        # (local files always, Google Docs when the Google login exists), under its own key so the
        # 2-hourly 'Hoje' refresh can never stand in for it; then 'Hoje' alone every 2 h
        if hm >= (6, 45) and f"export:{stamp}" not in done_today:
            done_today.add(f"export:{stamp}")
            if not _done_since(conn, "export:summary", at(6, 45)):
                last["export"] = time.time()
                _run(conn, "export:summary", export)
        elif due("export", 2 * 3600):
            _run(conn, "export:hoje", lambda: export({"hoje"}))
        if len(done_today) > 50:
            done_today = {k for k in done_today if k.endswith(stamp)}
        if once:
            return
        time.sleep(30)
