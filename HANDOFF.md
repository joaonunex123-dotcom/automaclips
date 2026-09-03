# Handoff — clips-automacao

Estado em 31/08/2026. Este documento é para quem chega (você daqui a três
semanas, ou outra sessão): o que existe, o que foi decidido e por quê, o que
falta, e onde estão as armadilhas.

O **README.md** explica como o sistema funciona. Aqui está o que o README não
diz: o estado desta instalação, as decisões que custaram discussão e o que
ainda não foi provado contra o mundo real.

---

## 1. Onde o projeto está

Pipeline de clips verticais, sete etapas entregues, rodando sozinho no
relógio. Descobre vídeos em alta nos canais monitorados, corta os melhores
trechos, edita com legenda e SFX, publica em três plataformas e recalibra a
seleção com o que performou.

**A publicação real nunca foi ligada.** `AUTO_PUBLISH=false` é o padrão e
continua assim: nenhum post saiu, nenhum clip foi renderizado em produção,
nenhuma chamada real foi feita a YouTube, Instagram ou TikTok.

| | estado |
| --- | --- |
| Etapas 1–7 | entregues, 800 testes passando |
| Plataformas | YouTube Shorts, Instagram Reels, TikTok |
| Canais de destino | 3 perfis criados (`cortes-podcast`, `motivacional`, `politico`) |
| Canais-fonte | 21 canais com id verificado, distribuídos nos 3 perfis |
| Credenciais | **nenhuma preenchida** — é o que trava tudo |
| Publicação | desligada, e o `--verificar` lista o que falta |

---

## 2. O que a última sessão fez

Seis commits, todos em `main` e no GitHub
(`joaonunex123-dotcom/automaclips`). A suíte saiu de 671 para 800 testes.

| commit | o que entrou |
| --- | --- |
| `726639c` | TikTok como terceiro destino de publicação |
| `dac6302` | coleta de métricas do TikTok na etapa 7 |
| `c5f27e2` | coluna `compartilhamentos` + a porta estreita do `ADD COLUMN` |
| `374b7fa` | share do Instagram, sem arriscar as views |
| `42bbedc` | perfis: um canal por perfil, um processo por canal |
| `cea2c20` | comando de autorização na sintaxe do PowerShell |

Fora do git (é segredo e dado de canal, e o `.gitignore` cobre):

- `.env` com as chaves comuns, em branco;
- `.env.cortes-podcast`, `.env.motivacional`, `.env.politico`;
- `perfis/<canal>/canais.json` com os canais-fonte de cada tópico.

---

## 3. Como está configurado

Cada canal é um **perfil**: `.env.<nome>` na raiz define, `perfis/<nome>/`
guarda os dados. Um processo por perfil, sem estado compartilhado.

```
.env                          chaves comuns (LLM, API do YouTube, AUTO_PUBLISH)
.env.cortes-podcast           credenciais e ritmo deste canal
.env.motivacional
.env.politico

perfis/cortes-podcast/
  canais.json                 7 canais-fonte (6 ativos)
  clips.db                    fila, clips, publicações, resultados
  downloads/  render/         nascem na primeira execução
  youtube_token.json          a autorização DESTE canal (ainda não existe)
```

### Ritmo, por canal

| | cortes-podcast | motivacional | politico |
| --- | --- | --- | --- |
| horários | 12:00, 18:00, 21:00 | 07:00, 13:00, 19:00 | 09:00, 15:00, 20:00 |
| YouTube/dia | 2 | 2 | 2 |
| Instagram e TikTok/dia | 3 | 3 | 3 |
| janela do vídeo-fonte | 72 h | 72 h | **36 h** |
| uploads olhados por canal | 20 | 20 | **50** |

Os horários são **escalonados de propósito**: os três canais dividem a mesma
quota do YouTube e rodam no mesmo ciclo. Caindo no mesmo minuto, o primeiro
perfil consumiria a quota e os outros dois ficariam sem upload no dia.

O político tem janela mais curta e varredura mais larga porque debate
envelhece em horas e canal de notícia publica dezenas de vídeos por dia — com
20 uploads olhados, o debate da noite anterior sairia da janela antes de ser
visto.

### Canais-fonte

- **cortes-podcast** (6 ativos): Flow Podcast, Inteligência Ltda, PodpahTV,
  Os Sócios, Ticaracaticast, PrimoCast. Um desligado: o handle `@PODDELAS`
  resolve para um canal pessoal ("Thaise Estaniecki Ramos") — confirme se é
  onde o podcast sai antes de ligar.
- **motivacional** (7): Joel Jota, Caio Carneiro, Flávio Augusto, O Primo
  Rico, Geronimo Theml, Tiago Brunet, TEDx Talks. O TEDx é global e traz
  palestra em inglês junto; o metadado sai no idioma da fala, então funciona,
  mas desligue se quiser só pt-BR.
