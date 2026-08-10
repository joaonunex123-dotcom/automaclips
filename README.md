# clips-automacao

Pipeline automatizado de clips verticais: descobre vídeos em alta nos canais
monitorados, recorta os melhores trechos, edita com legenda e SFX, publica e
recalibra a seleção com base no que performou.

Estado: **etapa 1 de 7** — sourcing e fila. As etapas seguintes estão listadas
em [Roadmap](#roadmap).

## Instalação

```bash
pip install -r requirements.txt
```

Configuração, nesta ordem:

1. `cp .env.example .env` e preencha `YOUTUBE_API_KEY`
   (console.cloud.google.com > APIs > Credenciais > Chave de API, com a
   *YouTube Data API v3* habilitada). Só leitura pública por enquanto — o
   upload da etapa 6 vai precisar de OAuth, que é outra credencial.
2. `cp sourcing/canais.exemplo.json sourcing/canais.json` e troque pelos canais
   que você acompanha. O `id` é o do canal, começando em `UC` — não o `@handle`.

Nenhum dos dois entra no git: `.env` guarda segredo, e `canais.json` é
reescrito pelo próprio sistema na etapa 7.

## Uso

```bash
python -m sourcing.descobrir
```

Varre os canais ativos, pontua os uploads recentes e grava na fila. Roda quantas
vezes quiser: reencontrar um vídeo é atualização, nunca linha duplicada.

```bash
python -m sourcing.descobrir --verbose --threshold 200
```

`--threshold` e `--max-por-canal` sobrescrevem o `settings.py` só naquela
execução — útil para calibrar o corte olhando o resultado antes de fixá-lo
no `.env`.

Os módulos rodam com `python -m` (e não `python sourcing/descobrir.py`) porque
importam entre pacotes: `-m` põe a raiz do repositório no `sys.path`.

## O score

```
score = views ganhas / horas desde a publicação
```

Ranquear por **total** de views premiaria arquivo — um vídeo com 5 milhões
acumuladas em dois anos ganharia de qualquer coisa publicada ontem. O que
interessa para recortar clip é o que está acelerando agora, então o numerador é
o incremento desde a observação anterior daquele mesmo vídeo (o histórico fica
em `observacoes_video`).

Na primeira vez que um vídeo é visto não há observação anterior, e a referência
implícita passa a ser a publicação com zero views — a primeira pontuação é a
velocidade média desde que o vídeo saiu.

O denominador é a idade desde a **publicação**, não o intervalo entre as duas
observações. É o que produz o decaimento: o mesmo ganho de 10 mil views vale
mais num vídeo de 6 h do que num de 60 h, porque no segundo a janela para
surfar o assunto já está fechando.

Detalhes e casos de borda (revisão de views para baixo, estreia agendada, piso
do denominador) em [`sourcing/score.py`](sourcing/score.py).

## Estrutura

```
settings.py              todo número ajustável, sobrescrevível por env var
db/schema.sql            schema do clips.db, idempotente
db/repositorio.py        acesso a dado puro (WAL, BEGIN IMMEDIATE)
sourcing/canais.py       lê canais.json
sourcing/youtube.py      YouTube Data API v3
sourcing/score.py        a fórmula (função pura)
sourcing/descobrir.py    decide o status e orquestra a varredura
```

`fila_clips` guarda o vídeo-FONTE, com um destes status na etapa 1:

| status | significado |
| --- | --- |
| `descoberto` | passou no corte, aguardando a etapa 2 |
| `abaixo_do_limiar` | pontuado, mas fora do threshold |
| `ignorado` | fora da faixa de duração, velho demais, ou publicação no futuro |

Vídeos fora do corte **são gravados**, não descartados: o score precisa de
"views ganhas desde a última observação", e não existe observação anterior de um
vídeo que nunca foi gravado. Sem essa linha, um vídeo que engata no dia seguinte
seria pontuado do zero para sempre.

## Quota do YouTube

O teto diário é 10.000 unidades, e o upload da etapa 6 custa 1.600 por vídeo.
Por isso o sourcing **não** usa `search.list` (100 unidades por chamada): o
caminho é canal → playlist de uploads → vídeos, a 1 unidade por chamada. Para
10 canais, 15 unidades por varredura — contra 1.000 se cada canal fosse um
search.

## Testes

```bash
python -m pytest
```

Suíte inteira sem rede, sem chave de API e sem tocar o `clips.db` real: o banco
vai para um `tmp_path` por teste e a API entra por duplo
([`tests/conftest.py`](tests/conftest.py)). Rodar a suíte completa antes de
qualquer commit.

## Roadmap

1. **`db/schema.sql` + `sourcing/` + fila** ← aqui
2. `pipeline/` — download, transcrição, `highlight_detect` (Claude + picos de
   energia via librosa)
3. `editing/` — template fixo em `template_config.json`, reframe + legendas
   word-by-word
4. SFX (whoosh nos cortes, ding/pop nos picos)
5. `publish/` em modo sombra (`AUTO_PUBLISH=false`: gera e não posta)
6. Publicação real com `scheduler.py`, respeitando quota
7. `analytics/` + `recalibrate.py` — top 10% viram few-shot no prompt, canais
   ruins saem do `canais.json`
