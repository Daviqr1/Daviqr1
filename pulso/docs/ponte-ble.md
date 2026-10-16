# Ponte BLE (relógio → notebook / ESP32) e cinta de referência

## O que é e o que não é

O Connect IQ deixa um app do relógio agir como **central BLE**: ele procura um periférico, conecta e
escreve em características GATT. Isso permite que o **Pulso Captura** mande RR e movimento direto para
o seu notebook na mesa, sem passar pelo celular nem pelo Garmin Connect Mobile.

O que a pesquisa encontrou (detalhes e fontes na seção “Fontes”):

| Fato | Consequência |
|---|---|
| O relógio só faz papel de **central**; não anuncia nem hospeda GATT | o notebook (ou um ESP32) é o periférico; o relógio escreve nele |
| Cada escrita tem **no máximo 20 bytes**, sem troca de MTU, **uma operação por vez** | protocolo em frames de 20 bytes com fila no relógio |
| Cadência medida em outros relógios: 66–220 ms por escrita (≈ 90–300 B/s); **FR255 ainda sem número** | 25 Hz de acelerômetro bruto (150 B/s) cabe em relógios rápidos, não nos lentos — a taxa é adaptativa |
| A conexão só existe enquanto o app está aberto (sem multitarefa no FR255) | ponte = “sessão de mesa” explícita |
| O mostrador **não** tem BLE em primeiro plano | a ponte é do app de captura |
| Não use o celular já pareado com o Garmin Connect como periférico | relatos de conexão que nunca completa |

Ou seja: a ponte **não dá mais banda** que o celular (os dois andam na casa de centenas de bytes/s). O ganho
real é independência do celular, latência baixa e estável, e os dados chegando direto onde a análise roda.

## Protocolo de frames (≤ 20 bytes)

Implementado em `central/pulso/bridge/frames.py` (decodificador e codificador de referência) e no
`BleLink.mc` do Pulso Captura (conferido contra o decodificador em `watch/capture/tests/check_watch_algos.py`).

```
byte 0  seq   u8 (incrementa por frame; o periférico confirma o último recebido)
byte 1  tipo  u8
2..19   payload little-endian

0x00 SYNC       epoch_s u32 | ms u16 | sessão u32                 (a cada 30 s)
0x01 RR         dt u24 (último batimento) | n u8 | rr u16 × n (n ≤ 6)
0x02 ACC_KEY    dt u24 | hz u8 | x i16 | y i16 | z i16 (mG)
0x03 ACC_DELTA  k u8 | (dx, dy, dz) i8 × k (k ≤ 5)                (delta > ±127 mG força novo KEY)
0x04 FEAT       dt u24 | n u8 | (enmo u16, mad u16) × n (n ≤ 3, janelas de 1 s)
0x05 HR         dt u24 | bpm u8
0x06 MARK       dt u24 | código u8 (1 estresse, 2 café, 3 reunião começou, 4 reunião acabou)
0x07 BAT        pct u8 | consumo ×10 u8 (%/h)
0x08 BLOB_START q u32                                             (um lote v1 da fila vem a seguir)
0x09 BLOB_DATA  até 18 bytes do JSON
0x0A BLOB_END   total u16 | mac 16 B   (HMAC-SHA256(SHA-256(token), JSON)[:16])

dt = ms desde o último SYNC (u24 → até 4,6 h)
notificação do periférico (característica CTRL): último_seq u8 | taxa_acel_sugerida u8
                                    [| 0xB1 | q u32 | status u8]   (0 gravado, 1 rejeitado, 2 assinatura, 3 reenviar)
```

O relógio escreve o `seq` na hora da transmissão, então um buraco no `seq` é perda no ar. Depois de
qualquer perda o decodificador ignora deltas até o próximo keyframe e aborta o lote (BLOB) em curso — ele
**nunca inventa amostra**, e o relógio reenvia o lote inteiro com o mesmo `q` (a central deduplica).

### O que vai por onde (relógio)

| Tráfego | Por BLE quando | Senão |
|---|---|---|
| Acelerômetro bruto (ACC_KEY + deltas, 1 unidade por callback do sensor) e bateria | ponte conectada e captura rodando; taxa sugerida pela ponte ≠ 0 | não sai do relógio (só a feature por segundo nos lotes; bruto completo no FIT do TREINO/MAX) |
| Lote da fila (JSON v1, RR, features, FC, pressão, histórico, marcações) | celular desconectado, lote > 6 KB ou sem URL de central | celular (HTTPS) |

Acima da taxa que a ponte pede (`taxa_acel_sugerida`, 25 Hz por padrão; 0 desliga o bruto) o relógio tira
a média de grupos de k amostras (k = ⌈taxa/sugerida⌉) — no MAX a 100 Hz seriam ~4× os frames que o link
aguenta. O JSON de um lote é montado em pedaços de 300 valores por segundo: o FR255 sem Music tem metade do
orçamento de watchdog dos outros relógios 5.x.

