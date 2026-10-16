"""Hardening of the public ingest API and of the central's local footprint: body limits on streamed
uploads, token scopes, file permissions, the tunnel lifetime and the Calendar push of alerts that the
ingest path fires."""

import asyncio
import hashlib
import json
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from pulso import analysis, config, store
from pulso.ingest import auth
from pulso.ingest.app import MAX_BODY, create_app
from pulso.store import Event

TZ = "America/Sao_Paulo"
MIN = 60_000
CHUNK = 16 * 1024


def _scope(method: str, path: str, headers: dict[str, str]) -> dict:
    return {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method, "scheme": "https",
            "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": ("203.0.113.9", 4711), "server": ("pulso.example", 443)}


def _call(app, path: str, headers: dict[str, str], chunks, method: str = "POST"):
    """Drive the ASGI app directly with a streamed body, counting what the app actually pulls."""
    pulled = {"bytes": 0, "calls": 0}
    it = iter(chunks)
    sent: list[dict] = []

    async def receive():
        pulled["calls"] += 1
        try:
            chunk = next(it)
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}
        pulled["bytes"] += len(chunk)
        return {"type": "http.request", "body": chunk, "more_body": True}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(_scope(method, path, headers), receive, send))
    status = next(m["status"] for m in sent if m["type"] == "http.response.start")
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body, pulled


def _endless():
    # 16 MB of chunked upload with no Content-Length; an app that buffers it all pulls every byte
    for _ in range(1024):
        yield b" " * CHUNK


@pytest.fixture()
def app_tokens():
    conn = store.connect(":memory:")
    dev = auth.create_token(conn, "fr255")
    web = auth.create_token(conn, "web", scope=auth.PAINEL)
    return create_app(config.Settings(), conn=conn), dev, web, conn


# ------------------------------------------------------------------ SEC-3: body limit while streaming


def test_unauthenticated_chunked_upload_is_refused_before_reading_the_body(app_tokens):
    app, _, _, _ = app_tokens
    status, _, pulled = _call(app, "/v1/ingest", {"content-type": "application/json", "transfer-encoding": "chunked"},
                              _endless())
    assert status == 401
    assert pulled["bytes"] == 0
    status, _, pulled = _call(app, "/v1/ingest", {"authorization": "Bearer pulso_dev_wrong", "content-type": "application/json"},
                              _endless())
    assert status == 401 and pulled["bytes"] == 0


def test_painel_token_cannot_upload_and_body_is_not_read(app_tokens):
    app, _, web, _ = app_tokens
    status, _, pulled = _call(app, "/v1/ingest", {"authorization": f"Bearer {web}", "content-type": "application/json"},
                              _endless())
    assert status == 403 and pulled["bytes"] == 0


def test_authenticated_chunked_upload_is_cut_at_the_limit(app_tokens):
    app, dev, _, _ = app_tokens
    h = {"authorization": f"Bearer {dev}", "content-type": "application/json", "transfer-encoding": "chunked"}
    status, body, pulled = _call(app, "/v1/ingest", h, _endless())
    assert status == 413 and json.loads(body) == {"ok": 0, "err": "too large"}
    assert pulled["bytes"] <= MAX_BODY + CHUNK


def test_declared_oversize_and_bad_content_length_never_reach_the_app(app_tokens):
    app, dev, _, _ = app_tokens
    h = {"authorization": f"Bearer {dev}", "content-length": str(MAX_BODY + 1)}
    status, _, pulled = _call(app, "/v1/ingest", h, _endless())
    assert status == 413 and pulled["calls"] == 0
    status, _, pulled = _call(app, "/v1/ingest", {"authorization": f"Bearer {dev}", "content-length": "12abc"}, _endless())
    assert status == 400 and pulled["calls"] == 0


