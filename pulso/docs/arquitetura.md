# Arquitetura

```
                    ┌──────────────────────── Forerunner 255 ────────────────────────┐
                    │                                                                 │
                    │  Mostrador "Pulso"                 App "Pulso Captura"           │
                    │  ├─ tela de saúde/performance      ├─ RR + acelerômetro (25 Hz+) │
                    │  ├─ complicações do sistema        ├─ FC 1 Hz, barômetro bruto   │
                    │  └─ background a cada 5 min:       ├─ RMSSD / DFA α1 ao vivo     │
                    │     SensorHistory + stress 30 s    ├─ governador de bateria      │
                    │     + passos + bateria             ├─ fila no Storage            │
                    │            │                       └─ FIT: SensorLogger + campos │
                    └────────────┼───────────────────────────┼──────────────┼──────────┘
                     makeWebRequest (via celular,     makeWebRequest     BLE 20 B      USB/MTP,
                     ≤3 KB, resposta = estado)        (≤6 KB)            (sessão de    Gadgetbridge
                                 │                       │                mesa)         ou zip diário
                                 ▼                       ▼                 │               │
  Garmin Connect ──(garminconnect,         ┌──────────────────────┐   ┌────┴─────┐         │
  (nuvem)          conta própria)────────▶ │  POST /v1/ingest      │◀──│ pulso    │         │
                                           │  (protocolo v1,       │   │ bridge   │◀─ cinta │
                                           │  idempotente)         │   │ (bless/  │  Polar  │
                                           └──────────┬───────────┘   │  bleak)  │  H10    │
  agenda .ics ─┐                                      ▼               └──────────┘         │
  transcrições ┼─ pulso import ─────────▶  ┌──────────────────────────────────────────┐   │
  git log ─────┘                           │  SQLite (WAL) — fonte de cada número      │◀──┘
                                           │  samples · rr_intervals · motion_chunks   │  pulso import fit
                                           │  events · transcript_segments · sleep     │
                                           └──────────────┬───────────────────────────┘
                                                          ▼
          ┌────────────────────────────── análise (pulso analyze / daemon) ──────────────────────────────┐
          │ limpeza de RR (Lipponen-Tarvainen) → HRV 2 min (RMSSD, espectro, resp. RSA, DFA α1)           │
          │ FC adicional (regressão quantílica: movimento + ritmo circadiano)                              │
          │ fusão Kalman/RTS: FC adicional + estresse Garmin + HRV → ativação (z) ± σ                      │
          │ episódios (histerese) → ligados a reuniões/commits/marcações; change-points                     │
          │ reuniões: resposta por evento · controles pareados (permutação) · efeito por falante/tema        │
          │ sono: noites, nadir de FC, HRV noturna, débito, SRI · prontidão (Plews) · carga de treino        │
          └───────────────┬──────────────────────────────┬───────────────────────────────┬────────────────┘
                          ▼                              ▼                               ▼
                 estado do mostrador (≤1 KB)     Claude: relatório a partir de    MCP: o Claude consulta,
                 volta na resposta do upload     fatos + verificação de números   cruza e marca eventos
                                                 tagger de transcrições           (Desktop / Code)
```

## Decisões

| Decisão | Por quê |
|---|---|
| Relógio fino, central grossa | o relógio tem 64–512 KB e uma VM lenta; a central tem NumPy/SciPy e o Claude |
| Dois apps no relógio | mostrador não lê sensores; só um app em primeiro plano lê RR/acelerômetro |
| Features de movimento no relógio, bruto via FIT/BLE | o link do celular não aguenta 25 Hz bruto |
| SQLite com chaves naturais e `INSERT OR IGNORE` | reimportar e reenviar nunca duplica; um arquivo para backup |
| `source` em toda amostra | todo número responde “como foi medido?”; preferência por fonte na análise |
| Séries derivadas marcadas `derived:v1` | recalculáveis, nunca confundidas com medida |
| Modelos só com histórico anterior | nada de “ver o futuro” no ajuste |
| Fusão por Kalman em vez de média | canais intermitentes e com ruídos diferentes; dá incerteza |
| Controle pareado por horário | separa efeito da reunião do efeito do relógio biológico |
| LLM só com fatos + verificação de números | o modelo escreve; não inventa número |
| FIT cru arquivado para sempre | a Garmin documenta mensagens novas com o tempo; dá para redecodificar |

## Código

| Caminho | O quê |
|---|---|
| `central/pulso/store/` | schema e acesso SQLite |
| `central/pulso/signal/` | RR, HRV, movimento, grades de tempo |
| `central/pulso/models/` | baseline, FC adicional, fusão, episódios, reuniões, sono, treino |
| `central/pulso/analysis.py`, `jobs.py` | pipeline diário e jobs de período |
| `central/pulso/ingest/` | protocolo v1, autenticação, API FastAPI |
| `central/pulso/importers/` | FIT, Garmin Connect, transcrições, .ics, git |
| `central/pulso/ai/` | cliente Claude, fatos + verificação, relatório, tagger |
| `central/pulso/mcp_server.py` | ferramentas MCP |
| `central/pulso/bridge/` | frames BLE, periférico GATT, cinta de FC |
| `watch/face/` | mostrador Pulso (Monkey C) |
| `watch/capture/` | app Pulso Captura (Monkey C) |
| `deploy/` | Docker Compose + Caddy |
