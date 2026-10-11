# Vídeos de preenchimento (layout `split`)

No layout `split` o clip ocupa a parte de cima da tela (60% por padrão) e um
vídeo de preenchimento ocupa a de baixo (40%), **sem áudio** — o som é sempre
o do clip. Os arquivos **não vêm no repositório** e **o sistema nunca baixa
nada**: você os fornece. Este diretório é ignorado pelo git, menos este README.

## Estrutura

```
assets/filler/
  <categoria>/          # o nome da categoria é o que aparece na variante
    qualquer-nome.mp4
    outro.mp4
```

A categoria `parkour` é usada pela variante `split:parkour`. Várias categorias
são variantes diferentes (`split:parkour`, `split:satisfying`, ...).

## Formato

- `.mp4`, `.mov`, `.mkv`, `.webm` ou `.m4v`; qualquer codec que o ffmpeg leia.
- Qualquer resolução e proporção: o render escala para cobrir a faixa
  (1080 x ~768) e corta ao centro. Prefira 16:9 ou vertical, nítido e sem
  marca d'água ou legenda própria.
- Qualquer duração. Para cada clip o sistema escolhe **um arquivo e um trecho
  aleatórios** com a duração do clip; arquivo mais curto que o clip entra em
  loop. Vídeos longos (minutos) dão mais variedade entre clips.
- O áudio do arquivo é descartado.

## Como ligar

Em `editing/template_config.json`:

```json
"variantes": { "pesos": { "full": 1, "split:parkour": 1 }, "semente": "ab1" }
```

Ou, para todo clip em split: `"layout": "split"` e `"split.categoria": "parkour"`.
Proporção, posição da legenda e separador estão em `split`.

Categoria sem arquivos: a variante é **ignorada** (com aviso no log) e os clips
saem nas variantes que sobraram. Nada é baixado.

## Licença — leia antes de usar

O preenchimento é publicado junto com o seu clip, nos seus canais. Use somente
material que **você tem o direito de republicar**: gravação própria, conteúdo
com licença que permita uso comercial e redistribuição (confira atribuição
obrigatória), ou material licenciado a você. Vídeo de gameplay, parkour ou
"satisfying" baixado de terceiros costuma ter direitos reservados e pode gerar
bloqueio de monetização, strike ou remoção. A responsabilidade pelo que entra
nesta pasta é sua; o repositório não verifica licença nenhuma.
