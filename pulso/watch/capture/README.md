# Pulso Captura (Forerunner 255)

App de relógio (Connect IQ, `watch-app`) que tira dados de alta resolução do FR255 e manda para a
central. Ele existe porque o **mostrador não pode ler sensores**: RR batimento a batimento,
acelerômetro e pressão bruta só chegam a um app aberto em primeiro plano. A análise pesada fica na
central; o relógio captura, calcula o mínimo (features de 1 s, HRV para a tela), guarda numa fila e
transmite.

- Contrato do fio: [`docs/protocolo.md`](../../docs/protocolo.md) (validado em `central/pulso/ingest/protocol.py`).
- Ponte BLE com o notebook: [`docs/ponte-ble.md`](../../docs/ponte-ble.md).

## Modos

| Modo | Para quê | O que grava | Período do sensor | Envio | Tela | FIT |
|---|---|---|---|---|---|---|
| **FOCO** | reuniões, trabalho | RR; acelerômetro 25 Hz → ENMO/MAD por segundo; FC 1 Hz; pressão bruta a cada 10 s | 1 s | a cada 60 s | completa | não |
| **SONO** | a noite | o mesmo; pressão a cada 60 s | 4 s | a cada 10 min | preta, mínima, 1×/min | não |
| **TREINO** | treino | o mesmo + **DFA α1 ao vivo**; cinta peitoral habilitada | 1 s | a cada 60 s | α1 + zona | sim, se “Gravar FIT” ligado |
| **MAX** | tudo | acelerômetro na **taxa máxima** do aparelho, giroscópio se existir, FIT sempre | 1 s | a cada 60 s | completa | sempre |

Em todos os modos cada lote leva ainda: bateria (`bat`), um retrato do ActivityMonitor (`m`: estresse
Garmin de 30 s, respiração, passos, FC de repouso medida) e, a cada 10 min, os pontos novos do SensorHistory
(estresse, Body Battery, SpO2; até 60 por série por coleta, o resto vem na próxima). Se esses pontos
fariam o lote passar do limite, o lote aberto é fechado antes; o cursor de cada série só é gravado
quando o lote com os pontos já está na fila. No início de cada sessão vai um evento de configuração, por exemplo
`["cfg", "acc_max=100 sys_max=100 gyro_max=0 p=1 hz=25 gyro=0 fit=acc v=0.1.0"]`, com as taxas
máximas que o relógio informou em tempo de execução (nada fica fixo no código).

O RR vai **bruto** para a central (que limpa de verdade). No relógio há só um filtro leve, usado na tela:
300–2000 ms e desvio < 30 % da mediana móvel de 9 batimentos.

## Botões

| Botão | Ação |
|---|---|
| START | inicia; com captura rodando, pergunta se para (e salva) |
| UP | marcação rápida **“estresse”** (vibra e mostra aviso) |
| DOWN | alterna status ↔ detalhes |
| MENU (segurar UP) | iniciar/parar, modo, marcar evento (estresse, café, reunião começou, reunião acabou, pausa), gravar FIT, enviar agora |
| BACK | sai; com captura rodando, pergunta “Parar captura e sair?” |

Marcações funcionam também **sem captura**: viram um lote pequeno enviado na hora, então o app serve
de “botão de estresse” mesmo parado.

## Tela

- **Status**: modo e tempo; FC; RMSSD dos últimos 60 batimentos aceitos (no TREINO/MAX, α1 e a barra
  `<0,5 ANAERÓBIO | 0,5–0,75 TRANSIÇÃO | ≥0,75 AERÓBIO`); qualidade do RR (% aceitos nos últimos 100);
  movimento (ENMO, mg) e pressão; bateria, consumo em %/h e nível do governador (N1–N3); fila; estado do envio.
- **Detalhes** (DOWN): id do aparelho, último `q`, sessão, taxa pedida/entregue do acelerômetro, máximos
  informados, governador, estado do FIT e amostras gravadas, enviados/duplicados/descartados, último
  código HTTP, memória.
- Valores somem (“--”) se nenhum batimento aceito chegou nos últimos 30 s.

## Configuração

