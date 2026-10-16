# Pulso — mostrador de saúde e performance (Forerunner 255)

Substitui a antiga tela de "limite do Claude". É um **watch face** Connect IQ que mostra, num só
olhar, o que a central do Pulso calculou sobre o seu estresse, e envia a cada 5 minutos os dados
de histórico do relógio para a central (`POST /v1/ingest`, protocolo v1 em `docs/protocolo.md`).

O relógio fica "magro": coleta, um pouco de cálculo leve, transporte e exibição. A análise pesada
(ativação fundida, HRV noturna, episódios, cruzamento com reuniões, insight do dia) roda na central.

## O que aparece onde

Tela redonda 260×260, 64 cores (MIP). Mock aproximado:

```
                  .  ~  ~  ~  ~  ~  .             anel externo: 12 segmentos coloridos,
             .  '      SEX 16 OUT      '  .        um por hora, na posição do relógio
          .'               14:37 52           '.   (traço branco = agora)
        .'      ATIVAÇÃO +1,4z   +12bpm         '.
       :        ░▒▓█ calmo ─────▲──── tenso       :   barra com dithering + marcador
       :     ♥ 58    ▮ 72    ≋ 14    ⧗ 18h        :   FC · Body Battery · resp. · recuperação
       :     ∿ 48    ☾ 7h12  ☾- 40m  ⚡ 31         :   HRV · sono · débito · estresse Garmin
        '.      ▦ 15:00 1:1 com Ana ● ● ○       .'    próxima reunião + carga prevista
          '.   ▲ 2 picos · 14:05 Planning Q4  .'      picos de hoje
             '.  Picos seguem reuniões     .'          insight da central (2 linhas)
                '. longas; pausa de 5 …  .'
                    '.   3m · ok    .'                 idade do dado · status do envio
```

| Região | Conteúdo | Fonte do dado |
|---|---|---|
| Anel externo | 12 segmentos, cada um na posição do relógio da hora que representa (o traço branco marca "agora"; o arco vazio entre o segmento mais novo e o traço é a idade do dado, e nada com mais de 12 h é desenhado). Cor: azul = mais calmo que o seu normal → verde = normal → amarelo/laranja/vermelho = tenso. Cinza fino = sem dado | `face.tr` da central (ativação −9..9 por hora). Sem central: estresse Garmin por hora (`SensorHistory.getStressHistory`, completado por um acumulador local do `stressScore`, porque no FR255 o histórico de estresse às vezes volta vazio) |
| Topo | dia da semana, dia, mês | relógio |
| Centro | hora (24 h ou 12 h conforme o relógio) + segundos pequenos | relógio |
| "agora" | **ATIVAÇÃO** em z (desvios do seu normal, 1 casa) + FC adicional (bpm acima do que o movimento explica) quando o estado da central tem menos de 15 min. Senão **ESTRESSE** Garmin 0–100 (`ActivityMonitor.stressScore`, média móvel de 30 s) | `face.az`, `face.ahr` / relógio |
| Barra | gradiente calmo→tenso com *dithering* Bayer 4×4 (só existem 64 cores) e marcador do valor atual (z de −3 a +3, ou estresse 0–100) | — |
| Linha 1 | ♥ FC · ▮ Body Battery · ≋ respiração (rpm) · ⧗ tempo de recuperação | complicações do sistema; fallback `ActivityMonitor` / `SensorHistory` |
| Linha 2 | ∿ HRV noturna (ms; cor = `below` laranja, `normal` verde, `above` azul) · ☾ sono da última noite · ☾- débito de sono · ⚡ estresse Garmin (ou ▲ nº de episódios quando a ativação não está disponível) | `face.hrv/hrvs/slp/sd/ep` |
| Reunião | próxima reunião "15:00 Título" + 0–3 pontos de carga prevista (verde→vermelho); horário fica laranja a ≤ 15 min. Sem central: horário do próximo evento do calendário | `face.nm` / complicação `CALENDAR_EVENTS` |
| Picos | episódios de estresse de hoje, hora do último pico e o evento ligado a ele | `face.ep/pk/pl` |
| Insight | frase curta do dia (até 2 linhas, com reticências) | `face.msg` |
| Rodapé | idade do estado da central (verde < 15 min, laranja < 1 h, vermelho) · status do último envio: `ok`, `sem cel.`, `sem resp.` (tempo esgotado), `token?` (401/403), `config?`, `pausado`, `erro N`; `+N` = lotes na fila | `Storage` |

