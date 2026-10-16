"""`pulso notebook`: the whole central on your laptop, in one process.

    ingest API + painel  (uvicorn thread)          ← watch via phone, through the HTTPS tunnel
    jobs                 (scheduler thread)        ← reanalysis, Garmin sync, reports, summary export
    BLE bridge           (main thread, optional)   ← watch directly, big payloads, no phone
    tunnel               (child process, optional) ← cloudflared quick tunnel or Tailscale Funnel

The phone path (Garmin Connect Mobile) needs a public HTTPS URL with a real certificate, so a
laptop needs a tunnel; Tailscale Funnel gives a stable *.ts.net URL (set it once in the watch
settings), a cloudflared quick tunnel changes URL every run. Claude Code talks to the same SQLite
through `pulso mcp` (stdio), so there is nothing else to run for it.
"""

from __future__ import annotations

import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable

from pulso import config, scheduler, store


def _start_api(settings: config.Settings, host: str, port: int) -> threading.Thread:
    import uvicorn

    from pulso.ingest.app import create_app

    app = create_app(settings, conn=store.connect(settings.db_path))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", proxy_headers=True))
    t = threading.Thread(target=server.run, name="pulso-api", daemon=True)
    t.start()
    return t


# `tailscale funnel --bg` persists in tailscaled (across reboots) until removed. Removing exactly
# the mount `--bg <port>` created ('/' on 443) leaves any other serve/funnel config untouched.
TAILSCALE_OFF = ["funnel", "--https=443", "--set-path=/", "off"]


def _start_tunnel(kind: str, port: int) -> tuple[Callable[[], None] | None, str | None]:
    """Open the tunnel; return (stop, url). `stop` undoes it and must run on exit."""
    if kind == "cloudflared":
        exe = shutil.which("cloudflared")
        if not exe:
            print("cloudflared não encontrado (https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)")
            return None, None
        p = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{port}"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
        deadline = time.time() + 30
        while time.time() < deadline and p.stdout is not None:
            line = p.stdout.readline()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line or "")
            if m:
                threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()  # drain
                return p.terminate, m.group(0)
        return p.terminate, None
    if kind == "tailscale":
        exe = shutil.which("tailscale")
        if not exe:
            print("tailscale não encontrado (https://tailscale.com/download)")
            return None, None

        def stop() -> None:
            try:
                # stdin may be a hung-up terminal; its own session keeps a terminal Ctrl+C/hangup off it
                # Windows ignores start_new_session; a new process group there has Ctrl+C disabled
                kw = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32"
                      else {"start_new_session": True})
                r = subprocess.run([exe, *TAILSCALE_OFF], stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=30, **kw)
                err = r.stderr.strip() if r.returncode != 0 else None
            except BaseException as e:  # whatever cut it short, say how to finish it by hand
                err = repr(e)
            if err is not None:
                print(f"não consegui desligar o Funnel: rode `tailscale {' '.join(TAILSCALE_OFF)}`\n{err}", file=sys.stderr)

        if subprocess.run([exe, "funnel", "--bg", str(port)], check=False).returncode != 0:
            return None, None  # nothing was set up, so nothing to undo (and never remove a config we did not make)
        try:
            st = subprocess.run([exe, "funnel", "status"], capture_output=True, text=True).stdout
        except BaseException:  # Ctrl+C/kill before the caller holds `stop`: the Funnel is up, undo it here
            _shielded(stop)
            raise
        m = re.search(r"https://[^\s]+\.ts\.net", st)
        return stop, m.group(0) if m else None
    return None, None


def _sigterm(signum, frame) -> None:
    # `kill`/systemd stop, or SIGHUP when the terminal closes or SSH drops: run the same cleanup as
    # Ctrl+C (close the tunnel) instead of dying with the `--bg` Funnel still public
    raise KeyboardInterrupt


def _shielded(cleanup: Callable[[], None]) -> None:
    """Run `cleanup` with Ctrl+C, SIGTERM and SIGHUP ignored (main thread only): a second Ctrl+C or kill
    must not cut short the command that closes the Funnel and leave it public. stop()'s 30 s timeout
    bounds the wait; the handlers go back as they were."""
    if threading.current_thread() is not threading.main_thread():
        cleanup()
        return
    sigs = [signal.SIGINT, signal.SIGTERM, *([signal.SIGHUP] if hasattr(signal, "SIGHUP") else [])]
    held = {s: signal.signal(s, signal.SIG_IGN) for s in sigs}
    try:
        cleanup()
    finally:
        for s, h in held.items():
            if h is not None:
                signal.signal(s, h)


def _exit_signals() -> list[signal.Signals]:
    sigs = [signal.SIGTERM]
    # only a SIGHUP left at its default (death): `nohup` sets SIG_IGN and must keep it
    if hasattr(signal, "SIGHUP") and signal.getsignal(signal.SIGHUP) == signal.SIG_DFL:
        sigs.append(signal.SIGHUP)
    return sigs


def run(settings: config.Settings, host: str = "127.0.0.1", port: int = 8080, bridge: bool = False,
        tunnel: str | None = None, jobs: bool = True) -> None:
    store.connect(settings.db_path).close()  # create/migrate before the threads race for it
    config.ensure_home()  # owner-only before the jobs write the FIT archive, summaries, Google login
    _start_api(settings, host, port)
    if jobs:
        threading.Thread(target=scheduler.loop, args=(settings,), name="pulso-jobs", daemon=True).start()
    main = threading.current_thread() is threading.main_thread()
    prev = {s: signal.signal(s, _sigterm) for s in _exit_signals()} if main else {}
    stop_tunnel: Callable[[], None] | None = None
    try:
        stop_tunnel, url = _start_tunnel(tunnel, port) if tunnel else (None, None)
        _serve(settings, port, tunnel, url, stop_tunnel is not None, bridge)
    except KeyboardInterrupt:
        print("encerrando", file=sys.stderr)
    finally:
        if stop_tunnel is not None:
            _shielded(stop_tunnel)
        for s, h in prev.items():
            signal.signal(s, h)


def _serve(settings: config.Settings, port: int, tunnel: str | None, url: str | None, tunnel_up: bool,
           bridge: bool) -> None:
    print(f"""
Pulso rodando no notebook
  banco        {settings.db_path}
  painel       http://localhost:{port}/painel
  ingestão     http://localhost:{port}/v1/ingest
  túnel        {url or ('—' if not tunnel else 'não detectado (veja a saída do túnel)')}
  Claude Code  claude mcp add pulso -- pulso mcp      (ou use o .mcp.json do projeto)
  relógio      URL da central = {url or 'https://<seu túnel>'} ; token = pulso token create fr255
  painel web   token só de leitura = pulso token create painel --painel
""", flush=True)
    if tunnel == "tailscale" and tunnel_up:
        print(f"  o Funnel é desligado ao sair; se o processo morrer: tailscale {' '.join(TAILSCALE_OFF)}\n", flush=True)

    if bridge:
        import asyncio

        from pulso.bridge.peripheral import local_sink, serve

        asyncio.run(serve(local_sink(str(settings.db_path))))
    else:
        while True:
            time.sleep(3600)
