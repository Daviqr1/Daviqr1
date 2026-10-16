"""HTTP ingest for the watch (via Garmin Connect Mobile's makeWebRequest proxy) and the BLE bridge.

Connect IQ constraints that shape this API: HTTPS with a publicly trusted certificate, JSON
bodies, a ~8 KB practical response ceiling (much lower in background services), one request in
flight. So responses are tiny and the face state rides on the ingest response itself — one
round trip per background run instead of two.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from pulso import analysis, config, store
from pulso.face import face_state
from pulso.ingest import auth
from pulso.ingest.handler import ingest
from pulso.ingest.protocol import Ack, Batch

MAX_BODY = 64 * 1024


def create_app(settings: config.Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or config.get()
    db = conn or store.connect(settings.db_path)
    lock = threading.Lock()  # sqlite connection shared across the threadpool: serialise access
    last_analysis: dict[str, int] = {}
    app = FastAPI(title="Pulso central", version="0.1.0", docs_url=None, redoc_url=None)

    def device(authorization: str | None = Header(default=None)) -> str:
        with lock:
            label = auth.check(db, authorization)
        if label is None:
            raise HTTPException(status_code=401, detail="bad token")
        return label

    @app.middleware("http")
    async def limit_body(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl and int(cl) > MAX_BODY:
            return JSONResponse({"ok": 0, "err": "too large"}, status_code=413)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict:
        return {"ok": 1}

    @app.post("/v1/ingest", response_model=Ack, response_model_exclude_none=True)
    def post_ingest(batch: Batch, label: str = Depends(device)) -> Ack:
        now = store.now_ms()
        with lock:
            res = ingest(db, batch, now_ms=now)
            face = None
            if batch.src == "face":
                _maybe_reanalyze(now)
                face = face_state(db, now, settings.tz)
        return Ack(ack=batch.q, dup=int(res["dup"]), face=face)

    @app.get("/v1/face")
    def get_face(label: str = Depends(device)) -> dict:
        now = store.now_ms()
        with lock:
            _maybe_reanalyze(now)
            return face_state(db, now, settings.tz)

    @app.get("/painel", response_class=HTMLResponse)
    def painel() -> str:
        from pulso import web

        return web.html()  # the page carries no data; it fetches with the bearer token

    @app.get("/v1/painel/dia")
    def painel_dia(data: str | None = None, label: str = Depends(device)) -> dict:
        from pulso import web

        d = date.fromisoformat(data) if data else datetime.now(ZoneInfo(settings.tz)).date()
        with lock:
            if store.get_artefact(db, "analysis:day", d.isoformat()) is None:
                analysis.analyze_day(db, d, settings.tz)
            return web.day_payload(db, d, settings.tz)

    @app.get("/v1/painel/tabela")
    def painel_tabela(dias: int = 30, label: str = Depends(device)) -> dict:
        from pulso import web

        with lock:
            return web.table_payload(db, max(1, min(dias, 366)), settings.tz)

    def _maybe_reanalyze(now: int) -> None:
        """Keep today's derived series fresh enough for the face without recomputing per request."""
        today = datetime.fromtimestamp(now / 1000, tz=timezone.utc).astimezone(ZoneInfo(settings.tz)).date()
        key = today.isoformat()
        if now - last_analysis.get(key, 0) < settings.reanalyze_after_s * 1000:
            return
        last_analysis[key] = now
        try:
            analysis.analyze_day(db, today, settings.tz)
        except Exception as e:  # never fail an upload because analysis failed
            store.put_artefact(db, "error:analysis", key, {"error": repr(e), "at": now})

    app.state.db = db
    app.state.settings = settings
    return app


def reanalyze(db: sqlite3.Connection, day: date, tz: str) -> dict:
    return analysis.analyze_day(db, day, tz).summary()