Valores ausentes aparecem como `--`. Dados da central que não são "agora" (sono, HRV, insight)
somem depois de 36 h sem atualização; picos só aparecem se forem do dia de hoje.

## Configurações

| Chave | Padrão | Uso |
|---|---|---|
| `centralUrl` | `https://pulso.example.com` | URL base da central (o mostrador acrescenta `/v1/ingest`). Precisa ser **HTTPS com certificado público**. O valor de exemplo conta como "não configurado" |
| `deviceToken` | vazio | token `pulso_dev_…` gerado pela central; vai como `Authorization: Bearer` |
| `uploadEnabled` | ligado | desliga o serviço de background |
| `showSeconds` | ligado | segundos (atualização parcial de 1 Hz quando a tela está em baixo consumo) |
| `ringSource` | Automático | anel: central, senão Garmin / só central / só Garmin |
| `batterySaver` | ligado | governador: envio a cada 5 min; 10 min com bateria < 30 %; 20 min < 15 %; sem segundos < 15 % (nada disso quando está carregando) |
| `deviceId` | (somente leitura) | id aleatório do relógio (`fr255-xxxxxxxx`) usado nos lotes, para conferir na central |

**Atenção:** o app Garmin Connect só edita configurações de apps instalados pela **Connect IQ Store**
(pode ser um app beta privado). Num mostrador copiado por USB, embuta URL e token na compilação:

```bash
python3 tools/bake_config.py --url https://minha-central.exemplo.dev --token pulso_dev_...
# gera config/local/properties.xml e local.jungle (ambos no .gitignore)
```

O token fica legível dentro do `.prg`: trate o arquivo como segredo e revogue o token na central
se perder o relógio ou o arquivo.

## Compilar e instalar (sideload)

Pré-requisitos: Connect IQ SDK ≥ 4.2 (testado com 9.2.0) e os arquivos do dispositivo `fr255`
(SDK Manager), uma chave de desenvolvedor (`openssl genrsa -out key.pem 4096 && openssl pkcs8
-topk8 -inform PEM -outform DER -in key.pem -out developer_key.der -nocrypt`).

```bash
cd pulso/watch/face
# build normal (configurações pelo app Garmin Connect, quando instalado pela loja)
monkeyc -d fr255 -f monkey.jungle -o bin/pulso.prg -y developer_key.der -l 2 -r
# build para sideload com URL/token embutidos
monkeyc -d fr255 -f "monkey.jungle;local.jungle" -o bin/pulso.prg -y developer_key.der -l 2 -r
# simulador
connectiq &  monkeydo bin/pulso.prg fr255
```

Instalação por USB: o FR255 aparece como dispositivo **MTP** (Linux: `gio mount` / `jmtpfs`;
macOS: OpenMTP). Copie `pulso.prg` para `GARMIN/APPS/`, desconecte, e escolha **Pulso** em
*Mostrador do relógio*. Para atualizar, substitua o arquivo (o `Storage` — fila, cursores, id — é
mantido).

Os recursos binários são gerados por `tools/gen_assets.py` (ícone 40×40 sem dependências; fontes
BMFont com Pillow a partir de Big Shoulders e Carlito, licença SIL OFL, e um conjunto de ícones
1-bit desenhados em pixel). Para o FR255S (218×218): `--scale 0.8385 --out
resources-round-218x218` e acrescente `fr255s`/`fr255sm` ao manifesto.

## Como funciona

**Foreground (o mostrador)**, `source/PulsoView.mc`, `Render.mc`, `Model.mc`

- Tudo menos os segundos é desenhado num `BufferedBitmap` de tela cheia (pool gráfico, fora dos
  128 KB do app) quando muda o minuto ou chega dado novo; nas atualizações de 1 s com a tela acordada
  é só um *blit*. Os segundos são desenhados por `onPartialUpdate` num retângulo de ~18×17 px com
  `setClip`; se o sistema chamar `onPowerBudgetExceeded`, as atualizações parciais são desligadas.
- Assina as complicações `BODY_BATTERY`, `HEART_RATE`, `RESPIRATION_RATE`, `RECOVERY_TIME`,
  `STRESS` e `CALENDAR_EVENTS` (com `try/catch`; cada valor tem fallback).
- Cada minuto soma o `stressScore` num acumulador horário (persistido) para o anel não ficar vazio
  quando o histórico de estresse do FR255 falha.

