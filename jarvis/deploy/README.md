# Jarvis no Orange Pi 4 Pro

Alvo: Ubuntu 22.04 aarch64 (Python 3.10), 4 GB de RAM, sem interface gráfica.
Tudo que você precisa depois da instalação é um navegador ou o app na mesma
rede Tailscale. Não precisa de SSH para usar.

Os caminhos abaixo supõem o usuário `orangepi` e o clone em
`/home/orangepi/automaclips`. Se for outro, troque nos dois lugares: aqui e em
`jarvis.service`.

## 1. Pacotes do sistema

```bash
sudo apt update
sudo apt install -y python3.10-venv git
sudo timedatectl set-timezone America/Sao_Paulo   # os horários de lembrete dependem disso
```

## 2. Código e ambiente Python

```bash
cd /home/orangepi
git clone https://github.com/joaonunex123-dotcom/automaclips.git
cd automaclips
git checkout ccr-48119767-bd2b3f                  # a branch onde está o Jarvis
cd jarvis

python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

## 3. Configuração (.env)

```bash
cp .env.example .env
echo "JARVIS_TOKEN=$(openssl rand -hex 32)" >> .env   # ou edite a linha vazia
nano .env
```

Anote o token. Ele é a senha de todas as rotas, e a tela web pede esse valor
na primeira abertura.

Com o llama-server ainda não instalado, deixe `JARVIS_LLM=mock`. Quando o
llama-server estiver rodando, mude para `JARVIS_LLM=llamacpp` e confira
`LLAMA_URL`.

## 4. Teste manual antes do serviço

```bash
cd /home/orangepi/automaclips/jarvis
.venv/bin/python -m backend
```

Em outro terminal (ou de outro aparelho na Tailscale):

```bash
T=$(grep ^JARVIS_TOKEN= .env | cut -d= -f2-)
curl -H "X-Jarvis-Token: $T" http://localhost:8000/status
```

Se voltar um JSON com `temperatura_c`, `ram` e `mascote`, está no ar. Pare com
Ctrl+C e siga para o serviço.

## 5. Como serviço (systemd)

```bash
sudo cp /home/orangepi/automaclips/jarvis/deploy/jarvis.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jarvis
systemctl status jarvis --no-pager
```

Logs ao vivo: `journalctl -u jarvis -f`. Reiniciar depois de mudar o `.env`:
`sudo systemctl restart jarvis`.

O serviço sobe sozinho após reboot (`enable`) e reinicia se cair
(`Restart=on-failure`).

## 6. Acesso pela Tailscale

O serviço escuta em `0.0.0.0:8000`. De qualquer aparelho na mesma tailnet:

- Tela web: `http://<nome-do-pi>:8000/` (ou o IP 100.x da Tailscale).
- API: a mesma base, com o cabeçalho `X-Jarvis-Token`.

A faixa 100.64.0.0/10 e os nomes `*.ts.net` já estão liberados no CORS. Quem
não está na tailnet não chega ao IP.

**Atenção:** `0.0.0.0` também expõe o serviço na rede local de casa. A proteção
nesse caso é só o token. Para restringir à Tailscale, troque `--host` no
`jarvis.service` pelo IP 100.x do Pi (`tailscale ip -4`), ou libere a porta só na
interface da Tailscale com `sudo ufw allow in on tailscale0 to any port 8000`
e não libere na outra.

## 7. llama-server (à parte)

O Jarvis não inicia o modelo. Rode o llama-server numa porta local, por
exemplo:

```bash
llama-server -m /caminho/para/modelo.gguf --host 127.0.0.1 --port 8080 -c 2048
```

Depois, no `.env` do Jarvis: `JARVIS_LLM=llamacpp` e `LLAMA_URL=http://127.0.0.1:8080`,
e `sudo systemctl restart jarvis`. Um serviço systemd próprio para o
llama-server ainda não existe (ver `docs/pendencias.md`).

## Problemas comuns

- **`erro de configuração: JARVIS_TOKEN ausente`**: o `.env` não tem o token,
  ou o arquivo não está em `jarvis/`.
- **401 em tudo**: o token do cliente não bate com o do `.env`. Não há
  recuperação: troque no `.env` e reinicie.
- **`temperatura_c: null`**: `/sys/class/thermal` não tem zonas no sistema.
  Confira com `ls /sys/class/thermal/`. Ajuste `JARVIS_THERMAL_DIR` se o caminho
  for outro.
- **Lembrete que não disparou**: confira `timedatectl` (fuso) e o horário
  sem fuso no `.env`. Horário sem fuso na API é lido como `JARVIS_TZ`.
- **Chat responde com erro de llama-server**: o `llama-server` não está de
  pé ou a `LLAMA_URL` está errada. `curl http://127.0.0.1:8080/health`.
