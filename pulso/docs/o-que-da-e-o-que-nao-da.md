# O que dá e o que não dá no Forerunner 255

Gabi, a pergunta original foi “vasculhar o SO, mudar o kernel, atualizar as libs dos sensores”. A resposta
curta: **o firmware não é nosso para mexer, e nem precisaria** — o que trava a precisão não é o algoritmo
rodando no relógio, é *o que a Garmin deixa sair dele*. Então o Pulso faz o contrário: tira do relógio
tudo o que é permitido tirar, no máximo de resolução, e põe o processamento pesado onde há CPU de verdade.

## O relógio por dentro

| | |
|---|---|
| Sistema | firmware proprietário da Garmin; apps de terceiros rodam numa **VM (Connect IQ / Monkey C) isolada** |
| Nível de API | Connect IQ **5.2** (firmware 29.x, ainda recebe atualização) |
| Tela | 260×260 redonda, MIP transflectiva, **64 cores** (2 bits por canal), sem toque |
| Sensores | óptico Elevate v4, acelerômetro, giroscópio, bússola, barômetro, termômetro, Pulse Ox, GNSS multibanda |
| Memória por tipo de app | mostrador 128 KB · app 512 KB · background 64 KB · campo de dados 256 KB |
| Bateria (Garmin) | smartwatch até 14 dias · GPS 30 h · todos os sistemas 25 h · multibanda 16 h |

## O que **não** dá (e por quê)

| Pedido | Realidade |
|---|---|
| Modificar SO, kernel, firmware ou as bibliotecas dos sensores | Não há caminho suportado. O firmware é distribuído e instalado só pela Garmin; tentar substituir arrisca brickar o relógio e perder garantia. **Não vamos por aí.** |
| Ler a forma de onda bruta do sensor óptico (PPG) | Nenhuma API expõe. Não existe no SDK nem na nuvem para indivíduos. |
| RR batimento a batimento 24/7 e acelerômetro 24/7 vindos da Garmin | Só para parceiros comerciais (Health SDK / Health API); o “Enhanced BBI” tem licença paga. Indivíduos não têm acesso, e o programa está fechado a novos pedidos desde 2026. |
| Capturar sensores brutos 24/7 com app próprio | Sem multitarefa no FR255: o app de captura só lê sensores **enquanto está aberto**. O background roda no máximo 30 s a cada 5 min. |
| HRV Status, prontidão, sono: o algoritmo interno | Só os *resultados* saem (arquivos FIT / nuvem). Training Readiness não existe no FR255. |
| “Aumentar a resolução” da tela | É física: 260×260 e 64 cores. |
| Ler sensores no mostrador | O mostrador não acessa sensores nem rede em primeiro plano; só pelo serviço de background. |
| Mandar tudo em tempo real pelo celular | O link Connect IQ ↔ celular dá ~400–800 B/s. RR cabe (~200 KB/dia); acelerômetro 25 Hz bruto (~13 MB/dia) não. |

## O que **dá** — e o Pulso usa

| Camada | O que sai | Resolução | Componente |
|---|---|---|---|
| Arquivos do próprio relógio (USB/MTP, Gadgetbridge ou zip diário da nuvem) | FC, passos, intensidade, estresse (3 min), Body Battery, respiração, SpO2, estágios do sono, HRV noturna de 5 min, atividades | 1–3 min; 1 s nas atividades | `pulso import fit` / `import garmin` |
| App de captura em primeiro plano | **RR do pulso**, acelerômetro até a taxa máxima que o relógio reportar (25 Hz típico; a API vai a 100 Hz em alguns), giroscópio se existir, barômetro bruto, FC 1 Hz | batimento a batimento / 25 Hz | **Pulso Captura** |
| FIT da sessão do app | IMU bruta pelo `SensorLogger` + campos próprios (RR, ENMO, α1) | taxa cheia | Pulso Captura + `import fit` |
| Background do mostrador (a cada 5 min) | histórico do relógio (FC, estresse, Body Battery, SpO2, temperatura, pressão), estresse instantâneo (média de 30 s), respiração, passos, bateria | 2–5 min | **Pulso** (mostrador) |
| Complicações do sistema no mostrador | Training Status, tempo de recuperação, VO2max, previsões de prova, respiração, Pulse Ox, próximo evento da agenda | ao vivo | Pulso (mostrador) |
| Cinta no notebook | RR de qualidade ECG | batimento a batimento | `pulso bridge hrm` |
| Ponte BLE na mesa | RR + movimento direto no notebook, sem celular | 20 bytes por escrita | `pulso bridge watch` |

