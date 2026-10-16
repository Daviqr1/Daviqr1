"""MCP server: lets Claude (Desktop, Code, or claude.ai via a remote connector) query the central.

Read tools return already-computed, unit-labelled numbers; Claude does the reasoning on top.
One write tool (`marcar`) records a manual note ("café", "discussão com X") as an event.

    pulso mcp                 # stdio — Claude Desktop / Claude Code
    pulso mcp --http          # streamable HTTP on 127.0.0.1:8765/mcp, Bearer $PULSO_MCP_TOKEN
"""

from __future__ import annotations

import math
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from pulso import analysis, config, jobs, store
from pulso import metrics as M
from pulso.ai import facts as F
from pulso.importers.transcripts import segments_for

INSTRUCTIONS = """Pulso é a central de saúde e performance de um engenheiro de software (Garmin Forerunner 255).
Unidades: ativação (arousal) em z — desvios em relação ao normal da própria pessoa naquele horário (1 z perceptível,
2 z forte); FC adicional em bpm acima do que o movimento explica; HRV em ms (RMSSD). Datas no formato AAAA-MM-DD,
horários no fuso local. Comece por `inventario` e `tabela_diaria`; use `reunioes` e `reuniao` para entender a origem
do estresse e `correlacionar` para cruzar dados (com defasagem em dias). Correlação não é causa: cite n, rho e p."""


def _d(s: str | None, default: date) -> date:
    return date.fromisoformat(s) if s else default


def _clean(x: Any) -> Any:
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    if isinstance(x, dict):
        return {k: _clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_clean(v) for v in x]
    if isinstance(x, np.generic):
        return _clean(x.item())
    return x


