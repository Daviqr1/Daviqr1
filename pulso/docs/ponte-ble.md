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
`BleLink.mc` do Pulso Captura (conferido contra o decodificador em `watch/capture/tests/check_watch_algos.py`:
formato dos frames, HELLO e a prova, MAC do lote sobre `q`, confirmação assinada com tentativa, o filtro de
tipos e o esquecimento do SYNC a cada HELLO).

```
byte 0  seq   u8 (incrementa por frame; o periférico confirma o último recebido)
byte 1  tipo  u8
2..19   payload little-endian

relógio → ponte (característica DATA)
0x0B HELLO      id_chave 4 B | nonce 12 B                         (primeiro frame de cada conexão)
0x00 SYNC       epoch_s u32 | ms u16 | sessão u32 | sid u8        (a cada 10 s)
0x02 ACC_KEY    dt u24 | hz u8 | x i16 | y i16 | z i16 (mG) | sid u8
0x03 ACC_DELTA  k u8 | (dx, dy, dz) i8 × k (k ≤ 5)                (delta > ±127 mG força novo KEY)
0x07 BAT        pct u8 | consumo ×10 u8 (%/h)
0x08 BLOB_START q u32 | tentativa u8                              (um lote v1 da fila vem a seguir)
0x09 BLOB_DATA  até 18 bytes do JSON
0x0A BLOB_END   total u16 | mac 16 B   (HMAC-SHA256(chave, "pulso-blob" | q u32 | JSON)[:16])
0x01, 0x04, 0x05, 0x06  reservados (RR, FEAT, FC, marcação em rascunhos antigos): a ponte descarta

chave    = SHA-256(token do aparelho);  id_chave = SHA-256(chave)[:4]
dt       = ms desde o SYNC cujo sid o ACC_KEY repete (u24 → até 4,6 h)
rótulos  "pulso-blob", "pulso-hello" e "pulso-ack" separam os três MACs: nenhum serve no lugar de outro

ponte → relógio (característica CTRL, notify); todas começam com último_seq u8 | taxa_acel_sugerida u8
           último_seq | taxa                                        a cada 10 frames
0xB2       último_seq | taxa | 0xB2 | prova 16 B                    resposta ao HELLO
           prova = HMAC-SHA256(chave, "pulso-hello" | nonce)[:16]; sem prova = token desconhecido
0xB1       último_seq | taxa | 0xB1 | q u32 | tentativa u8 | status u8 | mac 8 B      (17 B)
           status 0 gravado, 1 rejeitado, 2 assinatura, 3 reenviar
           mac = HMAC-SHA256(chave, "pulso-ack" | nonce | q | tentativa | status)[:8]
```

**Handshake.** Ao conectar, o relógio manda só o HELLO, com um nonce novo (`Cryptography.randomBytes`). A
ponte procura entre os tokens ativos a chave com aquele id e responde com a prova; o relógio confere e só
então entra em `ok` e começa a mandar fluxo e lotes. O HELLO vai até 3 vezes, 5 s entre elas, com o mesmo
nonce; sem resposta ou com prova errada o relógio desconecta e procura outro periférico (o que falhou fica
de fora daquela busca); `0xB2` sem prova (a ponte não conhece o token), ou relógio sem token → espera
2 min e os detalhes mostram `ble token`. Se uma confirmação da tentativa atual não confere, ou nenhuma
chega em 60 s, o relógio refaz a conexão e o HELLO (a ponte pode ter reiniciado ou recebido outro HELLO).

**Tentativas.** Cada envio de um lote é uma tentativa nova (`tentativa` no BLOB_START, repetida no `0xB1`).
Um BLOB_START novo descarta em silêncio a tentativa anterior que ficou pela metade (o relógio já desistiu
dela); `3 reenviar` só sai quando a tentativa **atual** se perde no meio (buraco no `seq`, tamanho). O
relógio ignora qualquer `0xB1` de outra tentativa, de outro lote ou com MAC que não confere.