def test_chunked_upload_within_the_limit_still_works(app_tokens):
    app, dev, _, conn = app_tokens
    t = 1_760_630_400
    raw = json.dumps({"v": 1, "d": "fr255-x", "q": 3, "src": "cap", "hr1": [t, [70, 71, 72]]}).encode()
    pieces = [raw[i:i + 7] for i in range(0, len(raw), 7)]
    h = {"authorization": f"Bearer {dev}", "content-type": "application/json; charset=utf-8",
         "transfer-encoding": "chunked"}
    status, body, _ = _call(app, "/v1/ingest", h, pieces)
    assert status == 200, body
    assert json.loads(body)["ack"] == 3
    assert store.series(conn, "hr", 0, 2**62)[1].tolist() == [70, 71, 72]
    # malformed JSON is still a 422, without echoing the body back to the watch
    status, body, _ = _call(app, "/v1/ingest", h, [b'{"v": 1, "d": "fr2'])
    assert status == 422 and b"fr2" not in body
    # and, as with FastAPI's strict content type, a body that does not say it is JSON is not parsed
    status, _, pulled = _call(app, "/v1/ingest", {"authorization": f"Bearer {dev}"}, [raw])
    assert status == 422 and pulled["bytes"] == 0


# ----------------------------------------------------------------- SEC-10: token scopes


def test_device_and_painel_tokens_have_separate_scopes(app_tokens):
    app, dev, web, _ = app_tokens
    c = TestClient(app)
    hd, hw = {"Authorization": f"Bearer {dev}"}, {"Authorization": f"Bearer {web}"}
    body = {"v": 1, "d": "fr255-x", "q": 1, "src": "face"}
    assert c.post("/v1/ingest", json=body, headers=hd).status_code == 200
    assert c.get("/v1/face", headers=hd).status_code == 200
    assert c.get("/v1/painel/tabela?dias=1", headers=hd).status_code == 403
    assert c.get("/v1/painel/dia?data=2026-10-05", headers=hd).status_code == 403
    assert c.get("/v1/painel/tabela?dias=1", headers=hw).status_code == 200
    assert c.post("/v1/ingest", json={**body, "q": 2}, headers=hw).status_code == 403
    assert c.get("/v1/face", headers=hw).status_code == 403


def test_old_database_is_migrated_and_old_tokens_keep_upload_and_face(tmp_path):
    p = tmp_path / "old.sqlite"
    raw = sqlite3.connect(p)
    raw.executescript("""
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_ms INTEGER NOT NULL);
        INSERT INTO schema_version VALUES (1, 0);
        CREATE TABLE device_tokens (token_sha256 TEXT PRIMARY KEY, label TEXT NOT NULL,
                                    created_ms INTEGER NOT NULL, revoked_ms INTEGER);
    """)
    tok = auth.PREFIX + "legacy-token-from-v1"
    raw.execute("INSERT INTO device_tokens VALUES (?, 'fr255', 0, NULL)", (auth._digest(tok),))
    raw.commit()
    raw.close()

    conn = store.connect(p)
    assert auth.lookup(conn, f"Bearer {tok}") == ("fr255", auth.DEVICE)
    assert conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] == 2
    store.connect(p).close()  # idempotent
    c = TestClient(create_app(config.Settings(), conn=conn))
    h = {"Authorization": f"Bearer {tok}"}
    assert c.post("/v1/ingest", json={"v": 1, "d": "fr255-x", "q": 1, "src": "face"}, headers=h).status_code == 200
    assert c.get("/v1/face", headers=h).status_code == 200
    assert c.get("/v1/painel/tabela?dias=1", headers=h).status_code == 403


def test_cli_token_create_painel(tmp_path, capsys):
    from pulso import cli

    db = tmp_path / "p.sqlite"
    assert cli.main(["--db", str(db), "token", "create", "web", "--painel"]) == 0
    web = capsys.readouterr().out.strip()
    assert cli.main(["--db", str(db), "token", "create", "fr255"]) == 0
    dev = capsys.readouterr().out.strip()
    conn = store.connect(db)
    assert auth.lookup(conn, f"Bearer {web}") == ("web", auth.PAINEL)
    assert auth.lookup(conn, f"Bearer {dev}") == ("fr255", auth.DEVICE)
    with pytest.raises(ValueError):
        auth.create_token(conn, "x", scope="admin")


