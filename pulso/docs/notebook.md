# Central no notebook

Para trabalhar com os dados no Claude Code, a central roda no seu notebook. Um comando:

```bash
pulso notebook --ble --tunnel tailscale
```

Ele sobe, no mesmo processo:

| Peça | Para quê |
|---|---|
| API de ingestão + painel (`localhost:8080`) | recebe o mostrador e o app pelo celular; painel em `/painel` |
| Jobs | reanálise a cada 5 min, alertas, Garmin Connect a cada 6 h, sono/reuniões às 04:30, relatório às 07:00, resumo às 07:15 |
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
| App de captura: RR + features de movimento + FC 1 Hz | ≤ 6 KB por lote | celular, ou BLE quando o notebook está perto |
| App de captura: acelerômetro bruto, sessões longas | grande | **BLE para o notebook**; se o notebook não estiver por perto, fica na fila do relógio (limitada) e o bruto completo vai no FIT da sessão |
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
recuperado pelo cursor na próxima sincronização, ou pelo Garmin Connect.

## Checklist do primeiro dia

1. `pip install -e 'central[all,bridge]'` e `pulso token create fr255`.
2. `pulso notebook --ble --tunnel tailscale` e anote a URL.
3. Garmin Connect (celular) → Connect IQ → Pulso e Pulso Captura → configurações: URL e token.
4. `pulso import garmin-export …` e/ou `pulso import garmin --days 180` para o histórico.
5. No Claude Code, dentro de `pulso/`: “como foi minha semana de estresse?”.