O relógio escreve o `seq` na hora da transmissão, então um buraco no `seq` é perda no ar. Depois de
qualquer perda o decodificador ignora deltas até o próximo keyframe e aborta o lote (BLOB) em curso — ele
**nunca inventa amostra**, e o relógio reenvia o lote inteiro com o mesmo `q` (a central deduplica). Um
ACC_KEY só é datado contra o SYNC com o mesmo `sid`: se o SYNC se perdeu, o trecho é descartado (no máximo
10 s de bruto) em vez de cair no horário do SYNC anterior. Cada HELLO também faz a ponte esquecer o SYNC
(uma conexão nova pode ser outra execução do app, cujo contador de `sid` recomeçou), e o relógio manda um
SYNC novo depois de cada handshake; o que estava na fila do fluxo de antes da reconexão e dependia do SYNC
antigo é descartado (no máximo ~40 s de bruto). SYNC a mais de 10 min do relógio do notebook também é
descartado.

### O que vai por onde (relógio)

| Tráfego | Por BLE quando | Senão |
|---|---|---|
| Acelerômetro bruto (ACC_KEY + deltas, 1 unidade por callback do sensor) e bateria | ponte conectada e captura rodando; taxa sugerida pela ponte ≠ 0 | não sai do relógio (só a feature por segundo nos lotes; bruto completo no FIT do TREINO/MAX) |
| Lote da fila (JSON v1, RR, features, FC, pressão, histórico, marcações) | celular desconectado, lote > 6 KB, sem URL de central, ou nos 10 min seguintes a uma falha do caminho do celular que não seja do lote (túnel caído, URL velha, timeout, 404/5xx) — o lote que falhou vai para a ponte na hora | celular (HTTPS) |

Acima da taxa que a ponte pede (`taxa_acel_sugerida`, 25 Hz por padrão; 0 desliga o bruto) o relógio tira
a média de grupos de k amostras (k = ⌈taxa/sugerida⌉) — no MAX a 100 Hz seriam ~4× os frames que o link
aguenta. O JSON de um lote é montado em pedaços de 300 valores por segundo: o FR255 sem Music tem metade do
orçamento de watchdog dos outros relógios 5.x.

Os dois tipos se alternam frame a frame, uma escrita por vez. A fila do fluxo guarda ~40 s; se a ponte não
acompanha, descarta o **segundo inteiro** mais antigo (nunca um delta solto, nunca o SYNC do qual os
seguintes dependem). Um lote só sai da fila do relógio depois de um `0xB1` assinado: `0` (gravado no
banco ou aceito pela central remota) ou `1` (o conteúdo que o relógio assinou para aquele `q` foi recusado:
validação local ou `400/413/422` da central). Qualquer outra resposta da central remota — `404` de proxy ou
URL errada, `401/403` do token da ponte, `408`, `429`, `5xx`, sem conexão — vira `3 reenviar`: o lote fica
no relógio; um JSON que diz ser de outro `q` também. MAC que não confere vira `2 assinatura`, que também
não apaga nada.

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
o serviço, conecta, liga as notificações, confere a ponte (HELLO) e começa a mandar; a tela de detalhes mostra
`ble ok 6.1f/s q2 -0 b3` (estado — `busca`, `conect`, `confere`, `ok`, `espera` ou `token` —, frames/s, fila
do fluxo, segundos descartados, lotes entregues). Sem URL de central, a linha de envio da tela principal
mostra o estado da ponte e os erros de envio por ela. O log do
`pulso bridge watch` mostra frames/s a cada minuto — é o primeiro número a medir no seu FR255.

Token: com `pulso notebook --ble` (ou `pulso bridge watch` sem `--central`) a ponte confere o HELLO e a
assinatura dos lotes contra os tokens de aparelho (escopo `device`) ativos do próprio banco — um token do
painel (`pulso token create --painel`) é só leitura e não serve para a ponte. Com `--central … --token T`,
use **o mesmo T** configurado no relógio — senão o handshake falha: os detalhes mostram `ble token` e,
quando a ponte é o único caminho, a tela principal mostra “ponte: token inválido” (a URL pode vir com ou
sem `/v1` ou `/v1/ingest`). Sem token no relógio a ponte não conecta.

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

