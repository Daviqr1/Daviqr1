# Central no notebook

Para trabalhar com os dados no Claude Code, a central roda no seu notebook. Um comando:

```bash
pulso notebook --ble --tunnel tailscale
```

Ele sobe, no mesmo processo:

| Peça | Para quê |
|---|---|
| API de ingestão + painel (`localhost:8080`) | recebe o mostrador e o app pelo celular; painel em `/painel` (token `pulso token create painel --painel`) |
| Jobs | reanálise e alertas a cada 5 min, Garmin Connect a cada 6 h (de hora em hora pela manhã até a noite passada chegar), sono/reuniões a partir das 04:30, a noite passada assim que chega, relatório às 06:30, os três resumos às 06:45 e o “Hoje” a cada 2 h ([horários](deploy.md#1-vps-ou-servidor-com-domínio-recomendado)) |
| Ponte BLE (`--ble`) | o relógio manda direto para o notebook, sem celular |
| Túnel (`--tunnel`) | HTTPS público para o celular alcançar o notebook |

Os dados ficam em `~/.pulso/` (banco, FIT crus, resumos). O Claude Code usa o mesmo banco pelo
`.mcp.json` do projeto (`pulso mcp` em stdio) — abra o Claude Code na pasta `pulso/` e pergunte.

## Por onde cada pacote vai

O FR255 não tem Wi-Fi: fora do celular, o único caminho direto até o notebook é BLE (ou USB para os FIT).
O roteamento é por tamanho e por quem está por perto:

| Pacote | Tamanho | Caminho |
|---|---|---|
| Mostrador: histórico do relógio, estresse, passos, bateria (a cada 5 min) | ≤ 3 KB | celular → túnel → notebook |
| App de captura: RR + features de movimento + FC 1 Hz + histórico | ≤ 6 KB por lote | celular; **BLE** (lote assinado) quando o celular não está conectado ou o lote passou de 6 KB |
| App de captura: acelerômetro bruto ao vivo | ~150 B/s | **só BLE para o notebook**; ~40 s de fila no relógio; sem a ponte, o bruto completo vai no FIT (TREINO/MAX) e no resto fica só a feature por segundo |
| FIT do relógio (monitoramento, sono, HRV, atividades) | MB | USB (MTP) ou zip diário do Garmin Connect |

É o mesmo caminho pelo celular que você já usa na tela do limite do Claude (o `makeWebRequest` passa pelo
Garmin Connect Mobile). A diferença é que o destino é a central no notebook, via túnel.

## Túnel

- **Tailscale Funnel** (recomendado): URL fixa `https://<maquina>.<tailnet>.ts.net`. Configure uma vez nas
  settings dos apps do relógio. `tailscale funnel --bg 8080`.
- **cloudflared** (sem conta): `--tunnel cloudflared` abre um túnel rápido; a URL muda a cada execução.
  Bom para testar, ruim para o dia a dia.

Com o notebook dormindo ou desligado, o mostrador continua funcionando com o último estado. Os lotes
do app de captura ficam na fila do relógio e saem quando a central volta. O histórico do relógio é
recuperado pelo cursor na próxima sincronização, ou pelo Garmin Connect. Os jobs que perderam o horário
rodam quando o notebook volta: sono/reuniões e os três resumos no mesmo dia, o relatório até 12:00.

## Checklist do primeiro dia

1. `pip install -e 'central[all,bridge]'`, `pulso token create fr255` (relógio) e
   `pulso token create painel --painel` (token só de leitura para o `/painel`; o do relógio não abre o painel).
2. `pulso notebook --ble --tunnel tailscale` e anote a URL.
3. Garmin Connect (celular) → Connect IQ → Pulso e Pulso Captura → configurações: URL e token do relógio
   (só para apps da loja; instalados por USB, embuta os dois no build: [COMECE-AQUI](../COMECE-AQUI.md#4-relógio)).
4. `pulso import garmin-export …` e/ou `pulso import garmin --days 180` para o histórico.
5. No Claude Code, dentro de `pulso/`: “como foi minha semana de estresse?”.
