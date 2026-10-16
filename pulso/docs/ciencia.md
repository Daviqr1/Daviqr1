# A ciência por trás de cada número

Regra do projeto: todo número responde “como foi medido?”. Abaixo, o método, a referência e onde ele
**não** vale.

## 1. Limpeza de RR (batimento a batimento)

**Método** — Lipponen & Tarvainen (2019), *J Med Eng Technol* 43(3):173–181 (o do Kubios). Série de
diferenças dRR e de desvios da mediana móvel (11 batimentos), cada uma normalizada por um limiar variável
(5,2 × desvio quartílico em 91 batimentos); classificação em ectópico, longo/curto, perdido e extra.
Correção: ectópico → batimento reposicionado no meio (preserva a linha do tempo); perdido → intervalo
dividido; extra → intervalos somados; longo/curto → mediana local. Antes: portão fisiológico 300–2000 ms
e segmentação onde faltam batimentos. Código: `signal/rr.py`.

Por que não dá para pular: num teste sintético da pesquisa, 0,5 % de artefatos (um batimento perdido,
um extra e um par ectópico em 600) **dobrou** o RMSSD (41,5 → 86,7 ms); a correção trouxe para 42,0 ms.

**Onde não vale** — RR do pulso (PPI óptico) em movimento. Parado e dormindo é aceitável, com viés:
Polar Vantage V2 vs. cinta H10 deu viés de ln RMSSD de 0,17–0,19 e ICC ≈ 0,92 (Nuuttila et al. 2022,
*Sensors*), superestimando quem tem HRV baixa; HRV noturna do Garmin Fenix 6 teve MAPE ≈ 10,5 % contra
ECG. Não há validação publicada do FR255 — leia como **tendência sua**, não valor absoluto. Em movimento o
erro sobe (Bent et al. 2020, *npj Digit Med*). Por isso:

- toda janela carrega `quality` (fração de batimentos não corrigidos) e `coverage`; qualidade < 85 % sai;
- **só janelas paradas viram sinal de estresse**: ENMO médio da janela < 45 mG (corte sedentário de
  Bakrania et al. 2016) ou, sem acelerômetro, passos/min ≤ 5;
- a cinta (`ble:strap`) tem prioridade sobre o pulso. Vale fazer uma autovalidação: 10 min deitado + uma
  noite com cinta e relógio juntos mostram o viés do **seu** pulso.

## 2. HRV

| Métrica | Janela | Uso | Cuidado |
|---|---|---|---|
| RMSSD, ln RMSSD | 2 min (passo 1 min); 5 min à noite | atividade vagal, estresse agudo | a métrica mais robusta em janelas curtas |
| SDNN, pNN50, SD1/SD2 | idem | complementares | SDNN depende muito da duração; SD1 = RMSSD/√2 (não é informação nova) |
| HF (Lomb-Scargle, sem reamostrar) | ≥ 1 min | HF ~ respiração | depende do ritmo respiratório |
| LF, LF/HF | ≥ 2,5 min (senão vazio) | — | **LF/HF não é “equilíbrio simpatovagal”** (Billman 2013, *Front Physiol* 4:26) — reportado, nunca usado como estresse |
| Respiração pela RSA | pico da banda HF | frequência respiratória sem sensor | só quando a banda HF está limpa |
| DFA α1 (escalas 4–16) | ~2 min / ≥ 50 batimentos | intensidade de exercício | Rogers et al. 2021 (*Front Physiol* 11:596567): ≈ 0,75 limiar aeróbio, ≈ 0,5 anaeróbio; precisa de RR de qualidade (cinta) |

Janelas: 2 min com passo de 1 min (ln RMSSD em 60 s já concorda com 5 min, ICC 0,98 — Esco & Flatt 2014;
10 s de ECG já dão RMSSD válido — Munoz et al. 2015). Normalização da PSD validada em teste: uma senoide de
amplitude A integra para A²/2 na banda. Validação cruzada com NeuroKit2 (`test_matches_neurokit2_rmssd_if_available`;
atenção: o DFA α1 padrão do NeuroKit2 usa caixas 4–11, não as 4–16 da literatura de limiares).

## 3. Separar estresse mental de esforço físico: FC adicional

