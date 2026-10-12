# clips-automacao

Pipeline automatizado de clips verticais: descobre vídeos em alta nos canais
monitorados, recorta os melhores trechos, edita com legenda e SFX, publica e
recalibra a seleção com base no que performou.

Estado, configuração desta máquina e o que falta para ligar:
**[HANDOFF.md](HANDOFF.md)**.

Estado: **as 7 etapas entregues**, rodando sozinho no relógio e recalibrando
a seleção com o que performou. Publica em YouTube Shorts, Instagram Reels e
TikTok — o mesmo arquivo nos três, com o texto de cada um. A publicação real
está **construída mas desligada**: `AUTO_PUBLISH=false` é o padrão, e virar
essa chave é decisão sua.

## Instalação

```bash
pip install -r requirements.txt
```

Fora do pip, obrigatório a partir da etapa 2: **ffmpeg no PATH**. O yt-dlp o usa
para juntar as faixas de vídeo e áudio separadas do YouTube, e a extração do
áudio de trabalho é uma chamada direta a ele.

O pipeline chama o `ffmpeg` **do PATH** (`shutil.which("ffmpeg")` em
`pipeline/download.py`; `"ffmpeg"` literal em `editing/render.py`). Não usa o
binário do `imageio-ffmpeg`. Precisa ter **libass** — sem ele o filtro
`subtitles`/`ass` não existe e a legenda não é queimada no render. Confira:

```bash
ffmpeg -hide_banner -filters | grep -E " (subtitles|ass) "
```

Caminho único documentado:

- **macOS (esta máquina):** build estático arm64 do martin-riedl.de, em
  `~/ffmpeg-bin/ffmpeg`, com `export PATH="$HOME/ffmpeg-bin:$PATH"` no
  `~/.zshrc`. Não use o do Homebrew nem o do `imageio-ffmpeg`.
- **Windows:** `winget install Gyan.FFmpeg` (já vem com libass).

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

O Python oficial do projeto é o `.venv313` (3.13; o yt-dlp novo exige >= 3.10).
Rode tudo com ele, ativado ou direto pelo caminho:

```bash
source .venv313/bin/activate            # ou: .venv313/bin/python -m ...
.venv313/bin/python -m pytest           # suíte
```

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
hook de abertura e watermark — tudo pelo `template_config.json`.

```bash
python -m publish.publicar
```

Gera o metadado de cada clip (uma chamada ao Claude serve as duas
plataformas), marca um horário livre e processa o que venceu. Com
`AUTO_PUBLISH=false` — o padrão — tudo acontece de verdade **menos** a chamada
à plataforma: o post fica marcado como `simulado`, com o texto que sairia.

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

## Qual modelo faz o quê

| etapa | provedor | por quê |
| --- | --- | --- |
| escolher os trechos | **Claude direto** (`claude-opus-5`), ou OpenAI/OpenRouter via `HIGHLIGHT_PROVEDOR` | é a decisão que define o produto — o último lugar onde vale economizar |
| metadado (título, caption) | OpenRouter (`MODEL_METADATA`) | escrever caption é trabalho de menor exigência |
| recalibração (etapa 7) | OpenRouter (`MODEL_RECALIBRATE`) | idem |
| transcrição | OpenAI (`whisper-1`) | é o que devolve timestamp por palavra |

O SDK da `openai` serve três caminhos pela mesma dependência: a Whisper API, o
OpenRouter e a própria OpenAI — todos compatíveis, só muda a `base_url`.
`LLM_PROVEDOR` escolhe entre OpenRouter e OpenAI para metadado e recalibração;
`HIGHLIGHT_PROVEDOR` faz o mesmo para a escolha do trecho, com `anthropic` como
padrão.

**Trocar o provedor do `highlight_detect` é decisão de custo, não de
conveniência.** Fora da Anthropic perdem-se três coisas: a saída estruturada
*garantida*, o cache de prompt e o fallback server-side contra recusa. Em troca,
roda com o crédito que você já tem — que às vezes é a diferença entre rodar e
não rodar.