def test_only_device_tokens_are_ble_mac_keys(tmp_path):
    # a painel token lives in a browser's localStorage: it must not sign BLE uploads either
    conn = store.connect(tmp_path / "p.sqlite")
    dev = auth.create_token(conn, "fr255")
    auth.create_token(conn, "web", scope=auth.PAINEL)
    auth.create_token(conn, "old")
    auth.revoke(conn, "old")
    assert auth.mac_keys(conn) == [hashlib.sha256(dev.encode()).digest()]


# ---------------------------------------------------------------- SEC-11: owner-only files


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_store_directory_and_database_files_are_owner_only(tmp_path, monkeypatch):
    old_umask = os.umask(0o022)
    try:
        # an install made by an older version: ~/.pulso 0755, database 0644
        home = tmp_path / "home"
        home.mkdir()
        os.chmod(home, 0o755)
        monkeypatch.setenv("PULSO_HOME", str(home))
        db = home / "pulso.sqlite"
        sqlite3.connect(db).close()
        os.chmod(db, 0o644)
        conn = store.connect(db)
        auth.create_token(conn, "fr255")  # a write, so the WAL exists
        assert _mode(home) == 0o700
        for f in ("pulso.sqlite", "pulso.sqlite-wal", "pulso.sqlite-shm"):
            assert _mode(home / f) == 0o600, f
        conn.close()

        # a fresh install creates the directory 0700 and the database 0600 from the start
        fresh = tmp_path / "x" / ".pulso"
        conn = store.connect(fresh / "pulso.sqlite")
        auth.create_token(conn, "fr255")
        assert _mode(fresh) == 0o700 and _mode(fresh / "pulso.sqlite") == 0o600
        assert _mode(fresh / "pulso.sqlite-wal") == 0o600
        conn.close()

        # an existing directory the user chose (PULSO_DB elsewhere) keeps its mode
        shared = tmp_path / "shared"
        shared.mkdir()
        os.chmod(shared, 0o755)
        store.connect(shared / "p.sqlite").close()
        assert _mode(shared) == 0o755 and _mode(shared / "p.sqlite") == 0o600
    finally:
        os.umask(old_umask)


def test_pulso_home_is_owner_only_even_when_the_database_lives_elsewhere(tmp_path, monkeypatch):
    from pulso import cli

    old_umask = os.umask(0o022)
    try:
        home = tmp_path / ".pulso"
        monkeypatch.setenv("PULSO_HOME", str(home))
        monkeypatch.delenv("PULSO_ARCHIVE", raising=False)
        db = tmp_path / "pulso" / "pulso.sqlite"  # README quick start: PULSO_DB=~/pulso/pulso.sqlite
        monkeypatch.setenv("PULSO_DB", str(db))

        # fresh install: created 0700 before an import writes years of FIT files under it
        assert cli.main(["inventory"]) == 0
        assert _mode(home) == 0o700 and _mode(db.parent) == 0o700 and _mode(db) == 0o600
        fit = config.archive_dir() / "export" / "x.fit"  # what `pulso import garmin-export` does
        fit.parent.mkdir(parents=True, exist_ok=True)
        fit.write_bytes(b".FIT")
        assert _mode(home) == 0o700

        # left 0755 by an older version: the store tightens it wherever the database lives, and so does the CLI
        os.chmod(home, 0o755)
        store.connect(db).close()
        assert _mode(home) == 0o700
        os.chmod(home, 0o755)
        assert cli.main(["inventory"]) == 0
        assert _mode(home) == 0o700
    finally:
        os.umask(old_umask)


