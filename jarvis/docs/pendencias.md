# Pendências

Ordem aproximada de prioridade. O que está marcado como **bloqueia** impede
o uso real no Pi.

## Antes de usar no Pi

- **Validar em aarch64 com Python 3.10.** Os testes rodaram em 3.10.20 no
  x86_64. Falta confirmar que os wheels de `psutil`, `websockets` e
  `pydantic-core` instalam no Pi sem compilar. Se compilar, o `apt` precisa de
  `python3-dev` e `build-essential`.
- **Kiosk no Pi.** `deploy/kiosk.sh` e `jarvis-kiosk.service` nunca rodaram num
  Pi de verdade. No Ubuntu 22.04, `chromium-browser` é pacote snap: validar se
  ele roda sob o `cage`, ou usar o binário do Debian. Conferir também se as
  fontes (DejaVu) estão instaladas.
- **Ler a temperatura real.** Conferir quais zonas existem em
  `/sys/class/thermal/` no Orange Pi e se o `max` entre elas é a leitura que
  interessa. Se não for, filtrar por `type`.
- **Rodar o serviço de verdade no Pi** (`deploy/README.md`, passos 1 a 5) e
  confirmar que sobe após reboot.
- **llama-server com modelo real.** Medir `tokens_por_s` e a RAM com o modelo
  carregado mais o Jarvis. Com 4 GB, o modelo cabe? Ainda não sabemos.
- **Serviço systemd para o llama-server.** Hoje ele sobe à mão. Precisa de
  unit própria, com a mesma lógica de restart.
- **Health check do provedor.** Se o llama-server cair, o chat só responde com
  erro. Não há um `/status` que mostre o modelo como indisponível.

## Funcionalidades que faltam

- **Tela 800×480: falta ver no display real.** As cinco telas, o mascote e o chat
  estão prontos e testados no navegador (`tests/e2e_tela.js`, Playwright). Falta
  conferir no display de 5": legibilidade a ~40 cm, e o kiosk.
- **Segurar para falar.** A tecla 1 só abre o campo de texto. Gravação, transcrição
  e resposta em voz não existem ainda.
- **App Flutter.** Não começado. O contrato está em `jarvis/README.md`
  (`/ws/chat`, `/events` e os endpoints REST).
- **GPIO.** Ligar os pinos e chamar `backend/entrada.py:registrar_tecla`.
  Falta decidir o que cada tecla faz e tratar debounce.
- **Cancelar a geração.** Hoje, uma resposta longa só para se o cliente cair.
  Falta uma mensagem `{"cancelar": true}` no WS.
- **Múltiplos chats.** Existe um histórico único global. Se o app quiser
  conversas separadas, a tabela `mensagens` ganha uma coluna de sessão.
- **Lembretes recorrentes e soneca.** Não existem. Hoje cada lembrete dispara
  uma vez.
- **Agenda com duração e filtro por período.** Hoje há só `quando`, sem fim, e a
  listagem é tudo.

## Qualidade e operação

- **Bind em 0.0.0.0 por padrão.** O serviço também responde na LAN de casa; a
  proteção é só o token. Ver `deploy/README.md`, seção 6, para restringir à
  Tailscale.
- **Contagem de tokens aproximada.** Cada pedaço do stream conta como um token.
  Se o llama-server devolver `usage` no stream, usar o número dele.
- **Crescimento do banco.** O histórico do chat não tem limpeza. No Pi isso
  demora anos, mas vale um comando de purga antes de virar problema.
- **Token na query string.** `/events` e `/ws/chat` aceitam `?token=`, e ele
  vai parar no log de acesso do uvicorn (`journalctl -u jarvis`). Para mitigar:
  trocar por um ticket de uso único, ou por cookie definido num login.
- **Token em `screen/config.js`.** O arquivo é servido sem autenticação pela porta
  8000, na LAN também. Se o token real for escrito ali, qualquer um na rede o lê.
  Deixe vazio no git; no Pi, digite o token uma vez na tela (fica no perfil do
  Chromium). Alternativa melhor: o backend entregar o token pronto só a quem tem
  o cabeçalho.
- **Sem rotação do token.** Trocar exige editar o `.env` e reiniciar. Não há
  rotação por tempo.
- **Teste de carga não feito.** Não sabemos quantos clientes SSE o Pi aguenta
  com o modelo rodando ao lado.
- **Aviso de deprecação no teste.** O `TestClient` do Starlette avisa que vai
  pedir `httpx2` no futuro. Ainda funciona com o `httpx` atual; quando o aviso
  virar erro, migrar.
- **Fuso do sistema.** O Pi precisa estar em `America/Sao_Paulo`
  (`timedatectl`). Se o `JARVIS_TZ` e o sistema divergirem, os horários
  na saída ficam certos, mas o `systemd` e o `journalctl` mostram outro.

## Decidido, mas merece revisão

- **Jarvis dentro do repositório `automaclips`.** Funciona, mas o certo a longo
  prazo é um repositório próprio. Ver `docs/decisoes.md`, seção 1.
- **Commits.** Os commits do Jarvis seguem na branch `ccr-48119767-bd2b3f`, que
  não é a `main`. Abrir PR quando for a hora.