**Método** — Myrtek et al. (2005, *Int J Psychophysiol* 56:27–37; Myrtek 2004, *Heart and Emotion*):
a “frequência cardíaca adicional” é a parte da FC que a atividade física não explica, usada para medir
ativação emocional na vida real. A regra original de Myrtek compara cada minuto com os 3 anteriores e
marca evento quando a FC sobe > 3 bpm sem a atividade subir; aqui usamos uma **variante por regressão**,
que aproveita todo o histórico. Brouwer et al. (2018, *Front Hum Neurosci* 12:284) concluem que a evidência
ligando FC não metabólica a emoção ainda é limitada — por isso ela é um canal entre outros, não o veredito.
Implementação: regressão **quantílica** (q = 0,3) da FC por minuto em
função de médias móveis exponenciais do log dos passos/min (τ = 1, 3 e 10 min — cinética de subida,
recuperação e deriva lenta) + harmônicos circadianos de 24 h e 12 h, ajustada só em minutos acordados e
**só no histórico anterior** (14 dias). FC adicional = FC − previsão. Código: `models/additional_hr.py`.

**Por que quantílica** — a média seria puxada pelos próprios momentos de estresse; o quantil baixo
estima “você relaxado fazendo isso, nesse horário”.

**Onde não vale** — cafeína, calor, febre, desidratação e álcool também sobem a FC sem movimento: o
modelo não sabe a causa, só que não foi movimento. Marcações manuais (“café”) entram como eventos para
você separar isso.

## 3b. HRV não metabólica

Verkuil et al. 2016 / Brown et al. 2020 (*Front Neurosci* 14:564123): por pessoa, regressão do RMSSD
contra a aceleração; episódio quando o RMSSD fica > 2 EP abaixo do previsto por ≥ 7,5 min (exploratório,
n = 6, mas o modelo publicado mais próximo do nosso caso). Aqui: ln RMSSD ~ log(1+ENMO) + harmônicos de 24 h,
regressão pela mediana em janelas paradas do histórico; o resíduo padronizado é o canal `rr_hrv` da fusão.
Código: `models/nonmetabolic_hrv.py`.

## 4. Fusão: ativação latente

**Método** — filtro de Kalman 1-D com estado de Ornstein-Uhlenbeck (decai ao normal com τ = 20 min,
variância estacionária 1) observado por canais padronizados: FC adicional / escala robusta, estresse da
Garmin padronizado pelo seu histórico, −ln RMSSD padronizado. Cada canal tem ruído próprio **estimado dos
dados** (passos EM sobre os resíduos suavizados); minutos sem um canal simplesmente não atualizam com ele.
Suavizador RTS para o passado, filtro causal para “agora”. Saída: z ± σ e a parcela de cada canal na
atualização. Código: `models/fusion.py`.

Validado em teste: recupera o estado com correlação > 0,9, ordena corretamente o ruído dos canais e
erra menos que qualquer canal sozinho. Filtros de estado para ativação simpática existem na literatura
(Wickramasuriya & Faghih 2020, *PLoS ONE*: condutância da pele + FC; Barbieri et al. 2005: FC como processo
pontual), mas **esta combinação de canais é desenho nosso**, não um método validado — o relatório trata
como indicador, e as suas marcações de “estresse” no relógio servem de validação (fato “X de Y marcações
caíram em episódios”).

**Leitura** — z é “desvios do seu normal”: 1 z perceptível, 2 z forte. Não é escala absoluta de estresse
e não se compara entre pessoas.

## 5. Episódios (os “picos”)

Histerese: abre acima de 1,5 z por ≥ 3 min, fecha abaixo de 0,5 z, junta episódios a < 5 min. Cada
episódio é classificado por movimento (passos/min > 60 em > 60 % do tempo = físico; 20–60 % = misto;
senão mental) e ligado a reuniões, commits e marcações com folga de 10 min (antecipação e recuperação).
Mudanças de regime no dia: PELT (Killick et al. 2012, *JASA* 107:1590) via `ruptures`.

## 6. Reuniões: é a reunião ou é o horário?

1. **Resposta por evento** — basal (−45 a −15 min), antecipação (−15 a 0), durante, pico, área e tempo de
   recuperação (até voltar a basal + 0,25 z, limite 60 min).
2. **Controles pareados** — para cada reunião, janelas no mesmo horário e duração, em dias do mesmo tipo
   (útil/fim de semana), sem compromisso ±15 min, até 6 semanas em volta. Diferença média com teste de
   permutação por troca de sinal (10 000) e IC 95 % por bootstrap. Validado em teste: detecta efeito real
   e **não** acusa efeito quando a reunião só cai num horário que já é tenso.