## As “melhorias escondidas” que valem a pena

Nenhuma exige hack — são ajustes e recursos pouco usados:

1. **Modo USB = MTP** (Sistema → Modo USB): no Linux o relógio vira um dispositivo MTP (`jmtpfs`/`gio`) e
   a pasta `GARMIN/` expõe Monitor, Sleep, HRVStatus, Metrics e Activity em FIT. É a fonte mais fiel.
2. **Gravação de dados “A cada segundo”** (Sistema → Gravação de dados) para atividades.
3. **“Registrar VFC” / “Log HRV”**: existe em irmãos (FR955/965/570); o manual do 255 (v12, jun/2025) não
   cita, e só um relato de fórum diz que aparece — **confira no seu** (Sistema → Gravação de dados). Se existir, toda atividade grava RR no FIT (mensagem `hrv`),
   e com cinta isso vira HRV de laboratório nos treinos. O importador já lê.
4. **Health Snapshot**: 2 minutos parado medindo FC, estresse, respiração, SpO2, RMSSD. Use como sonda
   padronizada antes e depois de reuniões difíceis.
5. **Pulse Ox só durante o sono** (não o dia todo): o dia todo custa muita bateria e, segundo relatos, apps
   que ligam o SpO2 podem deixar o ajuste em “o dia todo” — confira depois de usar o modo MAX do app.
6. **Transmitir FC** (BLE): o notebook recebe sua FC na mesa sem nenhum código no relógio (provavelmente
   sem RR — `pulso bridge hrm --probe` confirma).
7. **Horário de sono no perfil** coerente com a vida real: melhora a detecção de sono e a janela de HRV.
8. **Cinta peitoral pareada** (HRM-Pro, Polar H10) para treinos: RR de verdade, DFA α1 confiável.
9. **Programa beta da Garmin**: se o FR255 estiver no programa público de beta no momento, ele traz
   firmware (e atualização do hub de sensores) antes. A atualização do sensor óptico vem sempre dentro do firmware do relógio — não dá para
   escolher versão de algoritmo.

## O que o processamento “violento” faz que o relógio não faz

O relógio calcula estresse com HRV a cada 3 min e **desliga a medida quando você se mexe**. A central:

- limpa RR com o algoritmo do Kubios (Lipponen & Tarvainen 2019) e calcula RMSSD, SDNN, espectro
  (Lomb-Scargle), respiração pela arritmia sinusal e DFA α1;
- separa **estresse mental de esforço físico** com o modelo de FC adicional (Myrtek): o quanto o coração
  bate acima do que o seu movimento e o horário explicam;
- funde FC adicional, estresse da Garmin e a sua HRV num **estado latente com filtro de Kalman** (cada
  sensor com o seu ruído estimado dos dados), com incerteza;
- compara cada reunião com **os mesmos horários sem reunião** (teste de permutação) para não confundir
  “reunião estressa” com “16h sempre estressa”;
- alinha trechos de transcrição com a fisiologia (com defasagem estimada) e mede o efeito por falante
  e por tema;
- cruza sono, HRV noturna, commits tarde da noite, agenda e marcações num quadro diário com correlação
  com defasagem;
- e o Claude escreve o relatório **só com números verificados** — o que não bate com os fatos é descartado.

Detalhes e referências: [ciencia.md](ciencia.md).
