# Decisões

Registro do que foi decidido, e por quê. Quando uma decisão mudar, escreva a
nova por cima de uma seção nova, sem apagar a antiga.

## 1. Onde o projeto mora

O repositório onde o Jarvis foi pedido é o `automaclips` (pipeline de clips de
vídeo). O Jarvis fica numa subpasta, `jarvis/`, na branch designada
`ccr-48119767-bd2b3f`. Motivo: o `automaclips` tem `settings.py`,
`requirements.txt`, `pytest.ini`, `tests/` e `README.md` na raiz. Colocar o
Jarvis lá misturaria dependências e suítes de dois produtos diferentes.

O `.gitignore` da raiz já cobre o que o Jarvis gera: `.env`, `*.db`,
`.venv/`, `__pycache__/`. Não foi preciso alterá-lo.

Se o Jarvis ganhar vida própria, a pasta sai para outro repositório sem mudança
de código (ela não importa nada do `automaclips`).

## 2. Reaproveitamento do que já existia

Leitura completa do repositório antes de escrever. Resultado: quase nada é
reaproveitável diretamente, e o motivo é concreto.

- `llm_client.py`: fala com OpenRouter/OpenAI, com `settings` do pipeline.
  O Jarvis precisa de llama-server com streaming, que é outro caminho.
- `log_seguro.py`: mascara as chaves do `settings` do pipeline. O Jarvis tem um
  token só, e não loga o valor dele.
- `db/repositorio.py` e `db/schema.sql`: schema de clips, com conceitos que não
  existem aqui.

Reaproveitado como convenção (não como código): o `pytest.ini` com
`pythonpath = .`, os comentários explicando o porquê, o uso de WAL e
`busy_timeout` no SQLite, e a ideia de isolar a config em um dict nos testes
(o `conftest` do pipeline já faz isso com o `.env`).

## 3. Stack

- **FastAPI + uvicorn**: o pedido. WebSocket vem nativo; SSE sai de um
  `StreamingResponse`, sem a dependência `sse-starlette`.
- **SQLite pela stdlib (`sqlite3`)**: sem ORM. São quatro tabelas simples.
- **APScheduler 3.11**: o pedido. Não usei a 4.x, que ainda é alfa e tem API
  diferente.
- **psutil**: leitura de CPU, RAM e uptime.
- **httpx** (só no provedor `llamacpp`): cliente assíncrono com streaming. Já
  vem como dependência do FastAPI, então não aumenta a instalação.
- **websockets**: o uvicorn precisa dele para servir WebSocket.
- Sem `pydantic-settings`, sem `python-dotenv`. A leitura do `.env` tem 20
  linhas (`backend/config.py`), e o Pi ganha uma dependência a menos.

Versões fixadas em `requirements.txt`, testadas em **Python 3.10.20**, que é o
Python do Ubuntu 22.04 do Pi. Isso foi testado com `uv` em x86_64. O Pi é
aarch64, o que ainda não foi validado (ver pendências).

## 4. Provedor de LLM plugável

`JARVIS_LLM=mock|llamacpp`. Cada provedor é um objeto com `nome` e um
`async def gerar(mensagens)` que é um gerador assíncrono de pedaços de texto.
A interface é só isso, e o chat não sabe qual provedor está por trás.

O llama-server não é iniciado pelo Jarvis. O modelo sobe à parte, como
serviço próprio, e o Jarvis só fala com ele por HTTP. Assim um crash do modelo
não derruba a API, e trocar de modelo não exige mexer no backend.

## 5. Métricas do chat

- **tokens**: número de pedaços recebidos. O llama-server manda um token por
  pedaço, então é uma contagem boa, mas não a exata do tokenizador. Não usei o
  campo `usage`, que nem todo llama-server devolve no stream.
- **tempo_ate_primeiro_token_s**: do pedido até o primeiro pedaço (inclui o
  processamento do prompt).
- **tokens_por_s**: tokens divididos pelo tempo *depois* do primeiro token. É a
  velocidade de geração, que é o número que interessa no Pi. Incluir o prompt
  no cálculo faria o número parecer muito pior do que é.
- **tempo_total_s**: do pedido até o fim.

## 6. Uma geração por vez

Um `asyncio.Lock` no serviço inteiro. O llama-server atende um pedido por vez de
qualquer forma. Dois clientes intercalando tokens na mesma tela seria confuso.
O segundo cliente espera, e o estado fica `ouvindo` enquanto isso.