def build(conn: sqlite3.Connection, tz: str):
    from mcp.server.mcpserver import MCPServer

    lock = threading.Lock()
    server = MCPServer(name="pulso", instructions=INSTRUCTIONS, version="0.1.0")

    def today() -> date:
        return datetime.now(ZoneInfo(tz)).date()

    @server.tool()
    def inventario() -> dict:
        """Quais métricas existem no banco, de que fontes, quantas amostras e de quando a quando (datas locais)."""
        with lock:
            rows = store.metrics_available(conn)
        for r in rows:
            for k in ("first_ms", "last_ms"):
                r[k.replace("_ms", "")] = datetime.fromtimestamp(r.pop(k) / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).isoformat(timespec="minutes")
        return {"metricas": rows}

    @server.tool()
    def serie(metrica: str, inicio: str, fim: str, resolucao_min: int = 5) -> dict:
        """Série temporal de uma métrica (ex.: hr, stress_garmin, body_battery, arousal_z, additional_hr, rmssd,
        dfa_a1, resp_rsa, enmo) entre duas datas AAAA-MM-DD (inclusive), em médias por janela de `resolucao_min` minutos.
        Máximo de 2000 pontos."""
        a, _ = analysis.local_day_bounds(date.fromisoformat(inicio), tz)
        _, b = analysis.local_day_bounds(date.fromisoformat(fim), tz)
        step = max(1, int(resolucao_min)) * 60_000
        if (b - a) // step > 2000:
            step = int(math.ceil((b - a) / 2000 / 60_000)) * 60_000
        with lock:
            ts, v = store.series(conn, metrica, a, b)
        if ts.size == 0:
            return {"metrica": metrica, "pontos": [], "nota": "sem dados"}
        idx = (ts - a) // step
        out = []
        for k in np.unique(idx):
            m = idx == k
            out.append([datetime.fromtimestamp((a + int(k) * step) / 1000, tz=timezone.utc).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M"),
                        round(float(np.mean(v[m])), 2)])
        return {"metrica": metrica, "resolucao_min": step // 60_000, "pontos": out}

    @server.tool()
    def dia(data: str | None = None, recalcular: bool = False) -> dict:
        """Resumo de um dia: ativação média, minutos acima de 1,5 z, episódios de estresse (com horário, tipo
        mental/físico/misto e eventos coincidentes), canais usados na fusão."""
        d = _d(data, today())
        with lock:
            s = store.get_artefact(conn, "analysis:day", d.isoformat())
            if s is None or recalcular:
                s = analysis.analyze_day(conn, d, tz).summary()
        for ep in s.get("episodes", []):
            for k in ("start_ms", "end_ms", "peak_ms"):
                ep[k.replace("_ms", "")] = F.hhmm(ep[k], tz)
        return _clean(s)

    @server.tool()
    def tabela_diaria(inicio: str | None = None, fim: str | None = None) -> dict:
        """Uma linha por dia com tudo que dá para cruzar: ativação, episódios, sono da noite anterior, débito,
        HRV noturna, reuniões (quantidade e minutos), commits (e quantos entre 22h e 5h), marcações manuais."""
        end = _d(fim, today())
        start = _d(inicio, end - timedelta(days=13))
        with lock:
            rows = jobs.daily_table(conn, start, end, tz)
        return {"colunas": list(jobs.DAILY_COLUMNS), "linhas": _clean(rows)}

    @server.tool()
    def correlacionar(coluna_a: str, coluna_b: str, inicio: str | None = None, fim: str | None = None,
                      defasagem_dias: int = 0) -> dict:
        """Correlação de Spearman entre duas colunas da tabela diária, com p-valor por permutação.
        defasagem_dias = 1 compara o dia t de A com o dia t+1 de B (ex.: late_commits → sleep_h do dia seguinte)."""
        end = _d(fim, today())
        start = _d(inicio, end - timedelta(days=59))
        if coluna_a not in jobs.DAILY_COLUMNS or coluna_b not in jobs.DAILY_COLUMNS:
            return {"erro": f"colunas válidas: {', '.join(jobs.DAILY_COLUMNS)}"}
        with lock:
            rows = jobs.daily_table(conn, start, end, tz)
        return jobs.correlate_daily(rows, coluna_a, coluna_b, lag_days=max(0, int(defasagem_dias)))

    @server.tool()
    def reunioes(inicio: str | None = None, fim: str | None = None, recalcular: bool = False) -> dict:
        """Impacto das reuniões: comparação com os mesmos horários sem reunião (diferença em z, IC95, p),
        efeito por falante e por tema (trechos etiquetados), piores reuniões e recuperações mais lentas."""
        end = _d(fim, today())
        start = _d(inicio, end - timedelta(days=29))
        key = f"{start.isoformat()}..{end.isoformat()}"
        with lock:
            s = store.get_artefact(conn, "meetings:summary", key)
            if s is None or recalcular:
                s = jobs.meeting_job(conn, start, end, tz)
        return _clean(s)

    @server.tool()
    def reuniao(event_id: str) -> dict:
        """Uma reunião em detalhe: resposta fisiológica (antecipação, durante, pico, recuperação) e cada trecho da
        transcrição com falante, etiquetas e a ativação média ~20 s depois dele."""
        with lock:
            ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if ev is None:
                return {"erro": "evento não encontrado"}
            resp = store.get_artefact(conn, "meeting_response", event_id)
            segs = segments_for(conn, event_id)
            ts, z = store.series(conn, M.AROUSAL_Z, ev["start_ms"] - 3_600_000, ev["end_ms"] + 3_600_000, source=M.DERIVED)
        out = []
        for s in segs[:400]:
            m = (ts >= s["start_ms"] + 20_000) & (ts < max(s["end_ms"], s["start_ms"] + 60_000) + 20_000)
            out.append({"hora": F.hhmm(s["start_ms"], tz), "falante": s["speaker"], "texto": s["text"][:300],
                        "etiquetas": s["tags"], "ativacao_z": round(float(np.nanmean(z[m])), 2) if np.any(m) else None})
        import json

        return _clean({"titulo": ev["title"], "inicio": F.hhmm(ev["start_ms"], tz), "fim": F.hhmm(ev["end_ms"], tz),
                       "resposta": resp, "ia": json.loads(ev["attrs"]).get("ai"), "trechos": out,
                       "truncado": len(segs) > 400})

    @server.tool()
    def sono(inicio: str | None = None, fim: str | None = None) -> dict:
        """Noites no período: duração, eficiência, estágios, despertares, FC mínima e quando ela ocorre, HRV noturna,
        débito acumulado e status da HRV em relação ao seu normal (método de Plews)."""
        end = _d(fim, today())
        start = _d(inicio, end - timedelta(days=13))
        out = []
        d = start
        with lock:
            while d <= end:
                n = store.get_artefact(conn, "sleep:night", d.isoformat())
                if n:
                    n = dict(n)
                    n["inicio"], n["fim"] = F.hhmm(n.pop("onset_ms"), tz), F.hhmm(n.pop("offset_ms"), tz)
                    out.append(n)
                d += timedelta(days=1)
            reg = store.get_artefact(conn, "sleep:regularity", f"{start.isoformat()}..{end.isoformat()}")
        return _clean({"noites": out, "regularidade": reg})

    @server.tool()
    def fatos(inicio: str | None = None, fim: str | None = None) -> dict:
        """Os fatos numerados e verificados que o relatório usa (mesma base do `pulso report`)."""
        end = _d(fim, today())
        start = _d(inicio, end)
        with lock:
            book = F.build(conn, start, end, tz)
        return {"fatos": [{"id": f.id, "tema": f.topic, "texto": f.text} for f in book.facts]}

    @server.tool()
    def reanalisar(inicio: str | None = None, fim: str | None = None) -> dict:
        """Recalcula séries derivadas, sono e reuniões no período (pode levar alguns segundos por dia)."""
        end = _d(fim, today())
        start = _d(inicio, end)
        with lock:
            res = jobs.run_range(conn, start, end, tz)
        return {"dias": len(res["days"]), "noites": len(res["nights"]), "reunioes": res["meetings"]["n_meetings"]}

    @server.tool()
    def marcar(rotulo: str, quando: str | None = None) -> dict:
        """Registra uma marcação manual (ex.: "café", "discussão com o cliente", "dor de cabeça") no horário
        `quando` (AAAA-MM-DD HH:MM, fuso local; padrão: agora). Entra nas análises como evento."""
        if quando:
            t = datetime.strptime(quando, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo(tz))
        else:
            t = datetime.now(ZoneInfo(tz))
        ms = int(t.timestamp() * 1000)
        with lock:
            store.upsert_event(conn, store.Event(f"mark:mcp:{ms}", "note", ms, ms, rotulo[:120], "mcp", {"kind": "mark"}))
        return {"ok": True, "id": f"mark:mcp:{ms}", "quando": t.isoformat(timespec="minutes")}

    return server


def run(conn: sqlite3.Connection, tz: str, http: bool = False, host: str = "127.0.0.1", port: int = 8765,
        settings: config.Settings | None = None) -> None:
    settings = settings or config.get()
    server = build(conn, tz)
    if not http:
        server.run("stdio")
        return
    if not settings.mcp_token:
        raise SystemExit("defina PULSO_MCP_TOKEN para expor o MCP por HTTP")
    import os

    import uvicorn
    from mcp.server.transport_security import TransportSecuritySettings

    # MCP 2.x protects against DNS rebinding: behind a public hostname (Caddy, tunnel) the Host
    # header must be allow-listed, e.g. PULSO_MCP_ALLOWED_HOSTS="pulso.seudominio.dev"
    extra = [h.strip() for h in os.environ.get("PULSO_MCP_ALLOWED_HOSTS", os.environ.get("PULSO_DOMAIN", "")).split(",") if h.strip()]
    security = None
    if extra:
        hosts = extra + [f"{h}:*" for h in extra] + ["127.0.0.1:*", "localhost:*"]
        security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=hosts,
                                             allowed_origins=[f"https://{h}" for h in extra])
    app = server.streamable_http_app(host=host, transport_security=security)
    uvicorn.run(BearerGate(app, settings.mcp_token), host=host, port=port)


class BearerGate:
    """Minimal ASGI guard: every HTTP request must carry `Authorization: Bearer <PULSO_MCP_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            import hmac

            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)