| Ajuste | Padrão | |
|---|---|---|
| URL da central | vazio | ex.: `https://pulso.seudominio.dev` (sem `/v1`); HTTPS com certificado público |
| Token do aparelho | vazio | `pulso_dev_...` (gerado na central) |
| Modo padrão | FOCO | modo ao abrir o app |
| Orçamento de bateria | 3 %/h | FOCO; o TREINO usa o dobro; 0 desliga o governador |
| Orçamento no SONO | 1 %/h | |
| Bateria mínima | 15 % | abaixo disso a captura para e salva |
| Gravar FIT no TREINO | sim | |

**App instalado pela loja (inclusive beta privado)**: ajuste pelo app Garmin Connect → Dispositivo →
Atividades e apps → Pulso Captura → Configurações.

**App instalado manualmente (sideload)**: o Garmin Connect não edita configurações de apps fora da loja.
Coloque os valores no build, num arquivo que **não vai para o git** (`local/` está no `.gitignore`):

```xml
<!-- watch/capture/local/properties.xml -->
<properties>
    <property id="centralUrl" type="string">https://pulso.seudominio.dev</property>
    <property id="deviceToken" type="string">pulso_dev_xxxxxxxx</property>
</properties>
```

Sem central configurada a captura funciona normalmente, guarda na fila e a tela mostra “configure a central”.

## Para onde vão os dados

1. **HTTPS pelo celular** (padrão): `POST <central>/v1/ingest` via Garmin Connect Mobile, `Authorization:
   Bearer <token>`, JSON, uma requisição por vez. Precisa do celular por perto com o Garmin Connect aberto
   em segundo plano. Banda real de 400–800 B/s; um minuto de FOCO dá ~1,2 KB.
2. **Arquivo FIT** (TREINO/MAX): a atividade “Pulso Treino”/“Pulso Max” sai com o acelerômetro bruto do
   `SensorLogger` e os campos de desenvolvedor abaixo. Pegue por USB em `GARMIN/Activity/` ou exporte o
   original no Garmin Connect (atividade → engrenagem → “Exportar original”). O importador FIT da central lê.
3. **Ponte BLE** (menu → “Ponte BLE (notebook)”, ou nas configurações): o relógio conecta direto no
   `pulso notebook --ble` / `pulso bridge watch` (`BleLink.mc`). Por ela vão:
   - o **acelerômetro bruto** ao vivo, na taxa da captura — grande demais para o celular. Fila de ~40 s no
     relógio; se a ponte não acompanha, o segundo mais antigo inteiro é descartado (nunca meio segundo);
   - os **lotes da fila** quando o celular não está conectado, quando o lote passa de 6 KB ou quando não há
     URL de central configurada: JSON assinado com HMAC-SHA256 (chave = SHA-256 do token), apagado da fila só
     depois que a ponte confirma que gravou.

   Roteamento por lote (`Uploader.route`): celular é o padrão; BLE nos três casos acima. A tela de detalhes
   mostra `ble <estado> <frames/s> q<fila> -<descartados> b<lotes>` e por onde saiu o último lote.
   Use o **mesmo token** no relógio e na ponte remota (`pulso bridge watch --central … --token …`); com
   `pulso notebook --ble` a ponte confere contra os tokens do próprio banco.

Campos de desenvolvedor no FIT (um registro por segundo):

| id | nome | tipo | conteúdo |
|---|---|---|---|
| 0 | `rr` | UINT16 × 6 | batimentos que chegaram naquele segundo, ms; 0 = posição vazia |
| 1 | `enmo` | UINT16 | mg |
| 2 | `dfa_a1` | UINT16 | α1 × 100; 0 = indisponível |
| 3 | `gyro` | UINT16 | °/s médio (só no MAX, quando há giroscópio) |

GPS fica desligado de propósito: não ajuda nesta análise e custa bateria.

## O que vai em cada lote

`v, d, q, src="cap", sid, t` + as séries no formato exato do protocolo:

- `rr`: `[[t_último_batimento_ms, [rr...]], ...]`. Chamadas seguidas do sensor viram um bloco só
  enquanto a soma dos RR bate com o relógio. Como um batimento nunca fecha depois da chamada que o
  entrega, o horário segue o **envelope inferior** (mín. entre soma dos RR e hora da chamada). Em
  simulação o erro de horário por batimento caiu de ~650 ms para 0–100 ms. Se faltar tempo demais
  (batimentos perdidos), começa outro bloco.