**O que se perde ao sair do Claude direto:** a saída estruturada *garantida*.
Com `output_config.format` a resposta era JSON válido por construção; no
caminho compatível com OpenAI o melhor disponível é `response_format:
json_object`, que pede JSON mas não garante. Duas defesas cobrem isso: um
extrator tolerante (tira cerca ```json e prosa em volta) e o `MODEL_FALLBACK`,
que refaz a chamada num modelo mais forte quando a resposta é ininteligível.

Qual modelo realmente respondeu fica gravado em `geracoes_llm` — o OpenRouter
roteia para variantes e o fallback pode entrar no meio. Sem esse registro, a
etapa 7 compararia performance de clips sem saber que metade do texto veio de
um modelo e metade de outro, e atribuiria ao trecho uma diferença que era do
texto.

## Download em 1080p: servidor de PO token

O YouTube só entrega o vídeo em HD inteiro a quem apresenta um **PO token**.
Sem ele o cliente web devolve só imagens e o `android_vr` é cortado com 403 em
~10 MB. O pipeline gera o token com o servidor do
[bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
(plugin do yt-dlp + servidor node em `127.0.0.1:4416`).

`pipeline/download.py` decide sozinho: servidor no ar → tenta `YTDLP_FORMATO`
(1080p); servidor fora do ar, ou o HD falha → cai para o formato 18 (360p com
áudio, cliente android), que baixa inteiro sem token. A resolução obtida vai
para o log (`Baixado ... 1920x1080`) e para `download.ultimo_download`.

**Requisitos:** Python **>= 3.10** (yt-dlp 2026.x não roda no 3.9), `node` e o
servidor compilado. Instalação, uma vez (macOS arm64 como exemplo; tudo cai em
`.pot/`, fora do git):

```bash
uv venv --python 3.13 .venv313
uv pip install --python .venv313/bin/python -r requirements.txt "yt-dlp[default]" bgutil-ytdlp-pot-provider==2.0.2
# node (nodejs.org/dist, conferir o SHASUMS256) extraído em .pot/node-vXX-<so>-<arch>/
git clone --depth 1 --branch 2.0.2 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git .pot/src
cd .pot/src/server && PATH=../../node-v*/bin:$PATH npm ci && PATH=../../node-v*/bin:$PATH npx tsc
```

**Subir e parar:**

```bash
ops/pot_provider.sh subir     # sobe em 127.0.0.1:4416 (BGUTIL_PORTA muda a porta)
ops/pot_provider.sh status
ops/pot_provider.sh parar
```

Log em `.pot/servidor.log`. O servidor só responde em loopback e não guarda
credencial. Para o processo da fila, `YTDLP_JS_RUNTIME` aponta o node (o yt-dlp
novo resolve o desafio JS do YouTube com ele) e `POT_PROVIDER_URL` muda a URL.

Um `.mp4` já baixado em 360p **não é trocado sozinho**: apague (ou renomeie) o
arquivo e o `midia` para rebaixar em HD. HD custa banda e disco: o vídeo de 1h50
tem ~580 MB em AV1 (1,1 GB se `YTDLP_FORMATO` pedir avc1).

## Transcrição: local ou pela API

Dois backends, mesmo contrato de saída — nada depois de `transcribe.py` sabe
qual rodou.

| backend | quando |
| --- | --- |
| `youtube` | **o mais barato**: a legenda que o YouTube já tem. Sem chave, sem modelo, sem CPU |
| `local` (faster-whisper) | sem custo em dinheiro; em CPU, transcrever 4 h leva horas |
| `openai` (Whisper API) | minutos em vez de horas; cobrado por minuto de áudio |

**O backend `youtube` resolve o gargalo sem gastar nada** quando a fonte é
podcast — quase todos têm legenda. A escolha entre os dois tipos não é óbvia:
a **automática** traz timestamp por PALAVRA (a única que permite a legenda
word-by-word do template) com texto pior; a **manual** traz texto muito melhor
e timestamp só por frase. O padrão é automática, porque o destaque é o que o
template promete.

O formato das automáticas é *rolante* — cada bloco repete o texto do anterior
em linhas próprias. Ler ingenuamente triplica cada palavra, e deduplicar só
por tempo não salva (o texto repetido recebe o tempo do bloco atual e passa
como fala nova). Ver `parse_vtt`.

`youtube` **não** entra na escolha automática: ele depende de o vídeo ter
legenda, o que só se descobre tentando. Quem quer, pede.

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

**429 da legenda do YouTube.** O backend `youtube` espera e tenta de novo a cada
429 (`YOUTUBE_429_ESPERAS_S`, padrão 20, 60, 180 s). Esgotadas as esperas, ou se
o vídeo não tem legenda, o `processar` cai **sozinho** para a Whisper API
(`YOUTUBE_FALLBACK_API=0` desliga) passando pela mesma guarda de
`ORCAMENTO_USD`: se o fallback estouraria o teto, o vídeo fica em `falha` sem
chamar a API. O custo do fallback entra na tabela `custos` como qualquer
transcrição paga.

O gasto acumulado aparece no resumo de `python -m pipeline.processar`.

## O template do clip

Todo parâmetro visual mora em
[`editing/template_config.json`](editing/template_config.json) — fonte,
tamanho, cor, posição, reframe, zoom, watermark, codec. Nada disso existe no
código: ajustar o visual é editar o JSON e renderizar de novo.

### Reframe que segue o rosto

`reframe.rosto.ativo` (desligado por padrão, só no modo `corte`) troca a âncora
fixa por uma trilha que acompanha o maior rosto do trecho
([`editing/rosto.py`](editing/rosto.py)). Amostra o vídeo a 2 fps com o
classificador frontal do OpenCV (`opencv-python-headless<5`; a série 5 removeu o
`CascadeClassifier`), e filtra o movimento: zona morta, mediana móvel, rampas
suaves e no máximo `max_movimentos` por clip. Sem rosto confiável (menos de
`minimo_deteccoes` das amostras) ou sem OpenCV, o clip sai com
`ancora_horizontal`, como antes. Detector Haar acha rosto de frente, bem
iluminado; ele **não foi validado contra gravação de rosto real** — ligue e
assista antes de deixar ligado.

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

## Layouts A/B (full x split)

O clip sai em tela cheia (`"layout": "full"`, o padrão) ou em **split**: clip em
cima (~60%) e um vídeo de preenchimento embaixo (~40%), sem áudio — o som é
sempre o do clip. Tudo em `editing/template_config.json`: `split.proporcao_clip`,
`split.categoria`, `split.legenda` (posição da legenda no split) e
`split.separador`.

Para testar, dê pesos às variantes:

```json
"variantes": { "pesos": { "full": 1, "split:parkour": 1 }, "semente": "ab1" }
```

Cada clip recebe **uma** variante, por hash do `clip_id`, gravada em `renders` e
em `resultados` (coluna `variante`); o mesmo clip nunca sai em dois layouts.
`python -m analytics.analisar` compara views/hora (relativo à plataforma) por
variante, mostra o n de cada uma e diz "dado insuficiente" com n < 15
(`ANALYTICS_MIN_POR_VARIANTE`).

Os preenchimentos são arquivos seus em `assets/filler/<categoria>/` — o sistema
não baixa nada, e categoria vazia só desativa a variante (com log). Formato e
aviso de licença: [assets/filler/README.md](assets/filler/README.md).

## Capa do clip

Desligada por padrão (`capa.ativa: false` no `template_config.json`). Ligada,
cada clip ganha uma pasta `<nome>_capa/` ao lado do `.mp4` com
`capa_1080x1920.png` (vertical) e `capa_1280x720.png` (horizontal): o melhor
quadro do trecho com o título curto em fonte grande, sobre uma caixa escura.

O quadro é pontuado, não sorteado — nitidez (variância do Laplaciano), rosto
(OpenCV, Haar), brilho aceitável e penalidade para quadro de corte. Sem OpenCV
degrada para nitidez + brilho; sem amostragem usa o meio do trecho. Falha na
capa nunca derruba o render. O caminho vertical fica em `renders.capa_path`.

| chave | efeito |
| --- | --- |
| `capa.ativa` | gera os PNGs ao lado do clip (o vídeo não muda) |
| `capa.frame_inicial.ativo` | põe o quadro de título nos primeiros `duracao_s` (0,3 s) **do vídeo** |
| `YOUTUBE_ENVIAR_CAPA=true` (.env) | depois do upload chama `thumbnails.set` (50 unidades de quota) |

**Limitação:** o YouTube costuma ignorar capa personalizada em Shorts — a shelf
usa um quadro do próprio vídeo — e `thumbnails.set` exige canal verificado.
Por isso existe `capa.frame_inicial`: o quadro de título dentro dos primeiros
0,3 s é o que a plataforma tende a pegar. O custo é um flash de pôster no
começo do clip.

## Modo gráfico (slides, gráficos, tabelas)

O crop 9:16 central joga fora ~68% da largura do 16:9: num gráfico, o eixo e
metade das barras somem. Com `grafico.ativo: true` o clip detecta os trechos em
que a fonte é conteúdo (não pessoa) e mostra o quadro **inteiro**, centralizado,
sobre fundo preenchido, com fade de 0,25 s entre o enquadramento normal e o
gráfico.

| chave | valores |
| --- | --- |
| `grafico.ativo` | `false` (padrão) / `true` |
| `grafico.fundo` | `"desfoque"` (cópia desfocada da cena) ou `"solido"` (`cor_solida`) |
| `grafico.transicao_s` | duração do fade (0,05 a 1,0) |
| `grafico.largura_minima` | 0,70: abaixo disso o recorte tira só a margem vazia e amplia |

O pedido original chamava a chave de `layout.grafico.fundo`; como `layout` já é
uma string (`full`/`split`) no template, a seção ficou em `grafico`.

Detecção local (sem chamada paga), sobre quadros amostrados a 2/s: sem rosto,
fundo plano, poucas cores dominantes e linhas retas alinhadas aos eixos —
os quatro juntos. Calibrada com um gráfico sintético contra 107 quadros de
uma reportagem real (nenhum falso positivo). Sem detecção confiável o clip sai
como antes. Um modelo de visão não está ligado: o `llm_client` não aceita
imagem; `grafico.detectar(..., confirmar=...)` é o gancho.

## Biblioteca de transições

Tudo gerado por filtros nativos do ffmpeg (nada externo) e **desligado por
padrão**. Cada efeito é `efeitos.<nome>` no `template_config.json`, com
`ativo`, `intensidade` (0..1) e `duracao_s` (0,15 a 0,4 s):

| efeito | o que é | notas |
| --- | --- | --- |
| `shutter` | cortina de 6 lâminas horizontais que fecham e reabrem | `drawbox` por lâmina |
| `iris` | máscara circular que fecha e reabre | máscara em 1/10 da resolução, com alfa |
| `whip_pan` | borrão horizontal de câmera (3 degraus) | `avgblur` |
| `glitch` | franja de cor + saturação, leve | `chromashift` (em YUV) |
| `flash` | clarão que decai | já existia |
| `zoom` | punch-in (zoom punch) | já existia; `fator` faz o papel de intensidade |

`flash` e `zoom` foram **reaproveitados**, não duplicados. Ligar qualquer
transição nova (ou `efeitos.cadencia.ativo`) faz o planejador decidir também os
instantes deles, para valer a regra: **no máximo 1 efeito a cada 4 s**
(`cadencia.espacamento_minimo_s`) e **variando o tipo** (nunca o mesmo em
seguida quando há alternativa). Gatilhos, por prioridade: abertura (só
`flash`) e virada do hook; entrada de gráfico; picos de energia; saída de
gráfico. Com só `flash`/`zoom` ligados e a cadência desligada vale o
comportamento antigo, byte a byte.

Duas armadilhas de custo, medidas: `rgbashift` força o grafo inteiro por RGB e
levou o render de um clip de 30 s de 10 s para 46 s — o glitch usa
`chromashift`; e um `geq` por pixel em 1080x1920 custava +9 s — a íris é uma
máscara em baixa resolução ampliada com bicúbico.

## SFX dos efeitos visuais e mixagem

`efeitos.sfx_visuais` (desligado) faz cada efeito visual disparar um som
**sintetizado por numpy** (`editing/gerar_sfx.py`; nenhum arquivo de terceiros):

| som | acompanha | forma |
| --- | --- | --- |
| `whoosh` | `whip_pan` | ruído filtrado com varredura de 400 Hz a 3,5 kHz |
| `shutter` | `shutter` | dois cliques de obturador + tum de 180 Hz |
| `pop` | `glitch` | bolha: seno de 700 a 180 Hz |
| `impacto` | `flash`, `zoom` | queda de 120 a 38 Hz com cauda |
| `riser` | `iris` | subida de 0,8 s, ataque no fim |

`efeitos.sfx_visuais.categorias` liga/desliga cada som e `mapa` troca o
efeito -> som. Os arquivos são gerados na primeira vez em `sfx.diretorio`.

**Sincronia:** cada som tem uma âncora (o instante em que ele "bate", medido
no sinal) e o arquivo começa em `momento_chave_do_efeito - ancora`; se isso
cairia antes do clip, o início do arquivo é cortado em vez de atrasado.
Verificado num render real: o clique aparece no áudio a 3,0 s ± 1,5 quadro.

**Mixagem** (`efeitos.voz`, `ducking`, `limiter` — todos desligados):
voz normalizada a -16 LUFS (loudnorm em duas passadas), cada SFX limitado a
no máximo `teto_sfx_abaixo_da_voz_db` (8 dB) abaixo da voz, ducking por
sidechain quando há fala e `alimiter` no master (`limiter.teto_db`, -1,5).

## Bancada local de qualidade

```bash
python -m tools.qualidade_local --fonte ARQUIVO.mp4 --inicio 75 --duracao 30 \
    --saida teste_local/qualidade --repeticoes 2
```

Gera `01_base.mp4` .. `06_efeitos_visuais_sonoros.mp4`, um frame de cada em
`frames/`, `comparativo.mp4` (grade lado a lado) e `relatorio.json` com
resolução, duração, LUFS (ebur128), tempo de render e os efeitos ligados. Cada
vídeo passa pelo caminho real (`editar.renderizar_clip`, com banco temporário).
Como a fonte local não tem gráfico, o script insere um gráfico sintético de
1920x1080 no meio do trecho. Não chama LLM, transcrição nem publica; a legenda é
um texto fixo. Roda também o teste da limpeza num diretório temporário, com
ffmpeg real. `teste_local/` é ignorado pelo git.

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

## O laço fechado: analytics e recalibração

```bash
python -m analytics.analisar             # mede e recalibra
python -m analytics.analisar --simular   # mostra o que mudaria, sem mudar
```

Mede cada post publicado (uma linha nova em `resultados` por coleta — histórico,
não estado) e transforma o resultado em quatro ajustes, cada um fechando um
ponto que as etapas anteriores deixaram aberto de propósito:

| recalibração | alimenta |
| --- | --- |
| top 10% viram few-shot | o prompt do `highlight_detect` |
| canais fracos | `sourcing/canais.json` (`ativo: false`) |
| faixa de duração ideal | `select_clips`, via tabela `calibracao` |
| pesos por horário | `publish/scheduler.pesos_do_historico` |

**Toda recalibração tem um mínimo de amostras, e abaixo dele ela não
acontece.** Não é cautela genérica: recalibrar com três clips não aprende nada
e ainda estraga o que estava funcionando. Sem dado, o default do `settings`
continua valendo — um banco sem medição nenhuma se comporta exatamente como
antes desta etapa existir.

**Desempenho é views por hora**, não views cruas, pelo mesmo motivo do score de
sourcing: o acumulado premiaria o post mais antigo. E é normalizado pela
mediana da **própria plataforma** — YouTube, Instagram e TikTok têm escalas
tão diferentes que misturá-los faria uma plataforma vencer sempre. É por isso
que a chegada do TikTok não exigiu nada da recalibração: cada post já era
comparado contra os da própria casa.

Cada plataforma é medida pela credencial que ela mesma pede: o YouTube pela
chave de API (1 unidade por até 50 vídeos, contra as 1600 de um upload), o
Instagram pelo token de longa duração, o TikTok pelo access token da
publicação com o escopo `video.list` — em lotes de 20 ids por chamada. Uma
plataforma fora do ar não derruba a medição das outras: o erro fica no log e
a recalibração roda sobre o que já foi medido.

Cada medição grava views, likes, comentários e **compartilhamentos** — o
TikTok pelo `share_count`, o Instagram pelo insight `shares`, e 0 no YouTube,
que não expõe o número no `videos.list`. No Instagram o share vai pedido na
mesma chamada de insights que já buscava os plays, com uma segunda tentativa
sem ele caso a versão da API não conheça o nome: um `metric` desconhecido
derruba a chamada inteira, e perder as views por causa do compartilhamento
seria trocar o número que a recalibração usa por um que ninguém lê ainda.

Nenhuma recalibração lê essa coluna hoje: ela é gravada para o histórico
existir quando alguém for olhar, que é o contrário de descobrir daqui a seis
meses que o número nunca foi guardado.

Canal fraco é julgado pela **mediana**, não pela média: um único clip que
viralizou levantaria a média de um canal que não rende, e é justamente o canal
que acerta uma vez a cada vinte que se quer desligar. E ele é *desativado*, não
removido — a linha fica no arquivo com o motivo, e reativar é trocar uma
palavra.

Os valores aprendidos vão para a tabela `calibracao`, não para o `.env`: o
`.env` é território do humano (guarda segredo, é editado à mão), e um valor que
o programa reescreve ali viraria conflito na primeira vez que alguém abrisse o
arquivo. Apagar a linha devolve o default.

**Retenção fica de fora por padrão.** `averageViewPercentage` só existe na
YouTube Analytics API, que é outro escopo de OAuth. Sem ela a recalibração de
duração degrada para views/hora por faixa — pior, porque mede alcance e não o
quanto o clip segurou, mas continua sendo medição e não palpite.
`ANALYTICS_RETENCAO=1` liga, depois de autorizar o escopo extra.

## Limpeza do vídeo-fonte e espaço em disco

Um podcast baixado pesa gigabytes; os clips dele, dezenas de megabytes. Num
mini PC 24/7 o disco acaba antes da fila. `pipeline/limpeza.py` apaga o
vídeo-fonte e os intermediários (áudio, fatias de áudio, temporários do
yt-dlp, `.ass`) **depois** que todos os clips daquele vídeo estão renderizados e
validados.

```bash
python -m pipeline.limpeza --simular   # lista o que seria apagado; não toca em nada
python -m pipeline.limpeza             # apaga (exige LIMPAR_FONTE_APOS_CLIPS=true)
```

| variável (.env) | padrão | efeito |
| --- | --- | --- |
| `LIMPAR_FONTE_APOS_CLIPS` | `false` | liga a limpeza real; o laço principal a roda após cada etapa de edição |
| `LIMPEZA_ESPACO_MIN_GB` | `10` | abaixo disso o pipeline pausa os downloads **novos** e registra no log (0 desliga) |

Trava de segurança, por arquivo: precisa ser arquivo comum, **não** link
simbólico, e o `realpath` precisa estar dentro de `CLIPS_DOWNLOADS_DIR` (ou, para
os `.ass`, do diretório de render). Fora disso é recusado e logado. Nunca
apaga: clips renderizados, capas, `clips.db`, transcrições (o metadado que o
agendamento usa) nem arquivo que o banco aponte como render. O resultado fica em
`midia.fonte_apagada_em` e `midia.fonte_bytes_liberados`. Uma fonte apagada
conta como não baixada: reprocessar o vídeo baixa de novo.

## Rodando sozinho

```bash
python -m orchestrator.main_loop --uma-vez   # um ciclo e sai
python -m orchestrator.main_loop             # fica de pé (APScheduler)
```

| etapa | ritmo |
| --- | --- |
| sourcing | 6 h |
| pipeline | 1 h |
| editing | 1 h |
| publish | 15 min |
| analytics | 1x/dia (etapa 7) |

Falha em uma etapa **não derruba o laço**: cada uma roda no próprio
try/except, o erro fica no log com o nome da etapa e a execução continua. Um
canal fora do ar não pode impedir que os clips já renderizados sejam
publicados.

Prefira `--uma-vez` sob o Task Scheduler ou cron: o modo residente é mais
simples de começar, mas se o processo morrer ninguém o levanta — e num
pipeline que publica em horário marcado isso é perder a janela sem aviso.
`--uma-vez` também não precisa do APScheduler instalado.

### 24/7 no mini PC: fila, cadência e status

* **5 posts por dia** (`POSTS_POR_DIA=5`), em `HORARIOS_PADRAO` =
  08:00, 11:15, 14:30, 17:45, 21:00 (a cada 3h15, acima do `INTERVALO_MINIMO_MIN`
  de 120 min). O horário é lido no **relógio local**: o SO do mini PC precisa
  estar em `America/Sao_Paulo` (`FUSO_AGENDAMENTO`); o log e o `status.json`
  avisam se não estiver. O sexto clip do dia vai para o dia seguinte.
* **Quota do YouTube:** 5 uploads = 5 x 1.600 = **8.000** de 10.000. A
  `YOUTUBE_QUOTA_MARGEM` (500) mantém o sexto upload recusado, e sobram **2.000
  unidades** (1.750 com `YOUTUBE_ENVIAR_CAPA`, 50 por capa) para o sourcing e o
  resto do projeto. O contador é o do banco (`quota_api`, dia do Pacífico).
* **Aquecimento:** inalterado — `AQUECIMENTO_POSTS_DIA` (padrão 1; use 2 para
  1-2 posts) por `AQUECIMENTO_DIAS` (3) contados do primeiro post real. Os
  clips agendados além do teto ficam `agendado` e saem nos dias seguintes.
* **Fila de clips prontos:** com menos de `FILA_MINIMA_CLIPS` (8) clips
  renderizados e ainda não publicados, o laço inicia `FILA_VIDEOS_POR_CICLO`
  (1) vídeo novo, do maior score; acima disso só termina o que já começou. `0`
  desliga e volta a processar tudo. `ORCAMENTO_USD` atingido para o pipeline.
* **Instância única + reinício seguro:** trava `orquestrador.lock` (flock) e
  nenhum estado em memória — vídeo `baixado`/`transcrito` é retomado.
* **`ops/status.json`:** última execução, fila, quota, espaço livre, vencimento
  do token (`YOUTUBE_TOKEN_VENCE_EM`) e `alertas`. Exemplos de systemd e launchd
  em [ops/](ops/README.md).

## Vários canais

Um **perfil** é um canal de destino: fontes próprias, banco próprio, mídia
própria e contas próprias nas três plataformas. Quem tem um canal só nunca
precisa saber que isso existe — sem nenhum `.env.<nome>` na raiz, tudo roda
como sempre rodou.

```bash
cp .env.example .env.esportes     # as credenciais DAQUELE canal
cp .env.example .env.podcast

python -m orchestrator.perfis --listar
python -m orchestrator.perfis --verificar
python -m orchestrator.perfis --uma-vez        # todos, em sequência
python -m orchestrator.perfis --uma-vez --perfil esportes
```

O `.env.<nome>` é o que define o canal; os dados dele nascem sozinhos em
`perfis/<nome>/`:

```
perfis/esportes/clips.db            fila, clips, publicações e resultados
perfis/esportes/canais.json         os canais-fonte deste canal
perfis/esportes/downloads/          vídeo, áudio e transcrições
perfis/esportes/render/             os clips prontos
perfis/esportes/youtube_token.json  a autorização DESTE canal do YouTube
```

Três decisões que valem explicação:

* **Um processo por perfil.** O `settings` resolve os caminhos uma vez, no
  import, e congela; trocar de canal dentro do mesmo processo pediria
  recarregar o módulo e todos que já leram valores dele. O que sobraria seria
  um sistema em que um esquecimento publica o clip de um canal na conta do
  outro — sem desfazer. Um processo por perfil torna isso impossível por
  construção, e custa um `python` a mais por ciclo.
* **O `.env` comum fica por baixo.** O `.env.<nome>` é lido primeiro e o `.env`
  depois, e o primeiro a definir uma chave vence. Então `OPENROUTER_API_KEY` e
  `YOUTUBE_API_KEY` ficam num arquivo só, e cada `.env.<nome>` carrega apenas o
  que é daquele canal. A precedência completa é shell > `.env.<nome>` > `.env`.
* **O client secret é do projeto, o token é do canal.** O
  `client_secrets.json` do Google Cloud continua na raiz e serve a todos; o
  `youtube_token.json`, que é a autorização de um canal específico, vai para
  dentro do perfil. Para autorizar cada um:

  ```powershell
  $env:CLIPS_PERFIL = "esportes"; python -m publish.publicar --autorizar
  ```

  No bash, `CLIPS_PERFIL=esportes python -m publish.publicar --autorizar`. O
  navegador abre na conta que estiver logada — confira que é a do canal certo
  antes de autorizar, porque é essa autorização que vai para dentro do perfil.

**A quota do YouTube não se multiplica com os canais.** O teto de 10.000
unidades por dia é do PROJETO do Google Cloud: com três canais no mesmo
projeto, os seis uploads diários são divididos entre eles, não seis para cada.
Canal com projeto próprio (e `YOUTUBE_API_KEY`/`YOUTUBE_CLIENT_SECRETS`
próprios no `.env` dele) tem quota própria.

**O freio de emergência tem dois níveis.** `PARAR_PUBLICACAO` na raiz do
projeto para **todos** os canais; o mesmo arquivo dentro de `perfis/<nome>/`
para só aquele. Quem cria o arquivo no meio de um incidente quer que tudo
pare, não que pare o canal cujo nome lembrou de digitar.

Falha de um canal **não interrompe os outros**: cada perfil é um processo, e o
resumo do fim diz quem passou e quem não. `--verificar` sai com código 1 se
algum canal tiver impedimento.

## Ligando a publicação real

**O padrão é `AUTO_PUBLISH=false`.** Post público não tem desfazer, então a
chave é sua para virar. Antes:

```bash
python -m orchestrator.main_loop --verificar
```

Lista **todos** os impedimentos de uma vez, cada um com o comando ou a
variável que resolve — quem está ligando a publicação quer resolver tudo numa
sentada, não descobrir mais um item a cada execução. Sai com código 1 enquanto
houver bloqueio.

Depois de ligar, três freios independentes seguram o que a configuração
sozinha não segura:

| freio | o que cobre |
| --- | --- |
| `PARAR_PUBLICACAO` (arquivo na raiz) | emergência: bloqueia tudo na hora, sem editar `.env` nem parar processo |
| `MAX_POSTS_DIA_ABSOLUTO` | bug de agenda: o scheduler confia na própria agenda, este número não (com override por plataforma em `MAX_POSTS_DIA_ABSOLUTO_PLATAFORMA`) |
| `AQUECIMENTO_POSTS_DIA` | os primeiros dias no volume cheio, antes de ver como os clips performam |

Os três **adiam**, não descartam: o post continua `agendado` e sai quando o
freio soltar. Freio que descarta post é freio que perde clip. E a parada de
emergência é checada antes de tudo, inclusive antes do `AUTO_PUBLISH` —
emergência não negocia com configuração.

O relógio do aquecimento começa no **primeiro post que foi ao ar**, não na
data em que a flag foi ligada: ligar, esquecer uma semana e depois publicar no
volume cheio é exatamente o que ele existe para evitar.

## Publicação e modo sombra

O `publicar` faz duas coisas separadas de propósito: **agendar** (gerar o
metadado e marcar o horário) e **publicar** (processar o que venceu). O modo
sombra vive só na segunda — é o que permite olhar a fila inteira, com os
títulos e captions que realmente sairiam, antes de qualquer coisa ir ao ar.

Ao ligar a publicação real, `--reagendar-simulados` devolve o que já foi
planejado para a fila em vez de reconstruir tudo: o metadado gerado (e pago)
continua valendo.

**Quota do YouTube.** 1600 unidades por upload de um teto de 10.000 por dia —
seis uploads, num teto que é do projeto inteiro e compartilhado com o
sourcing. O detalhe que exige código próprio: o Google zera a quota à
meia-noite do **Pacífico**, não do fuso local. Contando pela data daqui, o
programa acharia que tem quota nova e gastaria uploads que o Google ainda
conta no dia anterior — todos recusados.

**Instagram.** Duas surpresas para quem chega do YouTube: não existe upload de
arquivo (a API baixa o vídeo de uma URL, daí o `CLIPS_BASE_URL`), e a
publicação é assíncrona em duas etapas — cria-se um contêiner, espera-se o
processamento, e só então publica. O token de longa duração dura ~60 dias e é
renovado pelo próprio programa: um token que morre num domingo derrubaria a
fila até alguém notar. Por isso o valor vigente vive na tabela `tokens`, não
no `.env` — segredo que o programa reescreve não cabe em arquivo que o humano
edita.

**TikTok.** Content Posting API — não é a API por trás do app de celular:
exige um app registrado em developers.tiktok.com, com os escopos
`video.publish` e `user.info.basic` para publicar, mais `video.list` para a
etapa 7 medir. Três diferenças em relação às outras duas:

* **App não revisado publica PRIVADO.** Enquanto a TikTok não aprovar a
  revisão do app, a única privacidade que a API aceita é `SELF_ONLY`: o vídeo
  sobe, fica na conta e só o dono vê. **Isso não é bug da integração** — é
  como a plataforma trata app em sandbox, e a revisão leva dias ou semanas.
  Quando for o caso, o código detecta pelo `creator_info`, rebaixa o pedido em
  vez de falhar (falhar perderia o clip por uma limitação que não se resolve
  sozinha) e avisa alto no log: `TikTok publicando em modo restrito`. O
  `--verificar` avisa antes, enquanto `TIKTOK_APP_AUDITADO=false`.
* **O access token dura ~24 h**, não 60 dias. Sem `TIKTOK_REFRESH_TOKEN` a
  fila para sozinha amanhã. Os dois valores vigentes passam a viver na tabela
  `tokens` depois da primeira renovação, pelo mesmo motivo do Instagram.
* **Aceita upload de arquivo**, ao contrário do Instagram. O padrão
  (`TIKTOK_MODO_UPLOAD=arquivo`) manda os bytes em pedaços e não depende de
  hospedagem pública; `url` reaproveita a `CLIPS_BASE_URL`, mas exige provar a
  propriedade do domínio no painel de desenvolvedor.

O TikTok chega **desligado**: `PUBLICAR_TIKTOK=true` (ou `tiktok` na lista de
`PLATAFORMAS`) é o que o liga. O clip publicado é o MESMO arquivo do Instagram
e do Shorts — 9:16, mp4, legenda queimada —, sem processamento extra. O que
diverge é só o texto: a caption do TikTok é mais curta (no feed aparecem duas
linhas) e as hashtags são outras, geradas na mesma chamada de LLM, em
`hashtags_tiktok`.

**Medido pela etapa 7** desde que o app tenha o escopo `video.list` na
autorização — o mesmo access token da publicação, outro escopo e outro
endpoint. Post que saiu privado (app não revisado) é a exceção: sem id
público não há o que consultar, e ele é pulado com aviso no log, não com
erro.

**Ritmo por plataforma.** `POSTS_POR_DIA` é o número geral e
`POSTS_POR_DIA_PLATAFORMA` (`tiktok=4,instagram=2`) manda em quem estiver
listado. Cada plataforma tem quota e rate limit próprios — a do YouTube é dura
e diária, a do TikTok conta publicações por token, a do Instagram conta
chamadas por hora —, e um número global obrigaria a mais restrita a ditar o
ritmo das outras.

**Horários.** O spec pede que venham do histórico de engajamento, com
horários padrão como fallback. Hoje o fallback é o caminho real, porque não
existe histórico: nenhum clip foi publicado. `pesos_do_historico` já existe e
devolve vazio — a etapa 7 a preenche e a ordenação por peso passa a valer sem
que mais nada mude.

## Estrutura

```
settings.py                  todo número ajustável, sobrescrevível por env var
db/schema.sql                schema do clips.db, idempotente (e só aditivo)
db/repositorio.py            acesso a dado puro (WAL, BEGIN IMMEDIATE)

sourcing/canais.py           lê canais.json
sourcing/youtube.py          YouTube Data API v3
sourcing/score.py            a fórmula (função pura)
sourcing/descobrir.py        decide o status e orquestra a varredura

pipeline/download.py         yt-dlp + extração do áudio de trabalho
pipeline/transcribe.py       faster-whisper local, e a escolha de backend
pipeline/whisper_api.py      Whisper API: compressão, fatiamento, custo
pipeline/legendas_youtube.py legenda do YouTube como transcrição (de graça)
pipeline/energia.py          picos de RMS (só carregar_audio toca o librosa)
pipeline/claude_cliente.py   fiação comum das chamadas ao Claude
pipeline/highlight_detect.py Claude sobre a transcrição
pipeline/select_clips.py     duração, energia, limiar, sobreposição
pipeline/processar.py        orquestra a fila, com retomada e orçamento

editing/template_config.json TODO parâmetro visual e sonoro, nada no código
editing/template.py          carga e validação do template
editing/legendas.py          gera o .ass (string pura, sem ffmpeg)
editing/sfx.py               decide quando cada efeito toca
editing/render.py            monta e executa o comando do ffmpeg
editing/editar.py            orquestra a fila de render

publish/metadata.py          título, descrição, caption e hashtags (Claude)
publish/scheduler.py         atribui os horários
publish/quota.py             quota diária do YouTube (dia do Pacífico)
publish/youtube.py           upload via OAuth
publish/instagram.py         Reels e renovação do token
publish/tiktok.py            Content Posting API (token de 24 h, upload em pedaços)
publish/publicar.py          agenda, publica ou simula
publish/preflight.py         confere se a publicação real pode ser ligada

orchestrator/main_loop.py    roda tudo no relógio, com falha isolada
orchestrator/perfis.py       roda o pipeline de cada canal, um processo por perfil

analytics/coletar.py         mede a performance de cada post
analytics/recalibrate.py     as quatro recalibrações
analytics/analisar.py        mede e recalibra

assets/sfx/                  os arquivos de efeito (fora do git)
assets/filler/               vídeos de preenchimento do layout split (fora do git)
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
4. ~~SFX (whoosh nos cortes, ding/pop nos picos)~~
5. ~~`publish/` em modo sombra (`AUTO_PUBLISH=false`: gera e não posta)~~
6. ~~Publicação real com `scheduler.py`, respeitando quota~~ (construída, desligada)
7. ~~`analytics/` + `recalibrate.py` — top 10% viram few-shot no prompt, canais
   ruins saem do `canais.json`~~
