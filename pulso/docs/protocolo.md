# Protocolo relógio ↔ central (v1)

Fonte de verdade: `central/pulso/ingest/protocol.py` (validação) e `central/pulso/ingest/handler.py`
(semântica). Este documento é o contrato para quem escreve o lado Monkey C.

## Transporte

| Caminho | Quem usa | Banda real | Observação |
|---|---|---|---|
| `Communications.makeWebRequest` → Garmin Connect Mobile → HTTPS | mostrador (background) e app de captura | 400–800 B/s | HTTPS com certificado público; JSON; **1 requisição por vez** |
| BLE direto (app de captura → ponte no notebook/ESP32) | app de captura | ver `docs/ponte-ble.md` | só com o app aberto |
| Arquivo FIT (USB/MTP ou export do Garmin Connect) | app de captura (`SensorLogger` + `FitContributor`) | — | acelerômetro bruto em taxa cheia |

Regras de tamanho (medidas da comunidade; ninguém da Garmin documentou):

- corpo do POST ≤ **6 KB** (o app de captura); ≤ **3 KB** no serviço de background;
- resposta ≤ **1 KB** (o background recebe `-403` com ~6.7 KB);
- uma requisição em voo; em erro `-104` (sem celular), `-2`/`-300` (timeout), `-402`/`-403` (memória) → fica na fila do `Application.Storage` e tenta de novo com backoff.

## Requisição

```
POST /v1/ingest
Authorization: Bearer pulso_dev_xxxxxxxx        (configurado nas settings do app via Garmin Connect)
Content-Type: application/json
```

```jsonc
{
  "v": 1,                        // versão
  "d": "fr255-a1b2",             // id do aparelho, gerado no 1º boot e salvo no Storage
  "q": 1842,                     // sequência por aparelho: (d, q) é a chave de idempotência
  "src": "face",                 // "face" | "cap" | "ble"
  "sid": "c-1760630400",         // sessão de captura (cap/ble)
  "t": 1760630400,               // Time.now().value() no envio (para estimar o relógio)
  "h": {                         // pontos do SensorHistory desde o último cursor
    "hr": [[t_s, bpm], ...], "stress": [[t_s, 0..100], ...], "bb": [...],
    "spo2": [...], "temp": [...], "press": [...], "elev": [...]
  },
  "m": {"t": t_s, "stress": 31, "resp": 14.5, "steps": 5120, "rhr": 52},  // ActivityMonitor agora
  "rr":  [[t_ultimo_batimento_ms, [812, 820, ...]], ...],   // até 600 RR por bloco
  "hr1": [t0_s, [bpm, bpm, null, ...]],                      // FC 1 Hz (captura)
  "acc": [[t0_ms, t0_s, [enmo_mg...], [mad_mg...]], ...],    // features de 1 s calculadas no relógio
  "raw": [[t0_ms, hz, "b64x", "b64y", "b64z"], ...],         // int16 LE em base64 — só ponte BLE
  "bar": [t0_s, passo_s, [pa, ...]],                         // Activity.Info.rawAmbientPressure
  "ev":  [[t_s, "mark", "estressado"], ...],                 // marcações por botão
  "bat": [t_s, pct, dias]                                    // System.getSystemStats()
}
```

Notas para o Monkey C:

- **Tempo dos RR**: no callback do `registerSensorDataListener`, ancore o *último* intervalo em
  `epochMs = âncoraEpochMs + (System.getTimer() − âncoraTimer)` (refaça a âncora a cada minuto; o
  `getTimer()` dá a volta depois de ~25 dias). O servidor reconstrói os outros batimentos para trás.
- **Features de movimento**: ENMO = média de `max(√(x²+y²+z²) − 1000, 0)` em mG por janela de 1 s;
  MAD = média de `|vm − média(vm)|`. Isso cabe no link; o bruto a 25 Hz (~13 MB/dia) não cabe.
- Campos ausentes podem ser omitidos. `null` dentro de arrays = amostra inválida.

## Resposta

```jsonc
{"ok": 1, "ack": 1842, "dup": 0, "face": { ... }}   // "face" só quando src = "face"
```

### `face` (≤ 1 KB, já formatado)

| chave | tipo | significado |
|---|---|---|
| `t` | int | epoch s do cálculo |
| `az` | float | ativação (estresse fundido) agora, em z (desvios do seu normal) |
| `ahr` | int | FC adicional agora (bpm acima do que o movimento explica) |
| `tr` | int[12] | ativação média por hora nas últimas 12 h, −9..9 (z×3), `null` sem dado |
| `ep` | int | episódios de estresse hoje |
| `pk` / `pl` | str | hora do último pico / evento ligado a ele (≤ 18 chars) |
| `slp` / `sd` | int | sono da última noite (min) / débito de sono (min) |
| `hrv` / `hrvs` | int / str | HRV noturna (ms) / `below` `normal` `above` `insufficient` |
| `nm` | obj | próxima reunião `{t: "15:00", ti: "Planning", r: 0..3}` (r = carga prevista) |
| `msg` | str | insight curto do dia (≤ 48 chars) |
| `al` | obj | alerta novo `{i: id numérico estável, m: texto ≤ 60}` — mostrar uma vez como notificação nativa (`Notifications.showNotification`, API 5.1.0) e lembrar o último `i` mostrado |

`GET /v1/face` devolve o mesmo objeto sem enviar dados.