O characteristic DATA é aberto (o Connect IQ só ganhou bonding nas APIs 4.2.5/5.1) e o UUID do serviço é
público. O que protege:

- **Quem recebe** (relógio → ponte): o relógio não escreve nada além do HELLO até a ponte provar que tem a
  chave (HMAC sobre um nonce novo do relógio). Um periférico que anuncia o mesmo UUID mas não tem o token
  não recebe lote nem fluxo, e o relógio vai procurar outro.
- **O que apaga** (ponte → relógio): todo `0xB1` é assinado com a chave e o nonce daquela conexão e nomeia a
  tentativa; o relógio só apaga um lote com `0xB1` que confere. Sem a chave não dá para forjar “gravado”.
  E a ponte só assina `0` ou `1` para o lote `q` quando recebeu o conteúdo que o próprio relógio assinou
  como `q`: o MAC do lote cobre o `q`, então quem escreve em DATA e repete um lote verdadeiro capturado no
  ar (válido, mas de outro `q`) sob o `q` do lote em voo recebe `2 assinatura` — nada é gravado nem apagado.
- **O que grava** (relógio → ponte): cada lote leva HMAC-SHA256 com chave = SHA-256 do token, sobre o rótulo
  `pulso-blob`, o `q` e o JSON, conferido antes de gravar. A central já guarda exatamente esse SHA-256 de
  cada token (nunca o token), então a ponte local não precisa de segredo próprio; token revogado deixa de
  assinar na hora, e só tokens de aparelho (escopo `device`) assinam — o do painel, que fica no navegador, não.
  O token em si nunca passa pelo ar (o HELLO leva só os 4 primeiros bytes de SHA-256 da chave — fixos por
  token, então identificam o relógio para quem escuta o rádio).
- **Fluxo ao vivo** (acelerômetro bruto e bateria): sem assinatura por frame (custaria metade da banda).
  Alguém a poucos metros, enquanto a ponte roda, consegue escrever em DATA e injetar movimento falso com
  fonte `ble:bridge`, só perto da hora atual (SYNC a mais de 10 min do relógio do notebook é descartado).
  RR, FC e marcações **não** são aceitos pelo fluxo (tipos reservados são descartados): só entram por lote
  assinado. Um frame malformado é contado e descartado; nunca derruba a ponte.

O que **não** está coberto:

- Negação de serviço: quem está no alcance pode embaralhar o `seq` (forçando reenvios), mandar HELLO com
  outro nonce (as confirmações daquela conexão deixam de conferir e o relógio refaz o HELLO; a ponte
  esquece o SYNC e perde até 10 s de bruto), injetar um lote com assinatura errada (o relógio recebe
  `2 assinatura` e espera como num erro de token antes de tentar de novo) ou anunciar o UUID com sinal mais
  forte. Nada disso apaga ou altera dado — o lote espera na fila. A ponte usa um decodificador só para
  todos os que escrevem em DATA (o `bless` não diz quem escreveu).
- Leitura: os lotes não são cifrados. Quem escuta o rádio lê o JSON dos lotes que passam (e pode
  repeti-los — repetido sob o mesmo `q`, a central deduplica; sob outro `q`, o MAC não confere). Um
  atacante perto do relógio **e** da ponte ligada ao mesmo tempo pode ainda repassar o HELLO para a ponte
  verdadeira (*relay*) e segurar os lotes. Nenhum dos dois consegue fazer o relógio apagar um lote que a
  ponte não gravou, nem fazer a ponte gravar um lote que o relógio não assinou.
- Próximos passos se quiser fechar isso também: bonding (`Device.requestBond`, suportado no FR255) e/ou
  cifrar o lote (AES no `Cryptography.Cipher`) — a testar com BlueZ/NimBLE e no orçamento de watchdog.

## O que medir primeiro no seu relógio

1. Vazão real: frames/s no log da ponte com `WRITE_TYPE_DEFAULT` e com resposta (o ajuste “escrita com
   confirmação” vale a partir do próximo frame, sem reiniciar o app).
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