## 7. Cliente que cai no meio da resposta

A resposta parcial **não é salva**. Ela não tem a quem ser entregue, e o
histórico fica só com pergunta e resposta inteiras. A geração para na hora, e
o mascote volta a `descansando`.

## 8. Contexto do chat

As últimas `JARVIS_HISTORICO_MSGS` (padrão 10) mensagens vão para o modelo, com
o `JARVIS_SYSTEM_PROMPT` na frente. Não há resumo nem janela deslizante por
tokens: é simples, e o limite é fácil de ajustar.

## 9. Autenticação

- Um token compartilhado em `JARVIS_TOKEN`, comparado com `hmac.compare_digest`.
- **Falha fechada**: sem token, ou com menos de 16 caracteres, o serviço não
  sobe. Não existe modo "sem auth".
- **Cabeçalho `X-Jarvis-Token`** nas rotas REST, que é o pedido.
- **`?token=` só em `/events` e `/ws/chat`**. EventSource e WebSocket do
  navegador não enviam cabeçalho customizado. Deixei a query string aceita
  somente nesses dois, para o token não acabar em log de todo endpoint. Ele
  continua indo para o log de acesso do uvicorn nesses dois casos.
- **A tela `/` é pública.** Ela é HTML sem dado; o token é pedido na tela e
  guardado no `localStorage` do navegador.
- O tráfego pela Tailscale já é criptografado (WireGuard). Por isso não há
  HTTPS no próprio serviço.

## 10. CORS

Liberado por regex: localhost, a faixa 100.64.0.0/10 da Tailscale e os nomes
`*.ts.net`. CORS não é controle de acesso (é o navegador que o aplica); a
chave continua obrigatória em cada rota.

## 11. Datas

- No banco: UTC em ISO 8601 com `+00:00`. Comparar texto dá o mesmo resultado
  que comparar tempo.
- Na API: sempre com o fuso de `JARVIS_TZ` (padrão `America/Sao_Paulo`).
- Entrada sem fuso é lida como horário de `JARVIS_TZ`.
- Lembrete com horário no passado é recusado (422). Agenda aceita passado, para
  registrar o que já aconteceu.

## 12. Lembretes

- Cada lembrete é um job `date` do APScheduler com id `lembrete-<id>`.
- `misfire_grace_time=None`: se o Pi estava desligado na hora, o lembrete sai
  assim que o serviço sobe. O padrão do APScheduler (1 s) descartaria em
  silêncio.
- Na subida, todos os pendentes são reagendados a partir do banco, porque o job
  vive só na memória.
- `PATCH` com novo `quando` zera `disparado_em` e reagenda. Assim um lembrete
  já disparado pode ser reativado sem ser recriado.

## 13. Rotas `async`, e por quê

O barramento de eventos (`asyncio.Queue`) não é thread-safe. Se uma rota fosse
`def` (que roda numa threadpool) e publicasse, a fila seria mexida de outra
thread. Todas as rotas são `async`. O SQLite é local e rápido, então ficar na
thread do loop não aparece.

## 14. Eventos e fila lenta

Cada cliente SSE tem uma fila de 200 eventos. Se encher (cliente travado), o
evento novo é descartado para aquele cliente. Preferimos perder um token na
tela a deixar a memória do Pi crescer.

## 15. Teclas físicas

`registrar_tecla(barramento, tecla, acao)` é o ponto único. Hoje o HTTP chama
essa função; o GPIO vai chamar a mesma. A função valida tecla (1–3) e ação
(down/up), e só publica o evento `tecla`. O que cada tecla *faz* não está
decidido, e não é o trabalho desta etapa.

## 16. Conexão ao SQLite

Uma conexão por operação, aberta e fechada dentro de um context manager.
Abrir é barato, e não há estado escondido entre rotas nem entre threads. WAL
para a leitura da tela não travar a escrita do chat.

## 17. Erros

- Falha do provedor vira `{"tipo": "erro"}` com a mensagem do `ErroLLM`, que é
  pensada para o usuário.
- Exceção inesperada é logada inteira no servidor, e o cliente recebe só
  `erro interno ao gerar a resposta`. Não vaza detalhe interno.

## 18. Tela web

Placeholder funcional em `screen/index.html`: mostra o mascote, a tarefa, os
sensores e os próximos itens, atualizando pelo `/events`. Não há chat na tela
ainda. O layout final de 800×480 é trabalho à parte.
