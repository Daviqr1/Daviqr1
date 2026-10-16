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
2. Para a API do Claude, verifique as configurações de retenção da sua organização na Anthropic
   Console (há opções de retenção reduzida/zero para contas elegíveis).
3. Use disco criptografado no servidor e backup criptografado.
4. A biblioteca do Garmin Connect guarda tokens da sua conta em `~/.garminconnect` — trate como senha.
5. Os resultados são **para você** entender padrões, não diagnóstico. Sinais persistentes (palpitação,
   insônia crônica, estresse incapacitante) são assunto para médico.