Os dois tipos se alternam frame a frame, uma escrita por vez. A fila do fluxo guarda ~40 s; se a ponte não
acompanha, descarta o **segundo inteiro** mais antigo (nunca um delta solto, nunca o SYNC do qual os
seguintes dependem). Um lote só sai da fila do relógio depois do `0xB1 … 0` da ponte, que vem depois de
gravado no banco (ou aceito pela central remota).

GATT (UUIDs em `bridge/peripheral.py`):

| | UUID | propriedades |
|---|---|---|
| serviço | `6c1e0001-5b7a-4d6e-9f2a-50554c534f31` | anunciado no payload primário |
| DATA | `6c1e0002-…` | write, write without response |
| CTRL | `6c1e0003-…` | notify, read |

## Rodando

```bash
pip install -e 'central[bridge]'            # bless + bleak (Linux/BlueZ, macOS, Windows)

# notebook como periférico; grava direto no banco local
pulso bridge watch
# ...ou manda para a central remota
pulso bridge watch --central https://pulso.seudominio.dev --token pulso_dev_...
```

No relógio: Pulso Captura → menu → **Ponte BLE (notebook)** (ou nas configurações do app). O relógio procura
o serviço, conecta, liga as notificações e começa a mandar; a tela de detalhes mostra
`ble ok 6.1f/s q2 -0 b3` (estado, frames/s, fila do fluxo, segundos descartados, lotes entregues). O log do
`pulso bridge watch` mostra frames/s a cada minuto — é o primeiro número a medir no seu FR255.

Token: com `pulso notebook --ble` (ou `pulso bridge watch` sem `--central`) a ponte confere a assinatura dos
lotes contra os tokens ativos do próprio banco. Com `--central … --token T`, use **o mesmo T** configurado
no relógio — senão o lote volta como “token inválido”.

ESP32 (fase 2, sem notebook ligado): o mesmo GATT com NimBLE-Arduino 2.x, encaminhando os frames por
Wi-Fi para `POST /v1/ingest` depois de decodificar (ou mandando os frames crus para um endpoint que use
`frames.Decoder`). Um ESP32 também pode pedir intervalo de conexão menor, que é o principal parâmetro
desconhecido de vazão.

## Cinta de referência e o broadcast do FR255

`pulso bridge hrm` lê qualquer dispositivo com o **Heart Rate Service** padrão (0x180D):

```bash
pulso bridge hrm --name "Polar H10"                  # RR de qualidade ECG, direto no notebook
pulso bridge hrm --name "Forerunner 255" --probe     # 2 min: o broadcast de FC do relógio traz RR?
```

- **Cinta (Polar H10, HRM-Pro em BLE, TICKR)**: RR com precisão de ECG, independente de qualquer limite
  da Garmin. É a referência para HRV/estresse na mesa e para calibrar o RR óptico do pulso. Entra no
  banco como fonte `ble:strap`, a de maior prioridade.
- **Broadcast do FR255** (“Transmitir frequência cardíaca”): relatos indicam que **não** inclui RR (flag
  0x10 desligada) — só FC ~1 Hz. O `--probe` responde isso no seu relógio em 2 minutos.

## Segurança

O characteristic DATA é aberto (o Connect IQ só ganhou bonding nas APIs 4.2.5/5.1). O que protege:

- **Lotes da fila** (tudo o que vira medida, marcação ou evento): HMAC-SHA256 com chave = SHA-256 do token do
  aparelho, conferido antes de gravar. A central já guarda exatamente esse SHA-256 de cada token (nunca o
  token), então a ponte local não precisa de segredo próprio; token revogado deixa de assinar na hora. O
  token em si nunca passa pelo ar.
- **Fluxo ao vivo** (acelerômetro bruto e bateria): sem assinatura por frame (custaria metade da banda).
  Alguém a poucos metros, durante a sessão de mesa, conseguiria injetar movimento falso com fonte
  `ble:bridge` — que entra só na análise de movimento, nunca em RR, estresse ou eventos.
- Próximos passos se quiser fechar o fluxo também: bonding (`Device.requestBond`, suportado no FR255) ou um
  HMAC por SYNC cobrindo os frames do intervalo — a testar com BlueZ/NimBLE.

## O que medir primeiro no seu relógio

1. Vazão real: frames/s no log da ponte com `WRITE_TYPE_DEFAULT` e com resposta.
2. Se o FR255 aceita intervalo de conexão menor pedido pelo periférico (ESP32).
3. Se a conexão BLE do app convive com o link do Garmin Connect Mobile sem derrubar sincronização.
4. Consumo de bateria em: conectado parado, só RR, RR + acelerômetro 25 Hz.
5. Se o broadcast de FC traz RR (`--probe`).

## Fontes

- SDK 9.2.0, `Toybox/BluetoothLowEnergy*.html` (papel central, limite de 20 bytes, 3 perfis, um delegate) e
  amostra `NordicThingy52` (fila de operações).
- Fórum Connect IQ, tópico 390619 (cadência de escrita em Fenix 5+ e vívoactive 5).
- `bless` 0.3.0 (PyPI, dez/2025); NimBLE-Arduino 2.x (h2zero).
- DC Rainmaker, “Bluetooth running broadcasting” (2020): broadcast sem RR.
