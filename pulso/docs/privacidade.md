# Privacidade e cuidado com os dados

Dado de saúde é dado pessoal sensível (LGPD, art. 5º, II). Transcrição de reunião tem a voz e as
opiniões de **outras pessoas**. O Pulso foi desenhado para que isso fique sob o seu controle — mas
**`pulso notebook`, `pulso daemon` e `pulso serve` (os serviços `jobs` e `api` do Docker) enviam dados
sozinhos**, sem você rodar nenhum comando, quando as credenciais abaixo existem. Esta página diz
exatamente o quê.

## O que sai sozinho (`pulso notebook`, `pulso daemon`, `pulso serve`; serviços `jobs` e `api`)

Os jobs (`pulso daemon`, serviço `jobs`, e o `pulso notebook` sem `--no-jobs`) cuidam de tudo abaixo;
o servidor de ingestão (`pulso serve`, serviço `api`, também dentro do `pulso notebook`) só publica
alertas no Agenda.

“Nomes reconhecidos” são os de participantes e falantes que a central conhece (convidados da agenda e
falantes das transcrições): viram **Pessoa A, B…**. Um nome que só aparece no título do evento, ou um
convidado só com e-mail de uma palavra (como `carla@…`), **não** é reconhecido e sai como está. De um
convidado só com e-mail de duas ou mais palavras (`bruno.costa@…`), só o nome inteiro (“Bruno Costa”) vira
Pessoa; “Bruno” ou “Costa” sozinhos saem como estão.

| Destino | Quando | Condição | O que vai |
|---|---|---|---|
| **API do Claude** (Anthropic) | uma vez por dia, entre 06:30 e 12:00 (relatório de ontem) e, às segundas, o semanal | `ANTHROPIC_API_KEY` definida **ou** a pasta `~/.config/anthropic` existir (perfil do `ant auth login`, mesmo que criado para outra coisa) | os fatos do relatório: números já calculados (ativação, episódios, sono, HRV, reuniões, commits), datas, dias da semana, horários, títulos de eventos e etiquetas de trechos. Nomes reconhecidos viram **Pessoa A, B…** antes do envio (veja acima o que não é reconhecido). Nunca séries cruas nem texto de transcrição |
| **Google Drive** (3 Google Docs “Pulso · Saúde”, no seu Drive, não compartilhados) | os três Docs uma vez por dia, a partir das 06:45 (ou ao iniciar, se ainda não saíram hoje); fora isso, só o “Hoje”, ao iniciar e a cada 2 h, dia e noite | existir o login do Google (`~/.pulso/google-token.json`, no Docker `/data/google-token.json`, criado por `pulso export --login`) | resumo diário e semanal: ativação, episódios com o evento coincidente, sono, HRV, débito, próximos compromissos (hora e título), alertas das últimas 48 h, tabela dos últimos 7/60 dias, análise de reuniões e o último relatório. Nomes reconhecidos viram **Pessoa A, B…** em todo o documento (veja acima o que não é reconhecido). O Gemini Notebook e o app Gemini leem esses Docs se você os adicionar como fonte |
| **Google Agenda** (sua agenda principal) | na hora de cada alerta, por quem o disparou: os jobs (a cada 5 min) ou o servidor de ingestão (`pulso serve`, serviço `api`), quando um envio do relógio dispara o alerta | `PULSO_GCAL_ALERTS=1` **e** o login do Google | um evento privado por alerta: “⚠ Pulso: <texto do alerta>” e o detalhe. **Não é pseudonimizado**: o alerta de reunião traz o título da reunião como está na sua agenda |
| **Garmin Connect** | ao iniciar, a cada 6 h e, das 05:00 às 12:00, de hora em hora enquanto a noite passada não chegou | `GARMIN_EMAIL` definido ou tokens em `~/.garminconnect` (ou em `$PULSO_GARMIN_TOKENS`; no Docker, `/data/garminconnect`) | login na sua conta para **baixar** os seus dados; nada do Pulso é enviado |

Para não enviar nada sozinho: rode `pulso notebook --no-jobs` sem `PULSO_GCAL_ALERTS=1` (sem jobs não há
relatório, Docs nem Garmin; o alerta no Agenda também sai do servidor de ingestão), ou rode a central
sem `ANTHROPIC_API_KEY`/`~/.config/anthropic` (sem relatório automático) e sem o login do Google (sem
Docs nem Agenda; os resumos continuam só em `~/.pulso/resumos/`).

## O que sai só quando você aciona

| Comando | Destino | O que vai |
|---|---|---|
| `pulso report` | API do Claude | os mesmos fatos do relatório automático, com Pessoa A, B… |
| `pulso tag` | API do Claude | o **texto** dos trechos de transcrição; falantes viram `S1`, `S2`… e você vira `EU`, mas nomes citados no texto vão junto |
| `pulso export --drive` | Google Drive | os mesmos 3 Docs; com `--com-nomes`, **com os nomes reais** |
| Conversa com o Claude usando o MCP | a conversa no app do Claude | o que as ferramentas devolvem, **sem pseudônimos**: falantes com nome e até 300 caracteres por trecho de transcrição (`reuniao`), títulos de eventos, números |

## O que não sai

