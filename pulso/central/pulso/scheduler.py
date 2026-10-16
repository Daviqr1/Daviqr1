"""`pulso daemon`: the periodic jobs, in one small loop (no cron/Celery to operate).

    every 5 min   re-analyse today (derived series, episodes) — the face reads this
    every 6 h     Garmin Connect sync of the last 2 days (if credentials/tokens exist)
    04:30 local   full range job for the last 3 days (sleep nights, meetings, controls)
    07:00 local   daily report with Claude for yesterday (if ANTHROPIC_API_KEY or a profile exists)
    Monday 07:10  weekly report

Each job is isolated: a failure is logged as an artefact and the loop goes on.
"""

from __future__ import annotations

import os
import time
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pulso import analysis, config, jobs, store


def _log(conn, name: str, ok: bool, detail: str = "") -> None:
    store.put_artefact(conn, "job", name, {"ok": ok, "at": store.now_ms(), "detail": detail[-2000:]})
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {name}: {'ok' if ok else 'ERRO'} {detail[:200]}", flush=True)


def _run(conn, name: str, fn) -> None:
    try:
        out = fn()
        _log(conn, name, True, str(out)[:500])
    except Exception:
        _log(conn, name, False, traceback.format_exc())


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

    while True:
        now = datetime.now(tz)
        today = now.date()
        stamp = today.isoformat()

        if due("today", 300):
            _run(conn, "analyze:today", lambda: analysis.analyze_day(conn, today, settings.tz).summary()["minutes_with_hr"])

        if due("garmin", 6 * 3600) and (os.environ.get("GARMIN_EMAIL") or os.path.exists(
                os.environ.get("PULSO_GARMIN_TOKENS", os.path.expanduser("~/.garminconnect")))):
            from pulso.importers import garmin_connect

            _run(conn, "garmin:sync", lambda: garmin_connect.sync(conn, days=2, tz=settings.tz))

        if now.hour == 4 and now.minute >= 30 and f"range:{stamp}" not in done_today:
            done_today.add(f"range:{stamp}")
            _run(conn, "analyze:range", lambda: jobs.run_range(conn, today - timedelta(days=3), today, settings.tz)["meetings"]["n_meetings"])

        has_claude = bool(settings.anthropic_api_key or os.path.exists(os.path.expanduser("~/.config/anthropic")))
        if has_claude and now.hour == 7 and f"daily:{stamp}" not in done_today:
            done_today.add(f"daily:{stamp}")
            from pulso.ai import report

            y = today - timedelta(days=1)
            _run(conn, "report:daily", lambda: report.generate(conn, y, y, settings.tz, settings=settings).get("headline"))
            if today.weekday() == 0:
                _run(conn, "report:weekly", lambda: report.generate(conn, today - timedelta(days=7), y, settings.tz,
                                                                     kind="weekly", settings=settings).get("headline"))
        if len(done_today) > 50:
            done_today = {k for k in done_today if k.endswith(stamp)}
        if once:
            return
        time.sleep(30)
