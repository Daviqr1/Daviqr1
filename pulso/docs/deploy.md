# Subindo a central

O relógio só consegue mandar dados para um endereço **HTTPS com certificado público**: quem faz a
requisição é o Garmin Connect Mobile no celular, e ele recusa certificado autoassinado. Três jeitos:

## 1. VPS ou servidor com domínio (recomendado)

```bash
cd pulso/deploy
cp .env.example .env            # PULSO_DOMAIN, ANTHROPIC_API_KEY, PULSO_MCP_TOKEN, ...
docker compose up -d --build
docker compose exec api pulso token create fr255     # cole este token nas settings dos apps do relógio
```

O Caddy emite o certificado (Let's Encrypt) para `PULSO_DOMAIN` e encaminha:

| caminho | serviço |
|---|---|
| `/v1/ingest`, `/v1/face`, `/health` | API de ingestão (`pulso serve`) |
| `/mcp` | MCP por HTTP (Bearer `PULSO_MCP_TOKEN`) |

O serviço `jobs` (`pulso daemon`):

| Quando | O quê |
|---|---|
| a cada 5 min | reanalisa o dia e avalia os alertas |
| a cada 6 h | sincroniza o Garmin Connect, últimos 2 dias (com credenciais ou tokens salvos); das 05:00 às 12:00, também de hora em hora (só o dia de hoje) enquanto a noite passada não chegou |
| uma vez por dia, a partir das 04:30 | sono/reuniões dos últimos 3 dias e de hoje; se a máquina estava desligada às 04:30, roda assim que ela volta |
| a partir das 04:30, enquanto a noite passada falta: a cada 15 min e logo após cada sincronização | resume a noite passada assim que o estagiamento chega e a noite fecha (registro de sono da Garmin, ou 90 min sem época nova); o mostrador e o alerta de sono da manhã leem esse resumo |
| uma vez por dia, 06:30 (até 12:00, se a máquina ligar depois) | relatório do dia anterior com o Claude, se houver `ANTHROPIC_API_KEY` (segunda: semanal também) |
| uma vez por dia, a partir das 06:45 | os três resumos (arquivos locais; Google Docs com o login Google); o “Hoje” sozinho de novo a cada 2 h |

Um job que falha fica registrado (artefato `job`, com o erro) e o laço continua, mesmo quando uma biblioteca
tenta encerrar o processo; só Ctrl+C / `docker compose stop` param o serviço. Reiniciado no mesmo dia, ele não
refaz o que já rodou bem (sono/reuniões, relatório, os três resumos).

Coolify: aponte para `pulso/deploy/docker-compose.yml`; o domínio pode ficar no proxy do Coolify em vez
do Caddy (remova o serviço `caddy` e exponha `api:8080` e `mcp:8765`).

## 2. Em casa, sem abrir porta: túnel

- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8080` (ou um túnel nomeado no seu
  domínio). Certificado público, sem porta aberta.
- **Tailscale Funnel**: `tailscale funnel 8080` — HTTPS público no domínio `*.ts.net`.

Rode `pulso serve` e `pulso daemon` localmente (ou o compose sem o `caddy`).

## 3. Só local (sem relógio online)

Dá para usar tudo sem servidor público: importe os FIT pelo USB (`pulso import fit /media/.../GARMIN`),
sincronize o Garmin Connect (`pulso import garmin --days 30`), use a ponte BLE na mesa
(`pulso bridge watch`) e converse com o Claude pelo MCP em stdio. Só o mostrador fica sem os números
da central.

## Seu histórico do Garmin Connect

Quanto mais histórico, melhores os modelos pessoais (normal por horário, FC adicional, HRV noturna,
controles pareados das reuniões). Dois caminhos, que se complementam:

1. **Export da conta (anos de histórico, recomendado)** — no site da Garmin: Conta → Gerenciamento de
   dados → *Exportar seus dados*. Chega por e-mail um zip com todos os FIT originais (monitoramento,
   sono, HRV, atividades) e resumos em JSON. Depois:

   ```bash
   pulso import garmin-export ~/Downloads/garmin-export.zip     # zips aninhados, idempotente
   ```

   Os FIT ficam guardados em `$PULSO_ARCHIVE/export/` (fonte da verdade; dá para redecodificar quando a
   Garmin documentar mensagens novas).
2. **Sincronização pela API não oficial (últimos meses, e dali em diante todo dia)**:

   ```bash
   pulso import garmin --days 180                 # retomável: dias já baixados são pulados
   pulso import garmin --since 2025-01-01 --pause 5
   pulso import garmin --days 365 --reload-cold   # pede recarga de dias em armazenamento frio
   ```

   A Garmin move o intradiário com mais de ~6 meses para armazenamento frio (a API devolve vazio). Com
   `--reload-cold` o Pulso pede a recarga (no máximo 20 por execução; a Garmin limita por dia) e baixa esses
   dias numa execução seguinte. Para anos, o export é mais rápido e completo.

Depois do backfill: `pulso analyze --since 2025-01-01` (≈ 1 s por dia com RR; menos sem RR).

## Primeiro uso, em ordem

```bash
pip install -e 'central[all]'                    # Python ≥ 3.12 (a garminconnect 0.3.x exige)
pulso token create fr255
pulso import garmin-export garmin-export.zip     # se já pediu o export da conta
pulso import garmin --days 30                    # histórico recente (GARMIN_EMAIL/PASSWORD no 1º login)
pulso import fit /caminho/GARMIN                 # se tiver o relógio no USB (modo MTP)
pulso import ics agenda.ics --me voce@empresa.com
pulso import git ~/code/* --author "Davi"
pulso import transcript reunioes/*.vtt           # ancora pela agenda; ou --start 2026-10-16T14:00
pulso analyze --days 30
pulso tag --days 30                              # etiqueta trechos das reuniões com o Claude
pulso report --days 7 --kind weekly
```

Agenda: reimporte o `.ics` exportado de novo para atualizar. Reuniões movidas, canceladas ou apagadas
saem do banco, mas só nos dias que o arquivo cobre (um export de uma semana não apaga o resto da
janela). Um convite avulso (`invite.ics`) mexe só no próprio evento.

## Claude

Modelos: relatório e etiquetagem usam `claude-opus-5-5` (relatório com esforço `high`/`xhigh`, etiquetagem
com `low`), com fallback de recusa no servidor ligado. Para baratear a etiquetagem em volume, dá para usar
`PULSO_TAGGER_MODEL=claude-haiku-5-5` (bem mais barato; sem fallback de recusa — trechos recusados ficam sem
etiqueta). A credencial vem de `ANTHROPIC_API_KEY` ou de um perfil `ant auth login`.

- **Claude Desktop / Claude Code (local)**: MCP em stdio.

  ```json
  { "mcpServers": { "pulso": { "command": "pulso", "args": ["mcp"], "env": { "PULSO_DB": "/caminho/pulso.sqlite" } } } }
  ```

  Claude Code: `claude mcp add pulso -- pulso mcp`.
- **Claude Code remoto**: `claude mcp add --transport http pulso https://SEU_DOMINIO/mcp --header "Authorization: Bearer $PULSO_MCP_TOKEN"`.
  Atrás de domínio público, o MCP 2.x exige o host na lista de permitidos (proteção contra DNS rebinding):
  o compose já passa `PULSO_DOMAIN`; fora dele, defina `PULSO_MCP_ALLOWED_HOSTS=seu.dominio`.
- **claude.ai (conector personalizado)**: o servidor precisa ser HTTPS público e usar OAuth com registro
  dinâmico de cliente (DCR) ou CIMD — ou ser sem autenticação, o que não faz sentido para dado de saúde.
  Cabeçalho estático está em beta só para algumas organizações (veja se aparece no seu “Add custom
  connector”). O Bearer deste projeto serve para Claude Code/Desktop; para o app web, o próximo passo é
  ligar o servidor de autorização embutido do SDK (`auth_server_provider` + `AuthSettings` em `MCPServer`).

## Backup

Tudo está em um arquivo SQLite (`/data/pulso.sqlite`) + `/data/fit-archive/` (FIT crus — guarde para
sempre; a Garmin documenta mensagens novas com o tempo e dá para redecodificar). `sqlite3 pulso.sqlite
".backup bkp.sqlite"` funciona com o serviço rodando (WAL).