| Dado | Onde fica |
|---|---|
| Séries do relógio, RR, sono, agenda, commits, transcrições | SQLite da central (`~/.pulso/pulso.sqlite`) |
| FIT crus | `fit-archive/` da central |
| Resumos em Markdown | `~/.pulso/resumos/` |
| Mapa Pessoa A, B… → nomes | SQLite da central (artefato `pseudonyms`) |
| Tokens de aparelho | só o SHA-256, no SQLite |

Os dados do relógio chegam pelo celular (Garmin Connect Mobile) até a URL da central. No notebook,
essa URL é um túnel: no **Tailscale Funnel** o TLS termina na sua máquina; no **cloudflared** o TLS termina
na borda da Cloudflare, que vê as requisições decifradas. A ponte BLE (`--ble`) não passa por terceiros.

## Medidas no código

- **Pseudônimos estáveis** (`central/pulso/ai/names.py`): todo falante e participante conhecido vira
  Pessoa A, B…, a mesma letra no relatório e nos Docs, de uma execução para a outra. Vale para o nome
  inteiro, o primeiro nome e o sobrenome sozinho (de quem só tem e-mail, só o nome inteiro), também dentro de nomes de arquivo (`1-1-ana-souza`,
  `1-1-souza`) e com qualquer apóstrofo (D'Ávila, D’Ávila); no meio de uma frase, o sobrenome sozinho só
  conta com maiúscula (“dias” e “costa” também são palavras comuns), e o mesmo vale para nomes de duas
  letras (“Zé”, “Lu”). Duas pessoas com o mesmo primeiro
  nome não se confundem (“Ana” sozinha vira “Pessoa A/B”). Seus próprios nomes (`PULSO_ME`) ficam como
  estão, mas só o nome inteiro conta: coloque em `PULSO_ME` também o seu nome completo como a agenda e
  as transcrições o escrevem (`PULSO_ME=eu,davi,davi becam`). Um colega que só tem o mesmo primeiro nome
  (“Davi Costa”) vira Pessoa como qualquer outro; só o primeiro nome sozinho (“Davi”) fica, porque pode
  ser você. Sem o nome completo na lista, o seu próprio nome vira Pessoa também (nada vaza, só fica
  menos legível). O relatório volta com os nomes reais **só na sua máquina** e no relógio.
- **Nomes pseudonimizados no tagger**: antes de enviar, cada falante vira `S1`, `S2`… e você vira `EU`;
  o mapa fica local. O **texto** dos trechos ainda é enviado — se alguém citar nomes, eles vão junto.
- **Relatório só com fatos verificados**: o modelo recebe números e rótulos já calculados, nunca as séries
  cruas, e todo número (também por extenso, como “zero”, “onze”, “duzentos” ou “dois mil e quinhentos”),
  data, dia da semana (“segunda-feira” ou “segunda feira”) e horário (também “meio-dia” e “meia-noite”) da
  resposta precisa estar nos fatos que ela cita. Ficam de fora “um”/“uma” e “segunda” sem “feira” (também
  artigo e ordinal), frações, proporções, ordinais e coletivos por extenso (“meia hora”, “metade”, “o dobro”,
  “terceira noite”, “dúzia”, “dezena”, “centena”, “milhão”) e o “zero” de “o IC cruza o zero” (um veredito
  sobre o intervalo, não um valor; “incluindo zero episódios” continua sendo verificado).
- **Tokens com hash**: a central guarda só o SHA-256 dos tokens de aparelho.
- **Sem telemetria**: além do que está nas tabelas acima, nada é enviado para terceiros.

## Recomendações

1. **Combine com o time** antes de etiquetar transcrições de reuniões com outras pessoas, ou limite
   o `pulso tag` às reuniões em que isso foi acordado. Gravar e transcrever já pede consentimento;
   processar com IA externa é mais um passo.
2. **API do Claude (relatório, tagger)**: pela política comercial, entradas e saídas são apagadas em até
   30 dias e não treinam modelos sem permissão; conteúdo sinalizado por moderação pode ficar até 2 anos.
   Retenção zero (ZDR) é contrato via vendas e **não** cobre a Batches API (guarda 29 dias). Por isso o
   Pulso usa chamadas síncronas. O Opus 5.5 não é “Covered Model” (que exige 30 dias).
3. **Conversas no app do Claude usando o MCP**: o resultado das ferramentas vira conteúdo da conversa e
   segue a política de consumidor da sua conta — desligue o uso para treino nas configurações (ou use o
   modo anônimo) antes de consultar dados de saúde e de reunião.
4. **Google (Drive, Gemini, Agenda)**: os Docs e eventos ficam na sua conta e seguem a política dela; veja
   em `docs/gemini-notebook.md` o que o Gemini guarda e decida quanto detalhe de saúde quer lá.
5. Use disco criptografado no servidor e backup criptografado.
6. A biblioteca do Garmin Connect guarda tokens da sua conta em `~/.garminconnect` (no Docker,
   `/data/garminconnect`, no volume `pulso-data`, junto do banco) — trate como senha.
7. Os resultados são **para você** entender padrões, não diagnóstico. Sinais persistentes (palpitação,
   insônia crônica, estresse incapacitante) são assunto para médico.
