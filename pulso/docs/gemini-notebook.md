# Resumo de saúde no Gemini Notebook (ex-NotebookLM) e no app Gemini

Estado verificado em outubro de 2026 (fontes no fim).

- O NotebookLM virou **Gemini Notebook** em julho de 2026 (`notebook.google.com`, links antigos redirecionam).
- **Google Docs importados do Drive sincronizam sozinhos** nas fontes do notebook (desde maio de 2026, contas
  pessoais incluídas; “a cada poucos minutos” ou ao abrir o notebook) — e há o botão “Click to sync with Google
  Drive”. Arquivo .md/PDF enviado é cópia congelada; Google Doc nativo é o caminho documentado.
- Os notebooks aparecem no **app Gemini** (barra lateral “Notebooks”, sincronia nos dois sentidos; Brasil e
  plano gratuito incluídos) e dá para anexar um notebook a qualquer conversa pelo **+**.
- **Não existe API** do Gemini Notebook para conta pessoal (a do Enterprise exige organização, ≥ 15 licenças e
  não aparece no app Gemini). Por isso o Pulso **escreve Google Docs** e você os adiciona ao notebook uma vez.
- Nenhum programa consegue mandar notificação dentro do Gemini. Para aviso no celular: a **Programação** do
  Gemini (resumo diário em horário fixo) e, em tempo real, o **relógio** e, opcionalmente, o **Google Agenda**.

## O que o Pulso publica

Três Google Docs, **sempre os mesmos arquivos** (sobrescritos no lugar — apagar e recriar tiraria a fonte do
notebook):

| Doc | Conteúdo | Tamanho |
|---|---|---|
| **Pulso · Saúde — Hoje** | `Atualizado em:`, linha `ALERTA: NENHUM / ATENÇÃO / ALTO — motivo`, hoje, última noite, próximos compromissos, alertas de 48 h, 7 dias, último relatório verificado | pequeno |
| **Pulso · Saúde — Últimos 60 dias** | uma seção `## AAAA-MM-DD` por dia (o Gemini cita o dia) | janela móvel |
| **Pulso · Saúde — Semanas e meses** | agregados por semana ISO e por mês + análise de reuniões de 90 dias | cresce devagar |

Só números calculados — sem séries cruas, sem texto de transcrição — e colegas como “Pessoa A, B…”
(`--com-nomes` desliga). Cópias locais em `~/.pulso/resumos/`.

Horários (no `pulso notebook`/`pulso daemon`): relatório 06:30, Docs 06:45, e o “Hoje” de novo a cada 2 h.

## Configuração (uma vez, ~10 min)

1. **Google Cloud** (console.cloud.google.com), com a sua conta pessoal: crie um projeto e ative a **Google Drive
   API** (e a **Google Calendar API** se quiser alertas no Agenda).
2. **Google Auth platform**:
   - *Branding*: nome do app e e-mail.
   - *Audience*: **External** (Internal exige organização) e **Publish app → In production**.
     ⚠️ Em “Testing” o token de renovação expira em **7 dias** e o job diário quebra.
   - *Data access*: só `.../auth/drive.file` (não sensível: o app só enxerga os arquivos que ele criou; sem
     verificação). Com alertas no Agenda, adicione `.../auth/calendar.events`.
   - *Clients*: **Create client → Desktop app**. Deixe desmarcado “Use this client for an AI-powered agent”.
     Baixe o JSON para `~/.pulso/google-client.json`.
3. No notebook:

   ```bash
   pip install -e 'central[gdrive]'
   pulso export --login --drive        # abre o navegador uma vez; cria os 3 Docs e imprime os links
   ```

4. **Gemini Notebook** (`notebook.google.com`) ou app Gemini → Notebooks → **Novo notebook** “Saúde (Pulso)”:
   - Adicionar fontes → **Drive** → os 3 Docs “Pulso · Saúde”.
   - Instruções do notebook (sugestão): *“Responda em português. Cite a data da seção (## AAAA-MM-DD) e diga o
     ‘Atualizado em’. Não faça diagnóstico. Se faltar dado, diga o que falta. z = desvios do meu normal.”*
   - **Não compartilhe** o notebook (compartilhado some do Gemini e pode aparecer no Modo IA da Busca).
5. **Atividade nos apps Gemini** (Keep Activity) precisa estar ligada para fontes do Drive, Workspace e
   Programações — leia o aviso de privacidade abaixo antes.

## Perguntar

- App Gemini → Notebooks → “Saúde (Pulso)”, ou numa conversa **+ → notebook**.
- Alternativa se o Gemini ignorar o notebook (há relatos em 2026): *“@Google Drive leia o documento ‘Pulso ·
  Saúde — Hoje’ e me diga…”*.
- Exemplos: “Qual reunião mais me desgastou este mês e quanto tempo levei para recuperar?”, “Durmo pior depois
  de commitar depois das 22h?”, “Compare esta semana com a anterior”.
- Para análise de verdade (correlação com defasagem, reunião trecho a trecho), o Claude Code com o MCP do Pulso
  vai mais fundo — o Gemini vê só o resumo.

## Alertas

| Canal | Quando | Como |
|---|---|---|
| **Relógio** (mostrador Pulso) | até 5 min | notificação nativa do relógio, uma vez por alerta (desliga em `alertNotify`) |
| **Google Agenda** (opcional) | tempo real | `PULSO_GCAL_ALERTS=1`: evento “⚠ Pulso: …” com lembrete pop-up no minuto 0, privado e sem bloquear a agenda |
| **Programação do Gemini** | resumo diário | crie no app Gemini (ex.: todo dia 08:00) com o prompt abaixo |

Prompt da Programação:

> Use o Google Drive: abra o documento “Pulso · Saúde — Hoje”. Se a linha ALERTA não for NENHUM, comece com ⚠ e
> explique em 3 linhas citando a data; senão, resuma o dia em 3 linhas. Diga a data de “Atualizado em”.

A Programação prepara a resposta na hora anterior à entrega, por isso os Docs saem às 06:45. Limite de 10
programações ativas; liberação gradual em contas pessoais — confira se aparece na sua.

## Privacidade

Com a Atividade dos apps Gemini ligada, conversas podem ser revisadas por pessoas e usadas para melhorar os
modelos do Google (revisadas ficam até 3 anos); desligada, ficam 72 h. Feedback (👍/👎) no Gemini Notebook envia
o conteúdo para revisão. Os Docs ficam no **seu** Drive, não compartilhados. Decida quanto detalhe de saúde
quer lá — o Pulso já manda só o resumo.

## Primeiro teste

1. `pulso export --drive` → mude algo, rode de novo → veja o “Atualizado em” mudar no notebook em minutos.
2. Pergunte no app Gemini dentro do notebook e confira se a resposta cita o “Atualizado em” correto.
3. Crie a Programação e veja se a notificação chega.

## Fontes

- Ajuda do Gemini Notebook: tipos de fonte e sincronização com o Drive — support.google.com/notebooklm/answer/16215270
- Workspace Updates, 2026-05-26: sincronização automática de fontes do Drive; 2026-07-16: NotebookLM → Gemini Notebook
- Ajuda do Gemini: apps conectados (Workspace) — support.google.com/gemini/answer/15229592; notebooks no Gemini — support.google.com/gemini/answer/16972047
- Drive API: importação/conversão e `files.update` — developers.google.com/workspace/drive/api
- OAuth: token de renovação de 7 dias em “Testing” — developers.google.com/identity/protocols/oauth2
