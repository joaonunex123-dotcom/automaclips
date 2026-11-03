# Jarvis

Assistente de mesa com IA local, rodando num Orange Pi 4 Pro. Este diretório é
o backend: FastAPI + SQLite, com dois clientes (um app Flutter de chat e uma
tela web 800×480). Para subir no Pi, ver [deploy/README.md](deploy/README.md).
Decisões e pendências estão em [docs/](docs/).

## Estrutura

```
backend/         o serviço (FastAPI)
  main.py        fábrica do app (criar_app)
  config.py      .env e variáveis de ambiente
  banco.py       SQLite: notas, agenda, lembretes, histórico
  chat.py        uma rodada de conversa (salva, gera, transmite)
  llm.py         provedores: mock e llamacpp
  lembretes.py   APScheduler dos lembretes
  eventos.py     barramento e SSE
  rotas_*.py     endpoints
screen/          tela web (servida em /)
deploy/          jarvis.service e README de instalação no Pi
docs/            decisoes.md, pendencias.md
tests/           pytest, sem rede e sem modelo
```

## Rodar na sua máquina

Python 3.10 ou mais novo (o Pi usa 3.10).

```bash
cd jarvis
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env          # preencha JARVIS_TOKEN (mínimo 16 caracteres)
.venv/bin/python -m pytest    # 88 testes, sem rede
.venv/bin/python -m backend   # sobe em http://localhost:8000
```

Com `JARVIS_LLM=mock` (o padrão) não precisa de modelo nenhum.

## Autenticação

Todas as rotas pedem o cabeçalho `X-Jarvis-Token` com o valor de `JARVIS_TOKEN`.
Exceções: `/events` e `/ws/chat` também aceitam `?token=`, porque EventSource e
WebSocket do navegador não mandam cabeçalho. A tela estática em `/` é pública
(não tem dado nenhum).

## Endpoints

| método | caminho | o que faz |
| --- | --- | --- |
| GET | `/status` | temperatura, CPU, RAM, uptime, `mascote`, `tarefa_atual`, `proximas` |
| GET/POST | `/notes` | lista e cria notas (`titulo`, `texto`) |
| GET/PATCH/DELETE | `/notes/{id}` | lê, altera parcialmente, apaga |
| GET/POST | `/agenda` | compromissos (`titulo`, `quando`, `descricao`) |
| GET/PATCH/DELETE | `/agenda/{id}` | idem |
| GET/POST | `/reminders` | lembretes (`texto`, `quando`); `quando` precisa estar no futuro |
| GET/PATCH/DELETE | `/reminders/{id}` | idem; mudar `quando` reagenda |
| GET | `/chat/historico?limite=50` | últimas mensagens, em ordem cronológica |
| GET | `/events` | SSE: `status`, `token`, `chat_final`, `nota_criada`, `lembrete_disparado`… |
| POST | `/input/key` | `{"key": 1\|2\|3, "action": "down"\|"up"}`; emite o evento `tecla` |
| WS | `/ws/chat` | ver protocolo abaixo |

Datas: entrada sem fuso é lida no `JARVIS_TZ`; a saída vem sempre com o fuso.
No banco tudo fica em UTC.

## Protocolo do chat (`/ws/chat`)

Cliente envia: `{"text": "sua pergunta"}`.

Servidor responde, nesta ordem:

```json
{"tipo": "token", "texto": "Olá"}
{"tipo": "token", "texto": ", "}
{"tipo": "final", "mensagem_id": 2, "texto": "Olá, ...", "provedor": "mock",
 "tokens": 6, "tokens_por_s": 19.74, "tempo_total_s": 0.34,
 "tempo_ate_primeiro_token_s": 0.04}
```

Em caso de falha: `{"tipo": "erro", "erro": "..."}`. A conexão continua aberta.
Uma geração por vez no serviço inteiro; um segundo cliente espera.

## Eventos (`/events`)

Cada evento é `event: <tipo>` com `data: {"tipo", "dados", "quando"}`. Ao
conectar, chega o `status` atual. Ping em comentário a cada 15 s.

## Provedores de LLM

- `JARVIS_LLM=mock`: texto falso com atraso (`JARVIS_MOCK_ATRASO_S`).
- `JARVIS_LLM=llamacpp`: `POST {LLAMA_URL}/v1/chat/completions` com `stream: true`.

Para trocar de provedor: edite o `.env` e reinicie. Um novo provedor é uma
classe com `nome` e `async def gerar(mensagens)` que dá `yield` nos pedaços de
texto, registrada em `backend/llm.py:criar_provedor`.

## Teclas físicas

`POST /input/key` chama `backend/entrada.py:registrar_tecla`. Quando o GPIO
entrar, a leitura dos pinos chama essa mesma função. Nada mais muda.

## Tela 800×480

`screen/` é a tela do display de 5", servida em `/`. Sem build: HTML, CSS e JS.

- Simulador no navegador: `http://localhost:8000/?sim=1` (moldura 800×480 e 3 botões).
- Teclas: 1 falar (por enquanto abre o campo de texto), 2 próxima tela, 3
  voltar/cancelar. O teclado manda `POST /input/key`, como o hardware.
- Teste de ponta a ponta com Playwright, com o backend no ar (`JARVIS_LLM=mock`):

  ```bash
  JARVIS_TOKEN=<mesmo token do .env> node tests/e2e_tela.js
  ```

  Grava as capturas em `docs/screens/`.
- No Pi, em modo quiosque: ver `deploy/README.md`, seção 8.

Decisões em `docs/decisoes.md`, seção 19.