3. **Dentro da reunião** — trechos da transcrição alinhados à fisiologia com defasagem estimada por
   correlação cruzada (padrão 20 s), corrigidos pela mediana da própria reunião, agregados por falante e
   por tema (etiquetas do Claude) com IC por bootstrap; grupos com < 3 trechos não aparecem.

4. **Reuniões coladas vs. com pausa** — o piloto de EEG da Microsoft (2021, n = 14, não revisado por pares)
   viu a atividade ligada a estresse se acumular em 4 reuniões seguidas e não acumular com pausas de 10 min.
   O Pulso compara o **nível** de ativação (sem correção pelo basal, que numa reunião colada seria a reunião
   anterior) entre reuniões com < 10 min de pausa antes e as demais, com teste de permutação.

**Onde não vale** — a evidência sobre fisiologia em reuniões é de pilotos (MIT, n = 16 em 26 reuniões;
Microsoft, n = 14). Poucas reuniões dão IC largo (o relatório diz isso). Associação não prova causa: a
reunião difícil pode ser difícil porque o projeto já estava atrasado.

## 7. Sono e prontidão

- Noites a partir do estagiamento (Garmin) — durações pelos intervalos entre transições. Estagiamento por
  pulso é fraco: ~90 % das épocas certas em sono/vigília mas só ~60 % da vigília, e ~72 % em 3 classes
  (Walch et al. 2019, *SLEEP*). Por isso o Pulso usa o sono para **horário, duração, regularidade, FC e HRV
  noturnas**, e não constrói conclusões em cima de minutos de REM.
- **Nadir de FC** (mediana móvel de 5 amostras) e **quando** ele acontece: nadir tardio costuma acompanhar
  refeição tarde, álcool ou noite que não desacelerou.
- HRV noturna: mediana do RMSSD de janelas de 5 min dos seus RR, senão as leituras de 5 min da Garmin.
- **Sleep Regularity Index** (Phillips et al. 2017, *Sci Rep* 7:3216).
- **Prontidão por HRV** no estilo Plews et al. (2013, *Sports Med* 43:773) / Kiviniemi et al. (2007):
  média de 7 dias do ln RMSSD noturno contra a faixa normal de 60 dias (média ± 0,5 DP, a menor mudança
  relevante) e o CV de 7 dias; só depois de 14 noites.
- Débito de sono: soma móvel de 14 dias de (necessidade − sono), necessidade padrão 7,5 h; noite sem
  dado não conta como noite sem sono.

## 8. Treino

TRIMP (Banister), fitness–fadiga (τ 42 e 7 dias). **Sem ACWR**: Impellizzeri et al. (2020, *IJSPP*
15:907) mostraram que a razão é artefato estatístico. Limiares por DFA α1 em corridas incrementais com
cinta (FC em α1 = 0,75 e 0,5).

## 9. IA: onde o Claude entra e onde não entra

| Entra | Não entra |
|---|---|
| Etiquetar trechos de reunião (tema, pressão, conflito, dirigido a você) com lista fixa | Calcular qualquer número |
| Escrever o relatório a partir de **fatos numerados** | Ver séries cruas |
| Conversar com você sobre os dados pelo MCP (consultas e cruzamentos) | Diagnosticar |

Todo número e horário do relatório é conferido contra os fatos que o próprio insight cita; insight que
falha é descartado e listado (`ai/facts.py`). É o mesmo princípio de um *gate* determinístico antialucinação.

### E os “modelos de fundação” de sensores?

Existem e são impressionantes, mas não servem hoje para uma pessoa com um FR255:

- Google LSM (2024/25), LSM-2 (2025), SensorFM (2026) e o modelo comportamental da Apple (2025): treinados
  em dezenas de milhões de horas, **sem pesos ou API públicos** (o do LSM-2 não foi confirmado).
- Modelos abertos de PPG (PaPaGei) pedem a **forma de onda bruta**, que o Connect IQ não expõe.
- OpenMHC (2026) diz ter código e pesos abertos — é o único candidato plausível a complemento, depois de
  conferir se a entrada dele aceita dados de minuto do FR255.

Para uma pessoa só, com séries de minutos e RR por janelas, modelos estatísticos interpretáveis e ajustados
a **você** ganham — e explicam o porquê. O Claude entra em cima deles, explicando e cruzando.
