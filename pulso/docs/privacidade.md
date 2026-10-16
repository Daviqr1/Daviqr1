# Privacidade e cuidado com os dados

Dado de saúde é dado pessoal sensível (LGPD, art. 5º, II). Transcrição de reunião tem a voz e as
opiniões de **outras pessoas**. O Pulso foi desenhado para que isso fique sob o seu controle:

## O que fica onde

| Dado | Onde fica | Sai da sua máquina/servidor? |
|---|---|---|
| Séries do relógio, RR, sono, agenda, commits | SQLite da central | não |
| FIT crus | `fit-archive/` da central | não |
| Fatos agregados para o relatório (números e títulos de eventos) | enviados à API do Claude | sim, só quando você roda `pulso report` |
| Trechos de transcrição | enviados à API do Claude | sim, só quando você roda `pulso tag` |
| Dados que o Claude consulta via MCP | respostas das ferramentas, na conversa | sim, na sessão em que você usar |

## Medidas no código

- **Nomes pseudonimizados no tagger**: antes de enviar, cada falante vira `S1`, `S2`… e você vira `EU`;
  o mapa fica local. O **texto** dos trechos ainda é enviado — se alguém citar nomes, eles vão junto.
- **Relatório só com fatos**: o modelo recebe números e rótulos já calculados, nunca as séries cruas.
- **Tokens com hash**: a central guarda só o SHA-256 dos tokens de aparelho.
- **Sem telemetria**: nada é enviado para terceiros além do que você aciona.

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
4. Use disco criptografado no servidor e backup criptografado.
5. A biblioteca do Garmin Connect guarda tokens da sua conta em `~/.garminconnect` — trate como senha.
6. Os resultados são **para você** entender padrões, não diagnóstico. Sinais persistentes (palpitação,
   insônia crônica, estresse incapacitante) são assunto para médico.