- **politico** (8): Roda Viva, Jovem Pan News, CNN Brasil, Band Jornalismo,
  UOL, Folha, TV Cultura, SBT News. Linhas editoriais diferentes de
  propósito; a curadoria é do João, não é endosso de nenhuma.

Todos os ids foram resolvidos no próprio YouTube com o `yt-dlp` (o mesmo que o
pipeline usa), a partir do `@handle`. Nenhum foi digitado de memória — id
errado faria o sourcing monitorar o canal errado em silêncio. Três handles
chutados não existiam e foram descartados em vez de virar entrada quebrada.

---

## 4. O caminho para ligar, em ordem

Cada passo é verificável pelo `--verificar`, que lista **todos** os
impedimentos de uma vez, por canal.

### 4.1 Chaves comuns — `.env`

| chave | onde tirar | sem ela |
| --- | --- | --- |
| `YOUTUBE_API_KEY` | console.cloud.google.com → Credenciais, com a *YouTube Data API v3* habilitada | o sourcing não roda: nada entra na fila |
| `ANTHROPIC_API_KEY` | console.anthropic.com | o `highlight_detect` não escolhe trecho |
| `OPENROUTER_API_KEY` | openrouter.ai | o metadado não é gerado, e **nada chega a ser agendado** |
| `OPENAI_API_KEY` | opcional — só se `TRANSCRIBE_BACKEND=api` | usa o Whisper local ou a legenda do YouTube |

### 4.2 Credenciais por canal — `.env.<canal>`

**YouTube.** O `client_secrets.json` do Google Cloud fica na raiz e serve aos
três; o token é de cada canal. Autorize um por um, conferindo qual conta está
logada no navegador:

```powershell
$env:CLIPS_PERFIL = "cortes-podcast"; python -m publish.publicar --autorizar
```

**Instagram.** `INSTAGRAM_USER_ID` e `INSTAGRAM_TOKEN_INICIAL` são da conta
daquele canal; `INSTAGRAM_APP_ID`/`SECRET` podem ser os mesmos nos três.
`CLIPS_BASE_URL` precisa apontar para a pasta render **daquele** canal — a API
baixa o vídeo por HTTP e não aceita upload de arquivo.

**TikTok.** Um app em developers.tiktok.com com os escopos `video.publish`,
`user.info.basic` e **`video.list`** (este último é o que permite a etapa 7
medir). `CLIENT_KEY`/`SECRET` podem ser os mesmos nos três; access e refresh
token são por conta.

### 4.3 Conferir

```bash
python -m orchestrator.perfis --verificar
```

Sai com código 1 enquanto houver bloqueio em qualquer canal.

### 4.4 Modo sombra primeiro

Com as chaves preenchidas e `AUTO_PUBLISH=false`, rode um ciclo:

```bash
python -m orchestrator.perfis --uma-vez
```

O pipeline roda inteiro — descobre, baixa, transcreve, corta, renderiza, gera
metadado, marca horário — e **para na porta da publicação**, marcando cada
item como `simulado`. É aqui que se olha a fila inteira, com os títulos e as
captions que realmente sairiam, antes de qualquer coisa ir ao ar.

### 4.5 Ligar, um canal de cada vez

`AUTO_PUBLISH=true` no `.env` daquele canal (não no comum), e
`--reagendar-simulados` devolve o que já foi planejado para a fila, em vez de
reconstruir tudo: o metadado já gerado (e pago) continua valendo.

Três freios seguram o que a configuração não segura, e os três **adiam** em
vez de descartar: o arquivo `PARAR_PUBLICACAO`, o teto absoluto por dia e por
plataforma, e o aquecimento (1 post/dia nos primeiros 3 dias, contados do
primeiro post que foi ao ar).

---

## 5. Armadilhas — leia antes de concluir que algo quebrou

**O TikTok vai publicar privado, e isso não é bug.** Enquanto o app não passar
pela revisão da TikTok, a única privacidade que a API aceita é `SELF_ONLY`: o
vídeo sobe, fica na conta, e só o dono vê. O código detecta pelo
`creator_info`, rebaixa o pedido em vez de falhar e avisa alto no log
(`TikTok publicando em modo restrito`). A revisão leva dias ou semanas. Quando
sair: `TIKTOK_APP_AUDITADO=true` e `TIKTOK_PRIVACIDADE=PUBLIC_TO_EVERYONE`.

**O access token do TikTok dura ~24 h.** Não 60 dias como o do Instagram. Sem
`TIKTOK_REFRESH_TOKEN` a fila para sozinha no dia seguinte. O `--verificar`
avisa como aviso, não como bloqueio — o post de hoje sai; o de amanhã é que
não.

**A quota do YouTube não se multiplica com os canais.** O teto de 10.000
unidades/dia é do **projeto** do Google Cloud, e cada upload custa 1.600 — seis
por dia no projeto inteiro, divididos entre os três canais. Se algum canal
ganhar projeto próprio, apague o `POSTS_POR_DIA_PLATAFORMA` do `.env` dele.

