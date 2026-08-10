# Biblioteca de efeitos sonoros

Os arquivos de áudio **não vêm no repositório** — são binários grandes e cada
um tem a própria licença. Este diretório é ignorado pelo git, menos este
README.

## O que colocar aqui

Os nomes vêm de `editing/template_config.json`, seção `sfx.eventos`. Com o
template que acompanha o repo:

| arquivo | quando toca |
| --- | --- |
| `whoosh.wav` | abertura do clip e virada do hook para o conteúdo |
| `ding.wav` | picos de energia do áudio, medidos na etapa 2 |
| `pop.wav` | palavras da lista `sfx.palavras_chave`, e exclamações |

`.wav` ou `.mp3`; o ffmpeg lê os dois. Se você usar outros nomes ou outros
formatos, mude `sfx.eventos.<nome>.arquivo` no template — o código não conhece
nome de arquivo nenhum.

## Como ligar

1. Ponha os arquivos aqui.
2. No `editing/template_config.json`, mude `sfx.ativo` para `true`.
3. **Suba `versao`** no mesmo arquivo. A versão vai gravada em cada render, e
   é o que permite à etapa 7 distinguir performance do trecho de performance
   do visual — sem subir, clips com e sem efeito viram a mesma série.

Com `sfx.ativo: true` e um arquivo faltando, a carga do template falha
apontando qual. É de propósito: um clip renderizado sem o som que o template
mandava é um defeito que só aparece assistindo, muito depois de a fila
inteira ter rodado.

## Escolhendo os sons

Efeito curto (150–400 ms) e com ataque limpo. Som longo se sobrepõe à fala e
some no meio dela. O `sfx.volume` do template é o padrão, e cada evento pode
sobrescrever o seu — comece baixo (0,5) e suba olhando o resultado: no celular,
com a mixagem da plataforma por cima, o efeito soa mais alto do que no fone.

Fontes gratuitas com licença permissiva: freesound.org (confira a licença de
cada arquivo), Pixabay Sound Effects, YouTube Audio Library.
