"""HTTP ingest for the watch (via Garmin Connect Mobile's makeWebRequest proxy) and the BLE bridge.

Connect IQ constraints that shape this API: HTTPS with a publicly trusted certificate, JSON
bodies, a ~8 KB practical response ceiling (much lower in background services), one request in
flight. So responses are tiny and the face state rides on the ingest response itself — one
round trip per background run instead of two.

The endpoint is on the public internet (tunnel or VPS), so an upload is authenticated before a
byte of its body is read, and the body is capped while it streams in (chunked uploads carry no
Content-Length).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from pulso import analysis, config, store
from pulso.face import face_state
from pulso.ingest import auth
from pulso.ingest.handler import ingest
from pulso.ingest.protocol import Ack, Batch

MAX_BODY = 64 * 1024

log = logging.getLogger(__name__)


class BodyTooLarge(HTTPException):
    def __init__(self) -> None:
        super().__init__(status_code=413, detail="too large")


class BodyLimit:
    """Pure ASGI middleware: refuses a declared Content-Length over the cap up front, and counts the
    body as the app pulls it, so a chunked upload is cut at `max_body` instead of buffered whole."""

    def __init__(self, app, max_body: int = MAX_BODY) -> None:
        self.app = app
        self.max_body = max_body

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cl = next((v for k, v in scope.get("headers", ()) if k == b"content-length"), None)
        if cl is not None:
            if not cl.strip().isdigit():
                await _reply(send, 400, "bad content-length")
                return
            if int(cl) > self.max_body:
                await _reply(send, 413, "too large")
                return
        seen = 0
        started = False

        async def capped_receive():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_body:
                    raise BodyTooLarge()
            return msg

        async def tracked_send(msg) -> None:
            nonlocal started
            if msg["type"] == "http.response.start":
                started = True
            await send(msg)

        try:
            await self.app(scope, capped_receive, tracked_send)
        except BodyTooLarge:
            if started:
                raise
            await _reply(send, 413, "too large")


async def _reply(send, status: int, err: str) -> None:
    body = json.dumps({"ok": 0, "err": err}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                            (b"connection", b"close")]})
    await send({"type": "http.response.body", "body": body})


def _is_json(content_type: str | None) -> bool:
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    return mime == "application/json" or (mime.startswith("application/") and mime.endswith("+json"))


def create_app(settings: config.Settings | None = None, conn: sqlite3.Connection | None = None) -> FastAPI:
    settings = settings or config.get()
    db = conn or store.connect(settings.db_path)
    lock = threading.Lock()  # sqlite connection shared across the threadpool: serialise access
    last_analysis: dict[str, int] = {}
    app = FastAPI(title="Pulso central", version="0.1.0", docs_url=None, redoc_url=None)
    app.add_middleware(BodyLimit, max_body=MAX_BODY)

    @app.exception_handler(BodyTooLarge)
    async def too_large(request: Request, exc: BodyTooLarge) -> JSONResponse:
        return JSONResponse({"ok": 0, "err": "too large"}, status_code=413, headers={"connection": "close"})

    def bearer(scope: str):
        def dep(authorization: str | None = Header(default=None)) -> str:
            with lock:
                hit = auth.lookup(db, authorization)
            if hit is None:
                raise HTTPException(status_code=401, detail="bad token")
            if hit[1] != scope:
                raise HTTPException(status_code=403, detail=f"token is not a {scope} token")
            return hit[0]
        return dep

    device = bearer(auth.DEVICE)        # the watch and the bridge: upload + face state
    painel_read = bearer(auth.PAINEL)   # the dashboard: reads the whole history

    async def authed_batch(request: Request, label: str = Depends(device)) -> Batch:
        """The token is checked first (a sub-dependency), so an unauthenticated upload is refused
        before its body is read; BodyLimit caps the read itself."""
        if not _is_json(request.headers.get("content-type")):  # FastAPI's strict content type, as before
            raise RequestValidationError([{"type": "content_type", "loc": ("body",), "msg": "expected application/json"}])
        raw = await request.body()
        try:
            return Batch.model_validate_json(raw)
        except ValidationError as e:  # 422 as before; no input echo (the watch reads tiny responses)
            raise RequestValidationError([{"type": err["type"], "loc": ("body", *err["loc"]), "msg": err["msg"]}
                                          for err in e.errors(include_url=False)]) from None

    @app.get("/health")
    def health() -> dict:
        return {"ok": 1}

    @app.post("/v1/ingest", response_model=Ack, response_model_exclude_none=True)
    def post_ingest(batch: Batch = Depends(authed_batch)) -> Ack:
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

        return web.html()  # the page carries no data; it fetches with a painel token

    @app.get("/v1/painel/dia")
    def painel_dia(data: str | None = None, label: str = Depends(painel_read)) -> dict:
        from pulso import web

        d = date.fromisoformat(data) if data else datetime.now(ZoneInfo(settings.tz)).date()
        with lock:
            if store.get_artefact(db, "analysis:day", d.isoformat()) is None:
                analysis.analyze_day(db, d, settings.tz)
            return web.day_payload(db, d, settings.tz)

    @app.get("/v1/painel/tabela")
    def painel_tabela(dias: int = 30, label: str = Depends(painel_read)) -> dict:
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
        try:
            from pulso import alerts

            fired = alerts.evaluate(db, now, settings.tz)
        except Exception as e:
            store.put_artefact(db, "error:alerts", key, {"error": repr(e), "at": now})
            return
        # an alert fires once, in whichever evaluator (this upload or the scheduler) sees it first,
        # and the scheduler pushes only its own: alerts first seen here are pushed from here
        if fired and os.environ.get("PULSO_GCAL_ALERTS") == "1":
            _push_gcal([a.as_dict() for a in fired])

    def _push_gcal(fired: list[dict]) -> None:
        """Off the request thread: the watch is waiting for the ack, and a Google outage or a missing
        login must cost the upload nothing. gcal.push is idempotent by alert id."""

        def run() -> None:
            for a in fired:
                try:
                    from pulso.export import gcal

                    gcal.push(a)
                except (Exception, SystemExit) as e:  # NeedsLogin, HttpError, google libs missing
                    log.warning("gcal: alerta %s não publicado: %r", a.get("id"), e)
                    try:
                        with lock:
                            store.put_artefact(db, "error:gcal", str(a.get("id")), {"error": repr(e), "at": store.now_ms()})
                    except Exception:
                        pass

        threading.Thread(target=run, name="pulso-gcal", daemon=True).start()

    app.state.db = db
    app.state.settings = settings
    return app


def reanalyze(db: sqlite3.Connection, day: date, tz: str) -> dict:
    return analysis.analyze_day(db, day, tz).summary()
