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
    p.add_argument("--db", help="caminho do SQLite (default: $PULSO_DB ou data/pulso.sqlite)")
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
    igc.add_argument("--days", type=int, default=7)

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

            _print(garmin_connect.sync(conn, days=args.days, tz=tz))
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

    if args.cmd == "inventory":
        _print(store.metrics_available(conn))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
