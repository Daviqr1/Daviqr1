# Pulso — notas para o Claude Code

Sistema pessoal de saúde e performance em cima de um Garmin Forerunner 255. Leia `README.md` e
`docs/arquitetura.md` antes de mudar algo estrutural.

## Dados

- O MCP `pulso` (`.mcp.json`) dá acesso ao banco da central (`~/.pulso/pulso.sqlite` por padrão).
  Para perguntas sobre saúde/estresse/sono/reuniões, use as ferramentas MCP (`inventario`,
  `tabela_diaria`, `dia`, `reunioes`, `reuniao`, `sono`, `correlacionar`, `fatos`) em vez de ler o
  SQLite na mão. Cite n, rho e p ao falar de correlação; correlação não é causa.
- Unidades: ativação em z (desvios do normal da própria pessoa naquele horário); FC adicional em bpm;
  HRV (RMSSD) em ms; horários no fuso `PULSO_TZ`.
- Dado de saúde e transcrição de reunião são sensíveis: não copie para fora do repositório nem para
  issues/PRs; o resumo exportado (`pulso export`) já sai com nomes pseudonimizados.

## Código

- `central/`: Python ≥ 3.12. Testes: `cd central && pytest` (sem rede, dados sintéticos em `tests/synth.py`).
- Toda amostra tem `source`; séries calculadas usam `derived:v1` e são recalculáveis. Nunca grave
  derivado como se fosse medida.
- Modelos só usam histórico **anterior** ao dia analisado.
- Números que chegam ao Claude no relatório passam pelo gate de `ai/facts.py`; não contorne.
- `watch/`: Monkey C para `fr255` (API 5.2, sem multitarefa, mostrador sem sensores). Compile com o SDK
  Connect IQ 9.2 (`monkeyc -d fr255 -f watch/<app>/monkey.jungle -o out.prg -y developer_key.der -l 2 -w`).
  O contrato com a central é `docs/protocolo.md` + `central/pulso/ingest/protocol.py`: mudou um, mude o outro.

## Comandos úteis

```bash
pulso notebook --ble --tunnel tailscale   # central inteira no notebook
pulso inventory                           # o que tem no banco
pulso analyze --days 7                    # recalcula
pulso report --dry-run --days 1           # fatos que o relatório usaria
pulso export --drive                      # resumo para o NotebookLM
```