- `hr1`: `[t0_s, [bpm|null...]]` por segundo. `acc`: `[[t0_ms, t0_s, [enmo...], [mad...]]]` em mg inteiros.
  `bar`: `[t0_s, passo_s, [Pa|null...]]`.
- `ev`: `cap` (início/fim), `cfg`, `mark`, `gov` (mudança de nível), `q` (`drop=n`), `err`.
- `bat`: `[t_s, %, dias]`; `m`: retrato do ActivityMonitor; `h`: SensorHistory (cursor por série).

`d` é aleatório, criado uma vez (`fr255-cap-xxxxxx`). O mostrador tem o dele, porque apps não
compartilham armazenamento. `q` é persistido na mesma gravação que torna o lote enviável, então
nunca se repete para conteúdo diferente (a central deduplica por `(d, q)`).

## Fila e envio

- Fila em `Application.Storage`, uma chave por lote (≤ ~4,5 KB de JSON cada, abaixo do limite de 6 KB
  do POST), **até 80 KB no total**. Cheia, descarta o lote mais antigo e avisa a central (`ev q drop=n`).
  Isso cobre ~1 h de FOCO ou ~1h20 de SONO sem celular.
- Um lote é fechado a cada ciclo de envio do modo ou quando passa de ~4 KB (FOCO/TREINO/MAX, que então
  envia na hora). No SONO só envia antes dos 10 min se a fila passar de 24 KB.
- Erros: sem celular (`-104`) espera 1 → 30 min; outros erros 15 s → 10 min, dobrando; `401/403` espera
  30 min (token errado). `400/413/422` e corpo grande demais descartam o lote (a central nunca o
  aceitaria); oito falhas seguidas do mesmo lote por erro do próprio lote (`500/501`, `-402/-403`) também.
  Central desligada, túnel caído (`404`, `502–504`, `52x`), timeouts e erros de BLE **nunca** descartam:
  o lote espera na fila. Cada tentativa tem o seu próprio callback, então uma resposta atrasada de uma
  tentativa abandonada nunca é creditada a outro lote. “Enviar agora” no menu zera a espera.

## Bateria e o governador

O consumo real **ainda vai ser medido** no seu relógio, não há número prometido aqui. O app mede e se ajusta:

- A cada 5 min lê a bateria e estima o consumo (%/h) pela inclinação da última hora (o % da bateria é
  grosseiro; uma diferença de 5 min é quase só ruído), suavizado por EMA.
- Compara com o orçamento do modo e muda de nível com histerese (mais de 1,2× o orçamento desce, menos
  de 0,7× sobe), no máximo a cada 20 min e só com ≥ 30 min de janela:

  | Nível | Efeito |
  |---|---|
  | 0 | preset do modo |
  | 1 | sensor a cada 4 s, envio 2× mais espaçado |
  | 2 | acelerômetro 10 Hz, envio 3× |
  | 3 | acelerômetro desligado (só RR + FC), pressão 3× mais espaçada, envio 4× |

- Lembra o consumo medido no nível que deixou e não volta, por 3 h, para um nível que estourou o
  orçamento. Em simulação com leituras de 1 %: 5 trocas em 8 h em vez de 12, e consumo médio dentro do orçamento.
- Abaixo da bateria mínima a captura para, salva o FIT e manda o que tem. No SONO não vibra.
- MAX não tem governador, só o piso. Carregando, volta ao nível 0.

## Limites honestos

- **RR do pulso não serve em movimento.** O sensor óptico entrega RR razoável parado (reunião, sono),
  mas cai muito ao se mexer. Para treino use **cinta peitoral pareada como sensor de FC do relógio**
  (HRM-Pro/HRM-Dual por ANT+ é o caminho mais seguro). Se o RR de uma cinta BLE (ex.: Polar H10)
  chega ao Connect IQ ainda precisa ser **verificado no seu relógio**. A H10 também pode ir direto ao
  notebook pela ponte (`pulso bridge hrm`).
