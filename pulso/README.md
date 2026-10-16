# Pulso

Sistema pessoal de saúde e performance em cima de um **Garmin Forerunner 255**: tira do relógio tudo o
que ele deixa sair — no máximo de resolução —, junta com o histórico do Garmin Connect, agenda,
transcrições de reunião e commits, e usa estatística séria + Claude para responder **de onde vem o
estresse, quando, com quem, e o que isso faz com o seu sono e o seu treino**.

> Não mexe no firmware do relógio. O que limita a precisão não é o algoritmo da Garmin rodando no
> pulso, é o que a Garmin deixa sair dele — então o Pulso captura o máximo permitido e faz o
> processamento pesado onde há CPU. Detalhes: [o que dá e o que não dá](docs/o-que-da-e-o-que-nao-da.md).

## O que tem aqui

| Peça | O que faz |
|---|---|
| **Mostrador Pulso** (`watch/face`) | tela de saúde/performance: ativação das últimas 12 h, “agora”, Body Battery, recuperação, sono, HRV noturna, próxima reunião com carga prevista, insight do dia; sincroniza com a central a cada 5 min |
| **Pulso Captura** (`watch/capture`) | app de captura em alta resolução: RR batimento a batimento, acelerômetro, FC 1 Hz, barômetro; modos Foco, Sono, Treino (DFA α1 ao vivo) e Máx; governador de bateria; FIT com IMU bruta; marcações por botão |
| **Central** (`central/`) | ingestão HTTPS, banco SQLite, importadores (FIT, Garmin Connect, export da conta, agenda, transcrições, git), análise, painel web, relatórios com Claude, servidor MCP |
| **Ponte BLE** (`central/pulso/bridge`) | relógio → notebook sem celular; cinta peitoral (Polar H10) direto no notebook |

## Como o estresse é medido

1. RR limpo pelo método do Kubios (Lipponen & Tarvainen 2019) → HRV em janelas de 2 min, **só quando você
   está parado** (o RR do pulso não presta em movimento).
2. **FC adicional**: quanto o coração bate acima do que o seu movimento e o horário explicam (Myrtek).
3. **HRV não metabólica**: quanto a sua HRV está abaixo do que o movimento explica (Verkuil/Brown).
4. Os sinais acima + o estresse da Garmin entram num **filtro de Kalman** → *ativação* em z (desvios do
   **seu** normal), com incerteza.
5. Episódios, ligação com reuniões/commits/marcações, comparação com **os mesmos horários sem reunião**,
   efeito por falante e por tema nas transcrições, reuniões coladas vs. com pausa, sono, prontidão por HRV.
6. O Claude escreve o relatório **só com fatos numerados**; todo número e horário é verificado contra os
   fatos — o que não bate é descartado.

Método, referências e limites de cada número: [docs/ciencia.md](docs/ciencia.md).

## Começo rápido

```bash
cd central && pip install -e '.[all]'            # Python ≥ 3.12
export PULSO_DB=~/pulso/pulso.sqlite PULSO_TZ=America/Sao_Paulo

pulso import garmin-export ~/Downloads/garmin-export.zip   # anos de histórico (export da conta)
pulso import garmin --days 60                               # últimos dias pela API (conta própria)
pulso import ics agenda.ics --me voce@empresa.com
pulso import transcript reunioes/*.vtt                      # ancora pela agenda
pulso import git ~/code/* --author "Seu Nome"
pulso analyze --days 60
pulso tag --days 30                                         # etiqueta trechos das reuniões (Claude)
pulso report --kind weekly --days 7                         # relatório verificado (Claude)
pulso serve                                                 # ingestão + painel em http://localhost:8080/painel
claude mcp add pulso -- pulso mcp                           # o Claude consulta e cruza seus dados
```

Relógio: compile e instale `watch/face` e `watch/capture` (instruções em cada README), gere um token com
`pulso token create fr255` e configure URL + token nas settings dos apps pelo Garmin Connect. A central
precisa de HTTPS com certificado público para o relógio: [docs/deploy.md](docs/deploy.md) (Docker + Caddy,
ou túnel Cloudflare/Tailscale).

## Documentação

- [O que dá e o que não dá no FR255](docs/o-que-da-e-o-que-nao-da.md) — SO, sensores, limites, ajustes escondidos
- [Arquitetura](docs/arquitetura.md)
- [Ciência](docs/ciencia.md)
- [Protocolo relógio ↔ central](docs/protocolo.md)
- [Ponte BLE e cinta](docs/ponte-ble.md)
- [Deploy e Claude/MCP](docs/deploy.md)
- [Privacidade](docs/privacidade.md)

## Estado

- Central: testada (pytest), incluindo validação cruzada de RMSSD com NeuroKit2, fusão com estado
  conhecido, controles pareados que não acusam efeito falso, e o gate de números do relatório.
- Relógio: compila para o FR255 com o SDK Connect IQ 9.2 (definição de dispositivo tirada do banco de
  dispositivos da própria Garmin dentro do SDK); **ainda não rodou no relógio real** — veja a lista
  “medir no seu relógio” em cada README.
- Pulso não é dispositivo médico; é para entender padrões seus.
