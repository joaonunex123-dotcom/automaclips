# clips-automacao

Pipeline automatizado de clips verticais: descobre vídeos em alta nos canais
monitorados, recorta os melhores trechos, edita com legenda e SFX, publica e
recalibra a seleção com base no que performou.

Estado: **etapa 4 de 7** — do canal monitorado ao clip vertical renderizado,
com legenda queimada e efeitos sonoros. Ainda sem publicação. As etapas
seguintes estão listadas em [Roadmap](#roadmap).

## Instalação

```bash
pip install -r requirements.txt
```

Fora do pip, obrigatório a partir da etapa 2: **ffmpeg no PATH**. O yt-dlp o usa
para juntar as faixas de vídeo e áudio separadas do YouTube, e a extração do
áudio de trabalho é uma chamada direta a ele.

```bash
winget install Gyan.FFmpeg
```

Configuração, nesta ordem:

1. `cp .env.example .env` e preencha `YOUTUBE_API_KEY`
   (console.cloud.google.com > APIs > Credenciais > Chave de API, com a
   *YouTube Data API v3* habilitada) e `ANTHROPIC_API_KEY`. A do YouTube é só
   leitura pública por enquanto — o upload da etapa 6 vai precisar de OAuth,
   que é outra credencial.
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

```bash
python -m pipeline.processar
```

Pega os vídeos descobertos, do melhor score para o pior, e leva cada um até a
seleção de trechos: download → transcrição → Claude → confirmação por energia.
`--limite` controla quantos por execução, `--retentar` reinclui os que estão em
`falha`.

Cada etapa concluída é gravada antes da seguinte começar, então a execução é
retomável: o download de um vídeo de duas horas não é refeito porque o Whisper
morreu depois dele. Falha em um vídeo não derruba os outros — ele vai para
`falha` com o motivo na coluna `erro` e a fila continua.

```bash
python -m editing.editar
```

Renderiza os trechos selecionados como .mp4 vertical, com legenda queimada,
hook de abertura e watermark — tudo pelo `template_config.json`. Nada é
publicado: com `AUTO_PUBLISH=false` os clips só ficam em `render/`.

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

## A escolha dos trechos

O `highlight_detect` manda a **transcrição inteira** num prompt só. Opus 5 tem
1M de contexto, o que cobre um podcast de horas com folga; janela deslizante
economizaria contexto e perderia justamente o que interessa — a piada que só
fecha porque foi montada oito minutos antes.

A resposta vem por *structured outputs* (`output_config.format`), então é JSON
válido por construção: sem parser tolerante, sem retry-on-parse, sem regex. O
system prompt é idêntico para todos os vídeos e carrega o breakpoint de cache;
a transcrição, que muda a cada vídeo, fica depois dele.

O Claude lê **texto**, e texto não carrega reação: uma frase morna na
transcrição pode ser a que fez a mesa rir. Por isso a nota dele é confirmada
contra os **picos de energia do áudio** (librosa) — risada, grito, palma e corte
de edição são todos saltos de RMS. A energia entra como fator multiplicativo,
nunca como nota própria: sozinha ela não escolhe clip nenhum, porque uma
vinheta é puro pico. Trecho sem pico perde 30% da nota em vez de ser vetado —
às vezes a revelação é dita baixinho, e é exatamente onde o modelo acerta e o
áudio não vê.

Depois disso: duração encaixada na faixa (corta o fim se longo, estica para os
dois lados se curto), corte por limiar, e resolução de sobreposição mantendo o
de maior score. Tudo que sai vira linha com `status = 'descartado'` e o motivo —
sem isso só se saberia como performou o que passou, nunca o que foi barrado.

## Transcrição: local ou pela API

Dois backends, mesmo contrato de saída — nada depois de `transcribe.py` sabe
qual rodou.

| backend | quando |
| --- | --- |
| `local` (faster-whisper) | sem custo; em CPU, transcrever 4 h leva horas |
| `openai` (Whisper API) | minutos em vez de horas; cobrado por minuto de áudio |

`TRANSCRICAO_BACKEND` vazio decide sozinho: usa a API quando há
`OPENAI_API_KEY` no ambiente, cai no local quando não há. Fixar a API como
padrão quebraria o pipeline numa máquina sem chave; fixar o local faria a chave
configurada não servir para nada.

Dois obstáculos moldam o caminho da API. O wav de trabalho tem ~1,9 MB por
minuto, então treze minutos já batem no teto de upload — o áudio é recomprimido
para mp3 mono de 32 kbps antes de subir (~50x menor, transcrição igual). E o
que ainda passa do teto é **fatiado no silêncio mais próximo** do alvo, achado
pelo `silencedetect` do ffmpeg: cortar no meio de uma palavra estraga uma
palavra por fronteira, e as fronteiras caem em posições arbitrárias — uma delas
eventualmente cai dentro de um clip. Os timestamps de cada fatia voltam
relativos a ela e são deslocados na normalização.

**Orçamento.** Como o preço é por minuto de áudio, o custo é conhecido *antes*
da chamada. Cada transcrição paga é registrada na tabela `custos`, e a que
ultrapassaria `ORCAMENTO_USD` (padrão 10) é **recusada antes de subir o
arquivo** — o vídeo fica em `falha` com o motivo. Sem isso o modo de falha real
é o saldo acabar no meio de uma execução noturna e metade da fila voltar com
erro de billing. `ORCAMENTO_USD=0` desliga a guarda.

O gasto acumulado aparece no resumo de `python -m pipeline.processar`.

## O template do clip

Todo parâmetro visual mora em
[`editing/template_config.json`](editing/template_config.json) — fonte,
tamanho, cor, posição, reframe, zoom, watermark, codec. Nada disso existe no
código: ajustar o visual é editar o JSON e renderizar de novo.

Legenda, hook e watermark saem de um **único arquivo .ass**, não de filtros
`drawtext`. Fazê-los com drawtext exigiria um filtro por palavra (centenas por
clip) mais um caminho de fonte absoluto que muda por sistema operacional. Como
consequência útil, a parte mais detalhista da etapa — o sincronismo palavra a
palavra — vira geração de string, testável sem ffmpeg instalado.

O destaque anda palavra a palavra, mas a linha inteira fica na tela (legenda de
clip é lida em tela pequena), e cada evento se estende até a palavra seguinte —
sem isso a legenda apagaria na pausa entre duas palavras e piscaria a cada
respiro do locutor.

`versao` é gravada junto de cada render. É o que permite saber, na etapa 7, se
a diferença de performance entre dois clips veio do trecho ou do visual —
**mude a versão sempre que mexer no template**, senão a série histórica mistura
dois visuais sem deixar rastro.

## Efeitos sonoros

Desligados por padrão: dependem de arquivos de áudio que não vêm no
repositório. Ver [assets/sfx/README.md](assets/sfx/README.md) para ligar.

Três gatilhos, cada um respondendo a um dado que o pipeline já produziu:

| gatilho | quando |
| --- | --- |
| `transicao` | abertura do clip e virada do hook para o conteúdo |
| `pico` | picos de energia do áudio, medidos na etapa 2 |
| `palavra_chave` | palavras da lista do template, e exclamação na fala |

Cada som declara o próprio gatilho no `template_config.json`, então
acrescentar um quarto efeito é acrescentar uma chave — não mexer no código.

O defeito que a regra de seleção existe para evitar: uma gargalhada produz
vários picos seguidos **e** várias palavras marcadas ao mesmo tempo. Sem
espaçamento mínimo e teto por clip, sai metralhadora de efeito em cima de três
segundos de áudio. Em disputa por espaço vence a `transicao` — ela é
estrutural, marca o corte; pico e palavra são realce, e perder um não custa
nada.

Os instantes dos picos ficam gravados por clip (`picos_clip`), relativos ao
início dele. Recalcular na hora do render custaria carregar o áudio inteiro do
vídeo-fonte — quase um gigabyte num podcast de quatro horas — uma vez por clip.

Com `sfx.ativo: true` e um arquivo faltando, a carga do template **falha
apontando qual**, antes do primeiro render. Pular o efeito em silêncio
produziria um defeito que só aparece assistindo, muito depois de a fila
inteira ter rodado.

## Estrutura

```
settings.py                  todo número ajustável, sobrescrevível por env var
db/schema.sql                schema do clips.db, idempotente
db/repositorio.py            acesso a dado puro (WAL, BEGIN IMMEDIATE)

sourcing/canais.py           lê canais.json
sourcing/youtube.py          YouTube Data API v3
sourcing/score.py            a fórmula (função pura)
sourcing/descobrir.py        decide o status e orquestra a varredura

pipeline/download.py         yt-dlp + extração do áudio de trabalho
pipeline/transcribe.py       faster-whisper local, e a escolha de backend
pipeline/whisper_api.py      Whisper API: compressão, fatiamento, custo
pipeline/energia.py          picos de RMS (só carregar_audio toca o librosa)
pipeline/highlight_detect.py Claude sobre a transcrição
pipeline/select_clips.py     duração, energia, limiar, sobreposição
pipeline/processar.py        orquestra a fila, com retomada e orçamento

editing/template_config.json TODO parâmetro visual e sonoro, nada no código
editing/template.py          carga e validação do template
editing/legendas.py          gera o .ass (string pura, sem ffmpeg)
editing/sfx.py               decide quando cada efeito toca
editing/render.py            monta e executa o comando do ffmpeg
editing/editar.py            orquestra a fila de render

assets/sfx/                  os arquivos de efeito (fora do git)
```

`fila_clips` guarda o vídeo-FONTE; `midia` os artefatos baixados; `clips` os
trechos. Status de um vídeo:

| status | significado |
| --- | --- |
| `descoberto` | passou no corte, aguardando o pipeline |
| `abaixo_do_limiar` | pontuado, mas fora do threshold |
| `ignorado` | fora da faixa de duração, velho demais, ou publicação no futuro |
| `baixado` | vídeo e áudio em disco |
| `transcrito` | transcrição em disco |
| `analisado` | trechos selecionados, pronto para a etapa 3 |
| `sem_clips` | analisado, nenhum trecho passou no corte |
| `falha` | quebrou; o motivo está na coluna `erro` |

Cada status é o estado **alcançado**, não o em andamento: a linha só sai de
`descoberto` quando o download terminou de verdade. É isso que faz a retomada
saber exatamente onde continuar.

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

Suíte inteira sem rede, sem chave de API, sem ffmpeg, sem Whisper e sem tocar o
`clips.db` real: o banco vai para um `tmp_path` por teste e todo o resto entra
por duplo ([`tests/conftest.py`](tests/conftest.py)). Nenhuma dependência
pesada precisa estar instalada para a suíte rodar — cada uma é importada dentro
da função que a usa, não no topo do módulo. Rodar a suíte completa antes de
qualquer commit.

## Roadmap

1. ~~`db/schema.sql` + `sourcing/` + fila~~
2. ~~`pipeline/` — download, transcrição, `highlight_detect` (Claude + picos de
   energia via librosa)~~
3. ~~`editing/` — template fixo em `template_config.json`, reframe + legendas
   word-by-word~~
4. **SFX (whoosh nos cortes, ding/pop nos picos)** ← aqui
5. `publish/` em modo sombra (`AUTO_PUBLISH=false`: gera e não posta)
6. Publicação real com `scheduler.py`, respeitando quota
7. `analytics/` + `recalibrate.py` — top 10% viram few-shot no prompt, canais
   ruins saem do `canais.json`
