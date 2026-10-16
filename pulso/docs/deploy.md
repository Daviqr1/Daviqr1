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

O serviço `jobs` (`pulso daemon`) reanalisa o dia a cada 5 min, sincroniza o Garmin Connect a cada 6 h,
roda sono/reuniões às 04:30 e gera o relatório do dia anterior às 07:00 (segunda: semanal também).

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

## Primeiro uso, em ordem

```bash
pip install -e 'central[all]'                    # Python ≥ 3.12 (a garminconnect 0.3.x exige)
pulso token create fr255
pulso import garmin --days 30                    # histórico recente (GARMIN_EMAIL/PASSWORD no 1º login)
pulso import fit /caminho/GARMIN                 # se tiver o relógio no USB (modo MTP)
pulso import ics agenda.ics --me voce@empresa.com
pulso import git ~/code/* --author "Davi"
pulso import transcript reunioes/*.vtt           # ancora pela agenda; ou --start 2026-10-16T14:00
pulso analyze --days 30
pulso tag --days 30                              # etiqueta trechos das reuniões com o Claude
pulso report --days 7 --kind weekly
```

## Claude

- **Claude Desktop / Claude Code (local)**: MCP em stdio.

  ```json
  { "mcpServers": { "pulso": { "command": "pulso", "args": ["mcp"], "env": { "PULSO_DB": "/caminho/pulso.sqlite" } } } }
  ```

  Claude Code: `claude mcp add pulso -- pulso mcp`.
- **Claude Code remoto**: `claude mcp add --transport http pulso https://SEU_DOMINIO/mcp --header "Authorization: Bearer $PULSO_MCP_TOKEN"`.
- **claude.ai (conector personalizado)** exige OAuth no servidor MCP remoto; o Bearer estático deste
  projeto não basta. Para o app web, use o Claude Desktop/Code ou adicione OAuth (o SDK MCP 2.x tem
  `auth_server_provider`/`token_verifier` em `MCPServer`).

## Backup

Tudo está em um arquivo SQLite (`/data/pulso.sqlite`) + `/data/fit-archive/` (FIT crus — guarde para
sempre; a Garmin documenta mensagens novas com o tempo e dá para redecodificar). `sqlite3 pulso.sqlite
".backup bkp.sqlite"` funciona com o serviço rodando (WAL).
