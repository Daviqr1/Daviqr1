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
import subprocess
import sys
import threading
import time

from pulso import config, scheduler, store


def _start_api(settings: config.Settings, host: str, port: int) -> threading.Thread:
    import uvicorn

    from pulso.ingest.app import create_app

    app = create_app(settings, conn=store.connect(settings.db_path))
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", proxy_headers=True))
    t = threading.Thread(target=server.run, name="pulso-api", daemon=True)
    t.start()
    return t


def _start_tunnel(kind: str, port: int) -> tuple[subprocess.Popen | None, str | None]:
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
                return p, m.group(0)
        return p, None
    if kind == "tailscale":
        exe = shutil.which("tailscale")
        if not exe:
            print("tailscale não encontrado (https://tailscale.com/download)")
            return None, None
        subprocess.run([exe, "funnel", "--bg", str(port)], check=False)
        st = subprocess.run([exe, "funnel", "status"], capture_output=True, text=True).stdout
        m = re.search(r"https://[^\s]+\.ts\.net", st)
        return None, m.group(0) if m else None
    return None, None


def run(settings: config.Settings, host: str = "127.0.0.1", port: int = 8080, bridge: bool = False,
        tunnel: str | None = None, jobs: bool = True) -> None:
    store.connect(settings.db_path).close()  # create/migrate before the threads race for it
    _start_api(settings, host, port)
    if jobs:
        threading.Thread(target=scheduler.loop, args=(settings,), name="pulso-jobs", daemon=True).start()
    proc, url = _start_tunnel(tunnel, port) if tunnel else (None, None)

    print(f"""
Pulso rodando no notebook
  banco        {settings.db_path}
  painel       http://localhost:{port}/painel
  ingestão     http://localhost:{port}/v1/ingest
  túnel        {url or ('—' if not tunnel else 'não detectado (veja a saída do túnel)')}
  Claude Code  claude mcp add pulso -- pulso mcp      (ou use o .mcp.json do projeto)
  relógio      URL da central = {url or 'https://<seu túnel>'} ; token = pulso token create fr255
""", flush=True)

    try:
        if bridge:
            import asyncio

            from pulso.bridge.peripheral import local_sink, serve

            asyncio.run(serve(local_sink(str(settings.db_path))))
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        print("encerrando", file=sys.stderr)
    finally:
        if proc is not None:
            proc.terminate()