- **O app precisa ficar aberto.** O FR255 não tem multitarefa: voltar ao mostrador fecha o app (ele
  salva tudo ao fechar). Não existe captura bruta 24/7 neste relógio; fora das sessões quem coleta é o
  mostrador (SensorHistory e ActivityMonitor, resolução de minutos).
- **Não há PPG bruto** no Connect IQ, só RR já processado pelo firmware. O FR255 provavelmente não tem
  giroscópio físico, embora o perfil do simulador informe 100 Hz. O app pergunta em tempo de execução e
  recua sozinho (listener, depois logger sem giroscópio).
- Sem celular por mais de ~1 h a fila começa a descartar o mais antigo. No TREINO/MAX o FIT é a cópia de segurança.
- A fila só esvazia **com o app aberto**: o app de captura não tem serviço de background (lá o corpo do
  POST cairia para ~3 KB). Depois de uma sessão longe do celular, abra o app perto dele por um minuto.

## Compilar e instalar

Requer o Connect IQ SDK (9.x) e uma chave de desenvolvedor:

```bash
openssl genrsa -out developer_key.pem 4096
openssl pkcs8 -topk8 -inform PEM -outform DER -in developer_key.pem -out developer_key.der -nocrypt

monkeyc -d fr255 -f watch/capture/monkey.jungle -o PulsoCaptura.prg -y developer_key.der -l 3 -w
```

Compila com **zero erros e zero avisos** em `-l 3` (tipagem estrita) e `-l 2`, contra os arquivos de
dispositivo do FR255 (API 5.2).

Sideload: conecte o relógio por USB (no Linux aparece como MTP: `gio mount`, `jmtpfs` ou o gerenciador
de arquivos; no macOS, OpenMTP), copie `PulsoCaptura.prg` para `GARMIN/APPS/` e desconecte. O app aparece
na lista de atividades/apps. Para atualizar, substitua o arquivo; a fila e o id do aparelho continuam no
armazenamento do app.

## Testes no computador

`tests/check_watch_algos.py` refaz em Python, com float32 como no relógio, os algoritmos do app e confere:

- DFA α1 contra `central/pulso/signal/hrv.py` (diferença máxima ~1e-5 em 150 séries);
- ENMO/MAD contra `central/pulso/signal/activity.py` (só arredondamento);
- filtro + RMSSD com 5 % de artefatos (erro mediano ~3 %; sem filtro, centenas de %);
- horário dos batimentos reconstruído pela central (p95 < 100 ms);
- governador em malha fechada com bateria de 1 %;
- um lote no formato do relógio passando pelo validador e pelo `ingest()` da central, inclusive o reenvio duplicado.

```bash
python3 watch/capture/tests/check_watch_algos.py     # numpy + pydantic
```

São transliterações à mão: quem mudar `HrvLive.mc`, `AccelFeatures.mc`, `BatchBuilder.mc` ou
`Governor.mc` deve atualizar o teste junto.

## Arquivos

| Arquivo | Papel |
|---|---|
| `source/PulsoCapturaApp.mc` | ciclo de vida do app |
| `source/Capture.mc` | controlador: sensores, tick de 1 Hz, lotes, marcações, SensorHistory |
| `source/HrvLive.mc` | filtro de RR, RMSSD, DFA α1 incremental (sem alocação por batimento) |
| `source/AccelFeatures.mc` | ENMO/MAD (e giroscópio) por segundo |
| `source/BatchBuilder.mc` | lote aberto no formato do protocolo + estimativa de tamanho |
| `source/UploadQueue.mc` | fila persistente limitada, sequência `q` |
| `source/Uploader.mc`, `source/Transport.mc` | envio com backoff e roteamento celular/BLE; `HttpTransport`, `BleTransport` |
| `source/BleLink.mc`, `source/Json.mc` | ponte BLE (scan, conexão, frames de 20 B, lotes assinados); JSON → bytes |
| `source/Governor.mc` | governador de bateria |
| `source/FitRecorder.mc` | sessão FIT, SensorLogger, campos de desenvolvedor |
| `source/StatusView.mc`, `source/Delegates.mc` | tela, botões e menus |
| `source/Cfg.mc`, `source/Clock.mc` | configurações + presets; relógio em ms disciplinado |