**Background (`source/PulsoService.mc`, evento temporal a cada 5 min)**

1. Lê os cursores (`cur`: último epoch confirmado pela central por série) e colhe do
   `SensorHistory` só amostras novas, no máximo dos últimos 60 min, em ordem cronológica:
   `hr` (≤ 1/min), `stress` e `bb` (≤ 1/3 min), `spo2`, `temp`, `press`, `elev` (≤ 1/5 min).
   O lote é limitado a 140 pontos (~2,6 KB de JSON); o excedente mais novo vai no próximo envio.
2. Acrescenta `m` (stress, resp, passos, FC de repouso) e `bat` [t, %, dias].
3. `POST <central>/v1/ingest` com `v=1, d, q, src="face"`. `d` é aleatório e fixo; `q` é
   incrementado e salvo **antes** do envio, então `(d, q)` nunca se repete e a central descarta
   reenvios duplicados.
4. HTTP 200: avança os cursores, guarda o objeto `face` da resposta e, se sobrar tempo
   (< 15 s, até 3 requisições), envia lotes antigos da fila.
5. Falha transitória (sem celular, timeout, 5xx, 401…): o lote vai para uma fila de até 3 lotes no
   `Storage` (o mais antigo é descartado) e será reenviado com o **mesmo** `q`. Os cursores não
   andam, então o próximo lote relê a última hora do histórico; pontos repetidos entre lotes da fila
   são removidos. Erros 400/413/415/422 descartam o lote (não adianta repetir os mesmos bytes).
6. `Background.exit({"ok", "t", "q", "n", "face"})` → `onBackgroundData` grava o estado e redesenha.

Tamanho (build `-r`, `--build-stats 0`): foreground 18 KB de código + 4,3 KB de dados; background
5 KB + 1,6 KB; `.prg` total 39 KB com fontes. Limites do FR255: 128 KB (mostrador) e 64 KB
(background).

## Limites honestos

- **Mostrador não lê sensores.** Watch faces não podem usar `Sensor`, `Communications` (fora do
  background), `Position`, gravação de atividade nem `SensorLogging`. Aqui só entra o que o próprio
  firmware já resumiu: histórico (`SensorHistory`), `ActivityMonitor`, complicações. Batimento a
  batimento (RR), acelerômetro bruto e FC a 1 Hz exigem o **app de captura** (`watch/capture`),
  que roda em primeiro plano.
- **5 minutos é o mínimo** do evento temporal, e o serviço só roda enquanto o Pulso é o mostrador
  ativo. Cada execução tem 30 s e 64 KB.
- **Depende do celular.** O envio passa pelo Garmin Connect Mobile via Bluetooth (400–800 B/s).
  Sem celular, os dados ficam no histórico do relógio; ao voltar a conexão a última hora é reenviada
  e a fila guarda até 3 lotes. Uma queda de várias horas perde o detalhe intermediário do histórico
  (a central pode completar com a exportação do Garmin Connect / arquivos FIT).
- **Resolução do histórico é a do firmware** (FC a cada ~1–2 min, estresse/Body Battery a cada
  ~3 min). Não há HRV contínua no mostrador: `hrv` é a noturna calculada na central.
- A central decide o que é "normal" para você; nos primeiros dias, sem linha de base, `az` e `tr`
  podem faltar e o mostrador cai para os números da Garmin.
- O FR255 tem 64 cores: gradientes são simulados por *dithering* e todas as cores do código são
  entradas exatas da paleta (níveis 00/55/AA/FF por canal).

## Arquivos

```
manifest.xml              watchface, minApiLevel 4.2.0, fr255 + fr255m, permissões
monkey.jungle             caminho de recursos (config/default ou config/local)
config/default/           properties.xml: padrões das configurações
resources/                strings pt-BR, settings, fontes BMFont, ícone
source/PulsoApp.mc        AppBase (:background), agendamento + governador de bateria
source/PulsoService.mc    ServiceDelegate: coleta, fila, envio (:background)
source/Shared.mc          chaves de Storage, configurações, id/sequência (:background)
source/Model.mc           estado exibido: central, complicações, anel de estresse
source/Render.mc          desenho (paleta, anel, barra com dithering, grade, textos)
source/PulsoView.mc       WatchFace: buffer, segundos, complicações; WatchFaceDelegate
tools/gen_assets.py       gera fontes e ícone
tools/bake_config.py      embute URL/token para sideload
```