**O dia da quota é o do Pacífico**, não o daqui. Contando pela data local, o
programa acharia que tem quota nova e gastaria uploads que o Google ainda
conta no dia anterior — todos recusados.

**`CLIPS_BASE_URL` é por canal.** Cada perfil tem sua pasta `render/`. Uma URL
apontando para a pasta de outro canal faria o Instagram baixar o clip errado.

**O freio de emergência tem dois níveis.** `PARAR_PUBLICACAO` na raiz para
todos os canais; o mesmo arquivo dentro de `perfis/<nome>/` para só aquele.

**PowerShell não é bash.** `CLIPS_PERFIL=x comando` não define variável no
PowerShell — é `$env:CLIPS_PERFIL = "x"; comando`. Um `--autorizar` rodado sem
o perfil grava o token na raiz, que é a autorização do canal errado no lugar
errado.

**Post privado do TikTok não é medível.** Sem id público não há o que
consultar, e o que fica gravado é o `publish_id`. A etapa 7 separa esses
antes de chamar a API e diz no log; não é falha de coleta.

**Sessões concorrentes na mesma pasta já causaram reversão silenciosa** neste
diretório (ver o `CLAUDE.md` do `fitsa-automacao`, que é outro repositório mas
descreve o mesmo padrão). Se um teste falhar de um jeito que não bate com o
que você acabou de fazer, confira o arquivo antes de "consertar".

---

## 6. O que NÃO foi validado

Isto é o que mais importa saber antes do primeiro post real.

- **Nenhuma chamada real a nenhuma das três APIs de publicação.** Os 800
  testes rodam contra duplos, sem rede. Endpoints, nomes de campo e limites do
  TikTok e do Instagram saem da documentação e estão em `settings` justamente
  para serem corrigidos sem mexer no código. Confira contra a documentação
  vigente antes de ligar.
- **O pipeline nunca rodou de ponta a ponta com material real** nesta
  instalação: sem `YOUTUBE_API_KEY`, o sourcing nunca varreu.
- **A etapa 7 nunca mediu nada**, porque nada foi publicado. As quatro
  recalibrações estão implementadas e todas têm mínimo de amostras — abaixo
  dele elas simplesmente não acontecem, e o default do `settings` continua
  valendo.
- **Os defaults de score e threshold são palpite calibrado no escuro.** O
  número certo depende do tamanho dos canais monitorados, e é a etapa 7 que
  substitui palpite por medição.

---

## 7. Pendências conhecidas

| o quê | onde | nota |
| --- | --- | --- |
| Revisão do app do TikTok | developers.tiktok.com | trava o post público; leva dias ou semanas |
| Escopo `video.list` na autorização | TikTok | sem ele a publicação funciona e a medição não |
| `@PODDELAS` | `perfis/cortes-podcast/canais.json` | desligado até confirmar se é o canal certo |
| TEDx Talks traz palestra em inglês | `perfis/motivacional/canais.json` | funciona; desligue se quiser só pt-BR |
| `reach` do Instagram | `analytics/coletar.py` | é pedido e descartado: não há coluna que o guarde |
| Retenção do YouTube | `ANALYTICS_RETENCAO` | exige outro escopo de OAuth; desligada por padrão |
| Direitos sobre o material de origem | — | o projeto recorta vídeo de terceiros e republica. As regras de cada plataforma sobre conteúdo reaproveitado, e o que cada canal-fonte permite, são decisão sua — o código não trata disso |

---

## 8. Comandos

```bash
python -m orchestrator.perfis --listar        # os canais configurados
python -m orchestrator.perfis --verificar     # o que falta em cada um
python -m orchestrator.perfis --uma-vez       # um ciclo em cada canal
python -m orchestrator.perfis --uma-vez --perfil politico

python -m pytest                              # 800 testes, sem rede
python -m analytics.analisar --simular        # o que a etapa 7 mudaria
python -m publish.publicar --reagendar-simulados
```

Um canal específico, por dentro (PowerShell):

```powershell
$env:CLIPS_PERFIL = "politico"; python -m publish.publicar --autorizar
```

---

## 9. Mapa do que foi escrito na última sessão

```
publish/tiktok.py            Content Posting API: token de 24 h, upload em
                             pedaços, e o rebaixamento para SELF_ONLY
publish/metadata.py          caption e hashtags por plataforma no mesmo JSON
publish/scheduler.py         teto de posts por dia POR PLATAFORMA
publish/publicar.py          texto_do_post() por plataforma, enviador do TikTok
publish/preflight.py         checagens do TikTok, parada em dois níveis
analytics/coletar.py         métricas do TikTok e share do Instagram
db/schema.sql                coluna compartilhamentos, e a regra do ADD COLUMN
db/repositorio.py            aplicar_colunas_novas(), pasta do perfil
settings.py                  PERFIL, raiz_de_dados(), caminhos por perfil
orchestrator/perfis.py       um processo por canal
```
