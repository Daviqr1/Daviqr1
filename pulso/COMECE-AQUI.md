# Comece aqui (no seu PC)

Tudo o que foi feito na sessão na nuvem está no branch `ccr-c10a1b69-63gl8d`. A central roda no seu
PC/notebook: é lá que ficam os dados (`~/.pulso/`), o relógio e o Claude Code.

## 1. Código e instalação (uma vez)

```bash
git clone https://github.com/Daviqr1/Daviqr1.git && cd Daviqr1     # ou: git fetch origin
git checkout ccr-c10a1b69-63gl8d
cd pulso
python3 -m venv .venv && . .venv/bin/activate                       # Python ≥ 3.12
pip install -e 'central[all,bridge]'
pulso inventory                                                      # cria ~/.pulso e mostra o banco vazio
```

Windows: `py -3.12 -m venv .venv` e `.venv\Scripts\activate`. Linux: a ponte BLE usa o BlueZ (já vem
nas distros comuns); o FR255 por USB aparece como MTP (`jmtpfs`/`gio mount`, libmtp ≥ 1.1.22).

## 2. Seus dados

| Fonte | Comando | Observação |
|---|---|---|
| Garmin Connect (site/API, sua conta) | `pulso import garmin --days 180` | retoma de onde parou; seja paciente (pausa entre dias); `--reload-cold` para > 6 meses |
| Export completo da conta (o zip que a Garmin manda por e-mail) | `pulso import garmin-export ~/Downloads/<arquivo>.zip` | anos de histórico; pode rodar depois do item acima — **não duplica** |
| FIT direto do relógio (USB) | `pulso import fit /caminho/GARMIN` | Monitor, Sleep, Activity, HRV… |
| Agenda | `pulso import ics agenda.ics --me voce@empresa.com` | |
| Transcrições | `pulso import transcript reunioes/*.vtt` | ancora pelo horário da agenda |
| Commits | `pulso import git ~/code/* --author "Seu Nome"` | |

Depois: `pulso analyze --days 60` (ou deixe o `pulso notebook` recalcular sozinho).

## 3. Central rodando

```bash
pulso token create fr255             # anote o token (aparece uma vez)
pulso notebook --ble --tunnel tailscale
```

Imprime a URL do túnel. Painel em `http://localhost:8080/painel`. Detalhes e horários dos jobs:
[docs/notebook.md](docs/notebook.md).

## 4. Relógio

Mostrador (`watch/face`) e Pulso Captura (`watch/capture`) — instruções de compilar/instalar em cada
README. Resumo: Connect IQ SDK (SDK Manager, dispositivo `fr255`), chave de desenvolvedor, `monkeyc`,
copiar o `.prg` para `GARMIN/APPS/` por USB. Instalado assim (sideload), o Garmin Connect **não** edita as
configurações: embuta URL do túnel + token no build (mostrador: `tools/bake_config.py`; captura:
`watch/capture/local/properties.xml`). Esses arquivos ficam fora do git — não commite token.

No Pulso Captura: menu → **Ponte BLE (notebook)** para mandar o acelerômetro bruto direto para o
`pulso notebook --ble` e, sem celular por perto, também os lotes.

## 5. Claude Code com os seus dados

Abra o Claude Code na pasta `pulso/` (app Claude Desktop, ou `claude remote-control` num terminal nessa
pasta, que aparece no app do Claude Code). O `.mcp.json` liga o servidor MCP do Pulso ao banco local; o
`CLAUDE.md` explica as ferramentas. Pergunte, por exemplo: “como foi minha semana de estresse?”.

## 6. Resumo no Gemini (opcional)

[docs/gemini-notebook.md](docs/gemini-notebook.md): `pulso export --login --drive` uma vez, adicione os 3
Docs ao notebook “Saúde (Pulso)”.

## 7. Primeiras medições no relógio real (ainda não testado em hardware)

1. Ponte BLE: frames/s no log do `pulso notebook --ble` e na tela de detalhes do Captura.
2. A conexão BLE convive com o Garmin Connect Mobile sem derrubar a sincronização?
3. `Sensor.getMaxSampleRate` real (a tela de detalhes mostra `acc max`).
4. Consumo de bateria por modo (o governador registra `gov` nos eventos).
5. Se o RR do pulso chega em repouso (contagem `rr=` no fim de cada captura).