def test_pulso_home_pointing_at_the_user_home_is_left_alone(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    os.chmod(user, 0o755)
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("PULSO_HOME", str(user))
    assert config.ensure_home() == user
    store.connect(user / "pulso.sqlite").close()
    assert _mode(user) == 0o755


# ------------------------------------------------------------ SEC-9: the Funnel closes with the process


@pytest.fixture()
def fake_tailscale(monkeypatch, tmp_path):
    from pulso import notebook

    calls: list[list[str]] = []
    rc = {"bg": 0}

    def fake_run(cmd, *a, **k):
        args = list(cmd[1:])
        calls.append(args)
        code = rc["bg"] if "--bg" in args else 0
        out = "# Funnel on:\n#     - https://box.tail1234.ts.net\n" if args[:2] == ["funnel", "status"] else ""
        return subprocess.CompletedProcess(cmd, code, stdout=out, stderr="")

    monkeypatch.setattr(notebook.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(notebook.subprocess, "run", fake_run)
    monkeypatch.setattr(notebook, "_start_api", lambda *a: None)
    settings = config.Settings(db_path=tmp_path / "p.sqlite")
    return notebook, settings, calls, rc


def test_notebook_turns_the_tailscale_funnel_off_on_ctrl_c(fake_tailscale, monkeypatch):
    notebook, settings, calls, _ = fake_tailscale

    def ctrl_c(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(notebook, "_serve", ctrl_c)
    notebook.run(settings, tunnel="tailscale", jobs=False)
    assert calls[0] == ["funnel", "--bg", "8080"]
    # removes only the mount `--bg 8080` created, not other serve config (and no `reset`)
    assert calls[-1] == ["funnel", "--https=443", "--set-path=/", "off"]


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="signals need the main thread")
def test_notebook_turns_the_tailscale_funnel_off_on_sigterm(fake_tailscale, monkeypatch):
    notebook, settings, calls, _ = fake_tailscale
    before = signal.getsignal(signal.SIGTERM)

    def killed(*a):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(5)
        raise AssertionError("SIGTERM did not stop the notebook")

    monkeypatch.setattr(notebook, "_serve", killed)
    notebook.run(settings, tunnel="tailscale", jobs=False)
    assert calls[-1] == ["funnel", "--https=443", "--set-path=/", "off"]
    assert signal.getsignal(signal.SIGTERM) == before


@pytest.mark.skipif(threading.current_thread() is not threading.main_thread(), reason="signals need the main thread")
def test_notebook_handles_sighup_unless_nohup_ignores_it(fake_tailscale, monkeypatch):
    notebook, settings, calls, _ = fake_tailscale
    seen = []

    def serve(*a):
        seen.append(signal.getsignal(signal.SIGHUP))
        raise KeyboardInterrupt

    monkeypatch.setattr(notebook, "_serve", serve)
    before = signal.getsignal(signal.SIGHUP)
    try:
        for disposition in (signal.SIG_DFL, signal.SIG_IGN):
            signal.signal(signal.SIGHUP, disposition)
            notebook.run(settings, tunnel="tailscale", jobs=False)
            assert signal.getsignal(signal.SIGHUP) == disposition  # restored on the way out
    finally:
        signal.signal(signal.SIGHUP, before)
    # a terminal's default SIGHUP becomes the cleanup; `nohup`'s SIG_IGN is kept
    assert seen == [notebook._sigterm, signal.SIG_IGN]


_HANGUP = r'''
import os, signal, subprocess, sys, time
signal.signal(signal.SIGHUP, signal.SIG_DFL)  # as under a terminal, whatever the test runner inherited
from pulso import config, notebook


def fake_run(cmd, *a, **k):
    print(" ".join(cmd[1:]), flush=True)
    out = "# Funnel on:\n#     - https://box.tail1234.ts.net\n" if list(cmd[1:3]) == ["funnel", "status"] else ""
    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def hangup(*a):
    os.kill(os.getpid(), signal.SIGHUP)  # the terminal window closes / the SSH session drops
    time.sleep(5)


notebook.shutil.which = lambda name: "/usr/bin/" + name
notebook.subprocess.run = fake_run
notebook._start_api = lambda *a: None
notebook._serve = hangup
notebook.run(config.Settings(db_path=sys.argv[1]), tunnel="tailscale", jobs=False)
'''


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_notebook_turns_the_tailscale_funnel_off_on_sighup(tmp_path):
    # in a child process: a SIGHUP that is not handled kills whoever receives it
    import pulso

    env = {**os.environ, "PULSO_HOME": str(tmp_path / "home"),
           "PYTHONPATH": os.path.dirname(os.path.dirname(pulso.__file__))}
    r = subprocess.run([sys.executable, "-c", _HANGUP, str(tmp_path / "p.sqlite")], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    lines = r.stdout.splitlines()
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert lines[0] == "funnel --bg 8080"
    assert lines[-1] == "funnel --https=443 --set-path=/ off"


_SECOND_SIGNAL = r'''
import os, signal, subprocess, sys, time
signal.signal(signal.SIGINT, signal.default_int_handler)  # as under a terminal, whatever the test runner inherited
from pulso import config, notebook


def fake_run(cmd, *a, **k):
    args = list(cmd[1:])
    print(" ".join(args), k.get("start_new_session"), flush=True)
    if args[-1] == "off":  # the user presses Ctrl+C again, then systemd/kill follows, while `off` runs
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.3)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.3)
        print("off done", flush=True)
    out = "# Funnel on:\n#     - https://box.tail1234.ts.net\n" if args[:2] == ["funnel", "status"] else ""
    return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")


def ctrl_c(*a):
    raise KeyboardInterrupt


notebook.shutil.which = lambda name: "/usr/bin/" + name
notebook.subprocess.run = fake_run
notebook._start_api = lambda *a: None
notebook._serve = ctrl_c
notebook.run(config.Settings(db_path=sys.argv[1]), tunnel="tailscale", jobs=False)
print(signal.getsignal(signal.SIGINT) is signal.default_int_handler, signal.getsignal(signal.SIGTERM) == signal.SIG_DFL)
'''


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
def test_notebook_second_signal_does_not_cut_the_funnel_shutdown_short(tmp_path):
    """review r2: a second Ctrl+C (or a kill) while `tailscale funnel ... off` runs used to kill it and
    leave the Funnel public; now the shutdown finishes, in its own session, and the handlers come back."""
    import pulso

    env = {**os.environ, "PYTHONPATH": os.path.dirname(os.path.dirname(pulso.__file__))}
    r = subprocess.run([sys.executable, "-c", _SECOND_SIGNAL, str(tmp_path / "p.sqlite")], env=env, cwd=tmp_path,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert r.stdout.splitlines()[-3:] == ["funnel --https=443 --set-path=/ off True", "off done", "True True"]


def test_notebook_undoes_the_funnel_when_interrupted_while_reading_its_url(fake_tailscale, monkeypatch):
    notebook, settings, calls, _ = fake_tailscale
    inner = notebook.subprocess.run

    def run(cmd, *a, **k):
        if list(cmd[1:3]) == ["funnel", "status"]:
            calls.append(list(cmd[1:]))
            raise KeyboardInterrupt  # after `--bg` succeeded, before `stop` reached run()
        return inner(cmd, *a, **k)

    monkeypatch.setattr(notebook.subprocess, "run", run)
    monkeypatch.setattr(notebook, "_serve", lambda *a: pytest.fail("served after Ctrl+C"))
    notebook.run(settings, tunnel="tailscale", jobs=False)
    assert calls == [["funnel", "--bg", "8080"], ["funnel", "status"], ["funnel", "--https=443", "--set-path=/", "off"]]


def test_notebook_prints_the_manual_command_whatever_stops_the_funnel_off(fake_tailscale, monkeypatch, capsys):
    notebook, settings, calls, _ = fake_tailscale
    inner = notebook.subprocess.run

    def run(cmd, *a, **k):
        if cmd[-1] == "off":
            raise KeyboardInterrupt
        return inner(cmd, *a, **k)

    def ctrl_c(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(notebook.subprocess, "run", run)
    monkeypatch.setattr(notebook, "_serve", ctrl_c)
    try:
        notebook.run(settings, tunnel="tailscale", jobs=False)
    except KeyboardInterrupt:
        pytest.fail("the interrupted `off` escaped run() without the manual command")
    assert "rode `tailscale funnel --https=443 --set-path=/ off`" in capsys.readouterr().err


def test_notebook_leaves_serve_config_alone_when_the_funnel_did_not_start(fake_tailscale, monkeypatch):
    notebook, settings, calls, rc = fake_tailscale
    rc["bg"] = 1

    def ctrl_c(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr(notebook, "_serve", ctrl_c)
    notebook.run(settings, tunnel="tailscale", jobs=False)
    assert calls == [["funnel", "--bg", "8080"]]


# ------------------------------------------- F5/F16: alerts first fired by the ingest path reach Calendar


@pytest.fixture()
def back_to_back(monkeypatch):
    """A face upload at 13:00 with three meetings ahead and < 10 min between them: the 'sequencia'
    alert fires in the ingest path (before any scheduler pass)."""
    conn = store.connect(":memory:")
    tok = auth.create_token(conn, "fr255")
    a, _ = analysis.local_day_bounds(date(2026, 10, 7), TZ)
    now = a + 13 * 60 * MIN
    for k, (s, e) in enumerate([(10, 40), (45, 75), (80, 110)]):
        store.upsert_event(conn, Event(f"c{k}", "calendar", now + s * MIN, now + e * MIN, f"R{k}", "ics", {}))
    monkeypatch.setattr(store, "now_ms", lambda: now)
    monkeypatch.setenv("PULSO_GCAL_ALERTS", "1")
    c = TestClient(create_app(config.Settings(tz=TZ), conn=conn))

    def upload():
        return c.post("/v1/ingest", json={"v": 1, "d": "fr255-x", "q": 1, "src": "face", "t": now // 1000},
                      headers={"Authorization": f"Bearer {tok}"})

    return conn, now, upload


def _join_pushers() -> None:
    for t in threading.enumerate():
        if t.name == "pulso-gcal":
            t.join(10)


def test_alert_fired_by_an_upload_is_pushed_to_calendar(back_to_back, monkeypatch):
    from pulso import alerts
    from pulso.export import gcal

    conn, now, upload = back_to_back
    pushed: list[dict] = []
    monkeypatch.setattr(gcal, "push", lambda alert, doc_url=None, svc=None: pushed.append(alert) or "id")
    assert upload().status_code == 200
    _join_pushers()
    assert [p["kind"] for p in pushed] == ["sequencia"]
    assert pushed[0]["id"].startswith("sequencia:")  # the alert id: gcal.push derives its idempotent event id from it
    # the scheduler's next pass finds nothing new, so it pushes nothing: only the ingest path could
    assert alerts.evaluate(conn, now + MIN, TZ) == []


def test_calendar_push_does_not_hold_the_upload(back_to_back, monkeypatch):
    from pulso.export import gcal

    _, _, upload = back_to_back
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_push(alert, doc_url=None, svc=None):
        started.set()
        release.wait(10)
        finished.set()
        return "id"

    monkeypatch.setattr(gcal, "push", slow_push)
    try:
        assert upload().status_code == 200
        assert started.wait(5)
        assert not finished.is_set()  # the ack went back while Google was still being called
    finally:
        release.set()
        _join_pushers()


def test_calendar_push_failure_or_missing_login_never_fails_the_upload(back_to_back, monkeypatch):
    from pulso.export import gcal, gdrive

    conn, _, upload = back_to_back

    def no_login(alert, doc_url=None, svc=None):
        raise gdrive.NeedsLogin("Google: login necessário")

    monkeypatch.setattr(gcal, "push", no_login)
    r = upload()
    assert r.status_code == 200 and r.json()["ack"] == 1
    _join_pushers()
    keys = [row[0] for row in conn.execute("SELECT key FROM artefacts WHERE kind = 'error:gcal'")]
    assert len(keys) == 1 and keys[0].startswith("sequencia:")
    assert conn.execute("SELECT COUNT(*) FROM artefacts WHERE kind = 'alert'").fetchone()[0] == 1


def test_no_calendar_push_unless_enabled(back_to_back, monkeypatch):
    from pulso.export import gcal

    _, _, upload = back_to_back
    monkeypatch.delenv("PULSO_GCAL_ALERTS")
    pushed: list[dict] = []
    monkeypatch.setattr(gcal, "push", lambda alert, doc_url=None, svc=None: pushed.append(alert) or "id")
    assert upload().status_code == 200
    _join_pushers()
    assert pushed == []
