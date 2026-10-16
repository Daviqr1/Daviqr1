"""`pulso` command line. Optional pieces import lazily so the core runs with the base deps."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pulso import config, store


def _date(s: str) -> date:
    return date.fromisoformat(s)


def _range(args, tz: str) -> tuple[date, date]:
    today = datetime.now(ZoneInfo(tz)).date()
    if getattr(args, "date", None):
        return args.date, args.date
    end = args.to or today
    start = args.since or end - timedelta(days=args.days - 1)
    return start, end


def _print(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pulso", description="Central de saúde e performance (Garmin FR255).")
    p.add_argument("--db", help="caminho do SQLite (default: $PULSO_DB ou ~/.pulso/pulso.sqlite)")
    p.add_argument("--tz", help="fuso (default: $PULSO_TZ ou America/Sao_Paulo)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="sobe a API de ingestão (uvicorn)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)

    t = sub.add_parser("token", help="tokens dos aparelhos")
    tsub = t.add_subparsers(dest="tcmd", required=True)
    tc = tsub.add_parser("create")
    tc.add_argument("label")
    tr = tsub.add_parser("revoke")
    tr.add_argument("label")

    imp = sub.add_parser("import", help="importa dados")
    isub = imp.add_subparsers(dest="icmd", required=True)
    it = isub.add_parser("transcript", help="transcrição de reunião (.vtt/.srt/.json/.txt)")
    it.add_argument("files", nargs="+")
    it.add_argument("--start", help="início absoluto, ISO 8601 (senão: agenda ou mtime)")
    it.add_argument("--title")
    ic = isub.add_parser("ics", help="agenda exportada (.ics)")
    ic.add_argument("file")
    ic.add_argument("--since", type=_date)
    ic.add_argument("--to", type=_date)
    ic.add_argument("--me", help="seu e-mail/nome para marcar reuniões que você organiza")
    ig = isub.add_parser("git", help="commits de repositórios locais")
    ig.add_argument("repos", nargs="+")
    ig.add_argument("--author", required=True)
    ig.add_argument("--since", default="90 days ago")
    ifit = isub.add_parser("fit", help="arquivos FIT do relógio (GARMIN/Monitor, Sleep, Activity...)")
    ifit.add_argument("paths", nargs="+")
    igc = isub.add_parser("garmin", help="Garmin Connect (biblioteca não oficial; sua conta)")
    igc.add_argument("--days", type=int, default=7, help="quantos dias para trás (backfill: 180)")
    igc.add_argument("--since", type=_date, help="desde esta data (em vez de --days)")
    igc.add_argument("--pause", type=float, default=2.0, help="segundos entre dias (seja gentil com a Garmin)")
    igc.add_argument("--no-resume", action="store_true", help="rebaixa dias já sincronizados")
    igc.add_argument("--reload-cold", action="store_true", help="pede recarga de dias em armazenamento frio (>6 meses)")
    iex = isub.add_parser("garmin-export", help="export completo da conta Garmin (.zip ou pasta) — anos de histórico")
    iex.add_argument("path")

    for name, help_ in (("analyze", "recalcula séries derivadas, sono e reuniões"),
                        ("report", "relatório com Claude (números verificados)"),
                        ("tag", "classifica trechos de transcrição com Claude")):
        a = sub.add_parser(name, help=help_)
        a.add_argument("--date", type=_date)
        a.add_argument("--since", type=_date)
        a.add_argument("--to", type=_date)
        a.add_argument("--days", type=int, default=1 if name != "tag" else 30)
        if name == "report":
            a.add_argument("--kind", choices=["daily", "weekly"], default="daily")
            a.add_argument("--dry-run", action="store_true", help="mostra os fatos sem chamar a API")

    m = sub.add_parser("mcp", help="servidor MCP para o Claude (stdio ou HTTP)")
    m.add_argument("--http", action="store_true")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8765)

    br = sub.add_parser("bridge", help="ponte BLE no notebook (relógio e cintas)")
    bsub = br.add_subparsers(dest="bcmd", required=True)
    bw = bsub.add_parser("watch", help="periférico GATT para o Pulso Captura (modo Ponte BLE)")
    bh = bsub.add_parser("hrm", help="cinta de FC (Polar H10, HRM-Pro) ou broadcast do relógio")
    bh.add_argument("--name", default="Polar", help="parte do nome do dispositivo BLE")
    bh.add_argument("--seconds", type=float, help="duração (padrão: até Ctrl+C)")
    bh.add_argument("--probe", action="store_true", help="só testa se vem RR (ex.: broadcast do FR255)")
    for b in (bw, bh):
        b.add_argument("--central", help="URL da central (senão grava direto no banco local)")
        b.add_argument("--token", help="token de aparelho para a central")

    nb = sub.add_parser("notebook", help="tudo no notebook: ingestão + painel + jobs (+ ponte BLE, + túnel)")
    nb.add_argument("--port", type=int, default=8080)
    nb.add_argument("--host", default="127.0.0.1")
    nb.add_argument("--ble", action="store_true", help="liga a ponte BLE (relógio direto, pacotes grandes)")
    nb.add_argument("--tunnel", choices=["tailscale", "cloudflared"], help="abre HTTPS público para o celular")
    nb.add_argument("--no-jobs", action="store_true")

    ex = sub.add_parser("export", help="resumo de saúde para o Gemini Notebook/Gemini (Markdown local e/ou Google Docs)")
    ex.add_argument("--days", type=int, default=60, help="janela do documento dia a dia")
    ex.add_argument("--drive", action="store_true", help="publica/atualiza os Google Docs (ver docs/gemini-notebook.md)")
    ex.add_argument("--login", action="store_true", help="login Google interativo (uma vez, no notebook)")
    ex.add_argument("--com-nomes", action="store_true", help="mantém nomes de colegas (padrão: pseudônimos)")

    dm = sub.add_parser("daemon", help="jobs periódicos: reanálise, Garmin Connect, relatórios")
    dm.add_argument("--once", action="store_true", help="uma passada e sai (útil para testar)")

    sub.add_parser("inventory", help="o que existe no banco")

    args = p.parse_args(argv)
    settings = config.get()
    tz = args.tz or settings.tz
    db_path = args.db or settings.db_path
    conn = store.connect(db_path)

    if args.cmd == "serve":
        import uvicorn

        from pulso.ingest.app import create_app

        uvicorn.run(create_app(config.Settings(db_path=db_path, tz=tz), conn=conn), host=args.host, port=args.port,
                    proxy_headers=True)
        return 0

    if args.cmd == "token":
        from pulso.ingest import auth

        if args.tcmd == "create":
            print(auth.create_token(conn, args.label))
            print("guarde este token: só o hash fica no banco", file=sys.stderr)
        else:
            print(f"revogados: {auth.revoke(conn, args.label)}")
        return 0

    if args.cmd == "import":
        if args.icmd == "transcript":
            from pulso.importers import transcripts

            start = datetime.fromisoformat(args.start) if args.start else None
            if start is not None and start.tzinfo is None:
                start = start.replace(tzinfo=ZoneInfo(tz))
            for f in args.files:
                print(transcripts.import_file(conn, f, start=start, title=args.title))
        elif args.icmd == "ics":
            from pulso.importers import calendar_ics

            today = datetime.now(ZoneInfo(tz)).date()
            n = calendar_ics.import_ics(conn, args.file, tz, args.since or today - timedelta(days=90),
                                        args.to or today + timedelta(days=30), me=args.me)
            print(f"{n} eventos")
        elif args.icmd == "git":
            from pulso.importers import gitlog

            for r in args.repos:
                print(r, gitlog.import_repo(conn, r, args.author, args.since))
        elif args.icmd == "fit":
            from pulso.importers import fit

            _print(fit.import_paths(conn, args.paths, tz))
        elif args.icmd == "garmin":
            from pulso.importers import garmin_connect

            rep = garmin_connect.sync(conn, days=args.days, tz=tz, since=args.since, pause_s=args.pause,
                                      resume=not args.no_resume, reload_cold=args.reload_cold)
            print(f"{len(rep)} dias sincronizados")
        elif args.icmd == "garmin-export":
            from pulso.importers import garmin_export

            _print(garmin_export.import_export(conn, args.path, tz))
        return 0

    if args.cmd == "analyze":
        from pulso import jobs

        start, end = _range(args, tz)
        res = jobs.run_range(conn, start, end, tz)
        _print({"days": [{k: d[k] for k in ("day", "minutes_with_hr", "arousal_mean_awake", "notes")} | {"episodes": len(d["episodes"])}
                         for d in res["days"]],
                "nights": len(res["nights"]), "meetings": res["meetings"]["n_meetings"]})
        return 0

    if args.cmd == "report":
        from pulso.ai import report

        start, end = _range(args, tz)
        out = report.generate(conn, start, end, tz, kind=args.kind, dry_run=args.dry_run, settings=settings)
        if isinstance(out, dict) and "markdown" in out:
            print(out["markdown"])
        else:
            _print(out)
        return 0

    if args.cmd == "tag":
        from pulso.ai import tagger

        start, end = _range(args, tz)
        _print(tagger.tag_range(conn, start, end, tz, settings=settings))
        return 0

    if args.cmd == "mcp":
        from pulso import mcp_server

        mcp_server.run(conn, tz, http=args.http, host=args.host, port=args.port, settings=settings)
        return 0

    if args.cmd == "bridge":
        import asyncio

        from pulso.bridge.peripheral import http_sink, local_sink

        sink = http_sink(args.central, args.token or "") if args.central else local_sink(str(db_path))
        if args.bcmd == "watch":
            from pulso.bridge.peripheral import serve

            asyncio.run(serve(sink))
        else:
            from pulso.bridge.hrm import stream

            secs = 120.0 if args.probe and not args.seconds else args.seconds
            on_batch = (lambda b: None) if args.probe else sink
            asyncio.run(stream(args.name, on_batch, device_id=f"hrm-{args.name.lower().replace(' ', '-')}",
                               seconds=secs, src="strap"))
        return 0

    if args.cmd == "notebook":
        from pulso import notebook

        notebook.run(config.Settings(db_path=db_path, tz=tz), host=args.host, port=args.port, bridge=args.ble,
                     tunnel=args.tunnel, jobs=not args.no_jobs)
        return 0

    if args.cmd == "export":
        from pulso.export import summary

        if args.login:
            from pulso.export import gdrive

            gdrive.credentials(interactive=True)
            print("login Google ok (token em ~/.pulso/google-token.json)")
        docs = summary.documents(conn, tz, names=args.com_nomes, days=args.days)
        print(f"resumos: {summary.write_local(docs, tz)}")
        if args.drive:
            from pulso.export import gdrive

            for key, url in gdrive.publish_all(docs).items():
                print(f"{key}: {url}")
        return 0

    if args.cmd == "daemon":
        from pulso import scheduler

        scheduler.loop(config.Settings(db_path=db_path, tz=tz), once=args.once)
        return 0

    if args.cmd == "inventory":
        _print(store.metrics_available(conn))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
