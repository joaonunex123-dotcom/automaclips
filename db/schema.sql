-- Schema do clips.db — fonte de verdade do pipeline de clips.
--
-- Aplicado por db/repositorio.conectar() via executescript: tudo aqui é
-- IF NOT EXISTS / INSERT OR IGNORE, então rodar de novo sobre um banco
-- existente é inofensivo.
--
-- Convenção de tempo, e a distinção importa:
--   publicado_em    ISO 8601 em UTC, exatamente como a API da plataforma
--                   devolve ('2026-08-10T14:23:45+00:00'). É a única data
--                   comparável entre canais de fusos diferentes, e é a que
--                   entra na conta do score.
--   *_em de controle (descoberto_em, atualizado_em, observado_em)
--                   ISO local, preenchido pelo próprio SQLite. Serve para o
--                   humano ler o log e para ordenar eventos da mesma máquina;
--                   nunca entra em cálculo.
--
-- Este arquivo cobre as sete etapas. Cada bloco abaixo foi acrescentado
-- quando havia código que o escrevesse — nada de schema adivinhado — e
-- sempre de forma ADITIVA: aplicar sobre um banco antigo é inofensivo.
--
-- Aditivo aqui quer dizer duas coisas, e só estas duas:
--
--   1. CREATE TABLE / CREATE INDEX, sempre IF NOT EXISTS. Tabela que já
--      existe não é tocada.
--   2. Coluna NOVA numa tabela que já existe, declarada em
--      db/repositorio.COLUNAS_ACRESCENTADAS e aplicada por ADD COLUMN quando
--      falta. O CREATE TABLE aqui embaixo já nasce com ela, então banco novo
--      nunca passa pelo ADD COLUMN — ele existe só para o banco que foi
--      criado antes da coluna.
--
-- O que continua fora: renomear coluna, mudar tipo, apagar coluna, mexer em
-- dado existente. Qualquer uma dessas deixaria de ser inofensiva sobre um
-- banco em uso, e aí a conversa passa a ser sobre sistema de migração de
-- verdade.

-- Um vídeo-FONTE descoberto num canal monitorado. Não é o clip cortado: é o
-- material bruto de onde os clips da etapa 2 vão sair.
CREATE TABLE IF NOT EXISTS fila_clips (
    id           INTEGER PRIMARY KEY,
    -- 'youtube' hoje; a coluna existe para que um segundo sourcing (Twitch)
    -- não colida com IDs de vídeo do YouTube, que não são globalmente únicos.
    plataforma   TEXT NOT NULL DEFAULT 'youtube',
    video_id     TEXT NOT NULL,
    canal_id     TEXT NOT NULL,
    canal_nome   TEXT NOT NULL DEFAULT '',
    titulo       TEXT NOT NULL DEFAULT '',
    url          TEXT NOT NULL DEFAULT '',
    publicado_em TEXT NOT NULL,
    duracao_s    INTEGER NOT NULL DEFAULT 0,

    -- Views na ÚLTIMA observação, não um acumulado próprio: é o valor lido da
    -- API na varredura mais recente. O histórico completo fica em
    -- observacoes_video; esta coluna é o atalho para não precisar de subquery
    -- só para saber onde o vídeo estava.
    views        INTEGER NOT NULL DEFAULT 0,

    -- Score da última observação = views ganhas desde a observação anterior,
    -- dividido pelas horas desde a PUBLICAÇÃO (ver sourcing/score.py para a
    -- definição completa e os casos de borda). Guardado como o valor calculado
    -- e não recalculado na leitura: o score depende de "agora", então um mesmo
    -- registro daria número diferente a cada SELECT.
    score        REAL NOT NULL DEFAULT 0,

    -- Vocabulário em db/repositorio.py (STATUS_*). Na etapa 1 só existem:
    --   'descoberto'        passou no threshold, aguardando a etapa 2
    --   'abaixo_do_limiar'  observado e pontuado, mas fora do corte
    --   'ignorado'          fora da faixa de duração ou de idade
    -- As etapas seguintes acrescentam os estados de processamento.
    --
    -- Por que gravar o que ficou fora do corte, em vez de simplesmente não
    -- inserir: o score precisa de "views ganhas desde a última observação", e
    -- não existe observação anterior de um vídeo que nunca foi gravado. Sem
    -- esta linha, um vídeo que engata no segundo dia seria pontuado de novo
    -- como se fosse a primeira vez — e ou o momento passa despercebido, ou o
    -- vídeo é reavaliado do zero para sempre.
    status       TEXT NOT NULL DEFAULT 'descoberto',

    -- Última falha de processamento, texto livre. NULL = nunca falhou.
    erro         TEXT,

    descoberto_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),

    -- O mesmo vídeo não pode virar duas linhas: reencontrá-lo numa varredura
    -- seguinte é UPDATE + nova observação, nunca um INSERT paralelo. É o que
    -- torna a varredura segura de rodar a cada 6 h sem deduplicar na mão.
    UNIQUE (plataforma, video_id)
);

-- Toda seleção de trabalho começa por "o que está descoberto, do melhor score
-- para o pior".
CREATE INDEX IF NOT EXISTS ix_fila_clips_status_score
    ON fila_clips (status, score DESC);

-- Histórico de views por vídeo — uma linha por varredura que o observou.
--
-- É esta tabela que torna "views GANHAS" possível: sem histórico, a única
-- métrica disponível seria o total de views, que é exatamente o que o score
-- não deve ser (um vídeo de 5 milhões de views de 2023 ganharia de qualquer
-- vídeo de ontem).
--
-- Nunca sofre UPDATE: cada observação é um fato datado. Se a API revisar o
-- número de views para baixo, a linha antiga continua registrando o que foi
-- lido naquele momento.
CREATE TABLE IF NOT EXISTS observacoes_video (
    id           INTEGER PRIMARY KEY,
    fila_clip_id INTEGER NOT NULL REFERENCES fila_clips(id),
    views        INTEGER NOT NULL,
    -- Views ganhas desde a observação anterior deste mesmo vídeo. Na primeira
    -- observação é o total de views (a "observação anterior" implícita é a
    -- publicação, com zero views).
    ganho        INTEGER NOT NULL DEFAULT 0,
    score        REAL NOT NULL DEFAULT 0,
    observado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- A leitura quente é sempre "a última observação deste vídeo".
CREATE INDEX IF NOT EXISTS ix_observacoes_video_clip
    ON observacoes_video (fila_clip_id, id DESC);

-- Mantém atualizado_em correto para QUALQUER update de fila_clips, inclusive
-- um vindo de SQL cru, sem depender de cada UPDATE lembrar de setá-lo. O WHEN
-- evita recursão infinita: só dispara quando o próprio UPDATE não mexeu na
-- coluna.
CREATE TRIGGER IF NOT EXISTS trg_fila_clips_atualizado_em
AFTER UPDATE ON fila_clips
FOR EACH ROW
WHEN NEW.atualizado_em = OLD.atualizado_em
BEGIN
    UPDATE fila_clips
    SET atualizado_em = datetime('now', 'localtime')
    WHERE id = NEW.id;
END;

-- ============================================================================
-- Etapa 2 — pipeline (download -> transcrição -> highlight -> seleção)
-- ============================================================================

-- Artefatos de mídia de um vídeo-fonte. Tabela SEPARADA de fila_clips, e não
-- colunas novas lá, por dois motivos:
--   1. mantém este schema puramente aditivo — só CREATE TABLE IF NOT EXISTS,
--      nenhum ALTER TABLE, então aplicar sobre um banco da etapa 1 continua
--      sendo inofensivo e não exige um sistema de migração ainda;
--   2. a relação é 1:1 mas OPCIONAL: a maioria das linhas de fila_clips nunca
--      será baixada (ficou abaixo do limiar), e não faz sentido carregar
--      quatro colunas vazias em todas elas.
--
-- Os caminhos são absolutos e apontam para fora do git (ver .gitignore). Vazio
-- = etapa ainda não rodou; o pipeline usa isso para saber onde retomar depois
-- de uma falha, em vez de refazer o download.
CREATE TABLE IF NOT EXISTS midia (
    fila_clip_id     INTEGER PRIMARY KEY REFERENCES fila_clips(id),
    video_path       TEXT NOT NULL DEFAULT '',
    audio_path       TEXT NOT NULL DEFAULT '',
    transcricao_path TEXT NOT NULL DEFAULT '',
    -- Duração medida no arquivo baixado. Pode divergir de fila_clips.duracao_s,
    -- que veio da API: é ESTA que vale para recortar, porque é a do arquivo que
    -- o ffmpeg vai cortar.
    duracao_real_s   REAL NOT NULL DEFAULT 0,
    baixado_em       TEXT,
    transcrito_em    TEXT
);

-- Um trecho candidato a virar clip vertical. Sai do highlight_detect (Claude
-- sobre a transcrição) e é confirmado contra os picos de energia do áudio.
CREATE TABLE IF NOT EXISTS clips (
    id           INTEGER PRIMARY KEY,
    fila_clip_id INTEGER NOT NULL REFERENCES fila_clips(id),

    -- Offsets em segundos dentro do vídeo-FONTE, não do clip.
    inicio_s     REAL NOT NULL,
    fim_s        REAL NOT NULL,

    -- Nota do Claude, 0–10 (ver pipeline/highlight_detect.py). Escala de dez
    -- em vez de 0–1 porque LLM pontua de forma mais estável e mais separável
    -- numa faixa inteira pequena do que numa fração decimal.
    score_claude REAL NOT NULL,
    -- Justificativa em texto livre. Não participa de nenhum cálculo: existe
    -- para o humano auditar por que um trecho entrou, que é a única forma de
    -- calibrar o prompt sem adivinhar.
    motivo       TEXT NOT NULL DEFAULT '',
    -- Frase de abertura extraída pelo Claude, usada na intro de 1 s da etapa 3.
    -- Pedida JUNTO com o trecho porque aqui a transcrição inteira já está no
    -- contexto — extraí-la depois custaria uma segunda chamada à API por clip.
    hook_text    TEXT NOT NULL DEFAULT '',

    -- Confirmação por áudio: quantos picos de energia (librosa) caem dentro do
    -- trecho, e o score depois de aplicado o fator derivado deles
    -- (pipeline/select_clips.py). É score_final que ordena a fila de edição.
    picos_energia INTEGER NOT NULL DEFAULT 0,
    score_final   REAL NOT NULL,

    -- 'selecionado' | 'descartado'. Trecho abaixo do limiar é GRAVADO como
    -- descartado, não some: sem a linha não há como comparar o que o prompt
    -- rejeitou contra o que performou, que é a matéria-prima da etapa 7.
    status       TEXT NOT NULL DEFAULT 'selecionado',
    -- Por que foi descartado (limiar, sobreposição, duração). Vazio se entrou.
    motivo_descarte TEXT NOT NULL DEFAULT '',

    criado_em    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),

    -- Reprocessar o mesmo vídeo não pode duplicar trechos. O par
    -- (vídeo, início) é a identidade do trecho: o Claude pode devolver um fim
    -- ligeiramente diferente numa segunda passada, mas o mesmo início é o
    -- mesmo momento do vídeo.
    UNIQUE (fila_clip_id, inicio_s)
);

CREATE INDEX IF NOT EXISTS ix_clips_status_score
    ON clips (status, score_final DESC);
CREATE INDEX IF NOT EXISTS ix_clips_fila
    ON clips (fila_clip_id);

-- Instantes dos picos de energia DENTRO de cada trecho, relativos ao início
-- dele.
--
-- `clips.picos_energia` guarda quantos são, que é tudo de que a seleção
-- precisa; a etapa 4 precisa saber ONDE eles caem, para colocar o efeito
-- sonoro em cima. Recalcular na hora do render custaria carregar o áudio
-- inteiro do vídeo-fonte (quase um gigabyte, num podcast de quatro horas) uma
-- vez por clip.
--
-- Guardados por CLIP e não por vídeo de propósito: um vídeo de quatro horas
-- tem milhares de picos e só uns poucos minutos viram clip. Por clip são
-- algumas dezenas de linhas, e são exatamente as que alguém vai usar.
--
-- ON DELETE CASCADE porque registrar_clips substitui os trechos de um vídeo a
-- cada reprocessamento: sem o cascade, os picos do trecho antigo ficariam
-- órfãos apontando para um clip que não existe mais.
CREATE TABLE IF NOT EXISTS picos_clip (
    id         INTEGER PRIMARY KEY,
    clip_id    INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    instante_s REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_picos_clip ON picos_clip (clip_id, instante_s);

-- Gasto com API paga, uma linha por chamada cobrada.
--
-- Existe porque o orçamento é pequeno e finito: sem o acumulado no banco, a
-- única forma de saber quanto sobrou é abrir o painel do provedor, e o
-- pipeline roda sozinho. É esta soma que db/repositorio.custo_acumulado lê
-- para RECUSAR uma chamada antes de fazê-la, em vez de descobrir o saldo
-- estourado no meio da fila.
--
-- `quantidade` é a unidade que o provedor cobra (minutos de áudio, para o
-- Whisper), guardada junto para o custo poder ser reconferido contra a tabela
-- de preços vigente depois — o preço unitário muda, o consumo não.
CREATE TABLE IF NOT EXISTS custos (
    id           INTEGER PRIMARY KEY,
    servico      TEXT NOT NULL,
    referencia   TEXT NOT NULL DEFAULT '',
    quantidade   REAL NOT NULL DEFAULT 0,
    unidade      TEXT NOT NULL DEFAULT '',
    custo_usd    REAL NOT NULL,
    registrado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_custos_servico ON custos (servico, registrado_em);

-- ============================================================================
-- Etapa 3 — editing
-- ============================================================================

-- O arquivo renderizado de um clip.
--
-- Tabela separada, e não uma coluna `status = 'renderizado'` em clips, porque
-- as duas coisas respondem perguntas diferentes: `clips.status` é o VEREDITO
-- da seleção (entrou ou não entrou), e o render é um ARTEFATO que pode ser
-- refeito com um template novo sem que o veredito mude. Com a presença da
-- linha valendo como "já renderizado", refazer é apagar o arquivo e a linha —
-- não reabrir uma máquina de estados.
CREATE TABLE IF NOT EXISTS renders (
    clip_id         INTEGER PRIMARY KEY REFERENCES clips(id),
    caminho         TEXT NOT NULL,
    -- Versão do template_config.json usada. É o que permite saber, na etapa 7,
    -- se a diferença de performance entre dois clips veio do trecho ou do
    -- visual — sem isso, uma mudança de template contamina a série histórica
    -- inteira sem deixar rastro.
    template_versao TEXT NOT NULL DEFAULT '',
    duracao_s       REAL NOT NULL DEFAULT 0,
    renderizado_em  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================================
-- Etapa 5 — publish (modo sombra)
-- ============================================================================

-- Uma publicação planejada de um clip numa plataforma.
--
-- A linha nasce em 'agendado' e o horário é decidido pelo scheduler; quem
-- publica só procura o que já venceu. Separar AGENDAR de PUBLICAR é o que
-- permite o modo sombra: a fila inteira é montada de verdade, com metadado
-- gerado e horário atribuído, e só a chamada à plataforma fica de fora.
--
-- Estados:
--   agendado   tem horário, esperando a hora chegar
--   simulado   a hora chegou com AUTO_PUBLISH=false — tudo pronto, nada
--              enviado. É o estado normal de todo o modo sombra.
--   publicado  saiu de verdade; id_externo e url preenchidos
--   falha      tentou e não foi; motivo em `erro`
CREATE TABLE IF NOT EXISTS publicacoes (
    id            INTEGER PRIMARY KEY,
    clip_id       INTEGER NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
    plataforma    TEXT NOT NULL,

    -- Metadado gerado pelo LLM. `hashtags` é uma lista JSON: guardar o texto
    -- já concatenado impediria a etapa 7 de correlacionar performance com
    -- hashtag individual, que é a pergunta óbvia a se fazer depois.
    titulo        TEXT NOT NULL DEFAULT '',
    descricao     TEXT NOT NULL DEFAULT '',
    hashtags      TEXT NOT NULL DEFAULT '[]',

    agendado_para TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'agendado',

    id_externo    TEXT NOT NULL DEFAULT '',
    url           TEXT NOT NULL DEFAULT '',
    erro          TEXT,

    criado_em     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    publicado_em  TEXT,

    -- O mesmo clip não pode ser agendado duas vezes na mesma plataforma. Em
    -- plataformas diferentes pode e deve — é o mesmo clip rendendo duas vezes.
    UNIQUE (clip_id, plataforma)
);
CREATE INDEX IF NOT EXISTS ix_publicacoes_agenda
    ON publicacoes (plataforma, status, agendado_para);

-- Consumo de quota de API por dia, para não descobrir o teto estourado no
-- meio de um upload.
--
-- `dia` NÃO é a data local: o YouTube zera a quota à meia-noite do Pacífico.
-- Usar a data daqui faria o contador virar em outro momento do que o teto de
-- verdade — em parte do ano com três horas de diferença, o suficiente para
-- gastar de manhã uma quota que o Google ainda contava como de ontem.
-- Ver publish/quota.py.
CREATE TABLE IF NOT EXISTS quota_api (
    servico   TEXT NOT NULL,
    dia       TEXT NOT NULL,
    unidades  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (servico, dia)
);

-- Credenciais que EXPIRAM e se renovam sozinhas — hoje, o token de longa
-- duração do Instagram (60 dias).
--
-- Não vai no .env com o resto das chaves porque não é constante: o valor muda
-- em runtime, e um segredo que o programa reescreve não cabe num arquivo que
-- o humano edita. As chaves fixas continuam no .env; aqui fica só o que gira.
CREATE TABLE IF NOT EXISTS tokens (
    servico       TEXT PRIMARY KEY,
    token         TEXT NOT NULL,
    expira_em     TEXT,
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- Qual modelo produziu cada saída de LLM.
--
-- Existe porque o projeto passou a usar modelos DIFERENTES por etapa (Claude
-- escolhe os trechos, modelos mais baratos escrevem o metadado) e porque o
-- fallback pode trocar o modelo no meio de uma chamada. Sem registro, a etapa
-- 7 compararia performance de clips sem saber que metade foi escrita por um
-- modelo e metade por outro — e atribuiria ao trecho uma diferença que era do
-- texto.
--
-- `modelo_respondeu` pode divergir de `modelo_pedido`: o OpenRouter roteia
-- para variantes, e o fallback entra quando a resposta vem malformada. É o
-- que respondeu que conta.
CREATE TABLE IF NOT EXISTS geracoes_llm (
    id               INTEGER PRIMARY KEY,
    etapa            TEXT NOT NULL,
    referencia       TEXT NOT NULL DEFAULT '',
    modelo_pedido    TEXT NOT NULL,
    modelo_respondeu TEXT NOT NULL DEFAULT '',
    usou_fallback    INTEGER NOT NULL DEFAULT 0,
    tokens_entrada   INTEGER,
    tokens_saida     INTEGER,
    registrado_em    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_geracoes_etapa
    ON geracoes_llm (etapa, registrado_em);

-- ============================================================================
-- Etapa 7 — analytics
-- ============================================================================

-- Uma MEDIÇÃO da performance real de um clip publicado.
--
-- Histórico, não estado: cada coleta anexa uma linha, como observacoes_video.
-- Um clip medido só uma vez não diz se cresceu ou estagnou, e é exatamente
-- essa diferença que separa um clip que pegou de um que teve um pico de
-- notificação e morreu.
--
-- Denormalizada de propósito, ao contrário do resto do schema. `score_previsto`
-- e os dados do trecho são um INSTANTÂNEO do momento da medição: reprocessar um
-- vídeo apaga e recria as linhas de `clips` (registrar_clips), então buscar o
-- score por JOIN meses depois traria o score recalibrado de hoje, não o que a
-- seleção realmente apostou. Comparar previsão com resultado exige que a
-- previsão fique congelada.
CREATE TABLE IF NOT EXISTS resultados (
    id             INTEGER PRIMARY KEY,
    publicacao_id  INTEGER NOT NULL REFERENCES publicacoes(id) ON DELETE CASCADE,
    clip_id        INTEGER NOT NULL,
    plataforma     TEXT NOT NULL,

    -- A fonte, para descobrir canal que rende e canal que não rende.
    canal_id_fonte TEXT NOT NULL DEFAULT '',
    canal_fonte    TEXT NOT NULL DEFAULT '',

    -- O trecho, para a recalibração de duração.
    trecho_inicio_s  REAL NOT NULL DEFAULT 0,
    trecho_duracao_s REAL NOT NULL DEFAULT 0,

    -- O que a seleção previu, congelado.
    score_previsto REAL NOT NULL DEFAULT 0,

    -- O que aconteceu de verdade.
    views          INTEGER NOT NULL DEFAULT 0,
    likes          INTEGER NOT NULL DEFAULT 0,
    comentarios    INTEGER NOT NULL DEFAULT 0,
    -- Compartilhamento é o sinal mais forte do TikTok: quem manda um clip
    -- para alguém está fazendo a distribuição que o algoritmo cobra. Fica 0
    -- onde a plataforma não informa — o YouTube não expõe o número no
    -- videos.list, e o Instagram exigiria outra métrica de insights.
    compartilhamentos INTEGER NOT NULL DEFAULT 0,
    -- Fração média assistida (0–1). NULL é o normal: exige a YouTube Analytics
    -- API, que é outro escopo de OAuth — ver settings.ANALYTICS_RETENCAO.
    -- A recalibração de duração degrada para views/hora quando falta.
    retencao       REAL,

    -- Idade do post no momento da medição. É o denominador do desempenho:
    -- ranquear por views cruas premiaria post antigo, o mesmo erro que o score
    -- de sourcing existe para evitar.
    horas_publicado REAL NOT NULL DEFAULT 0,
    coletado_em    TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS ix_resultados_publicacao
    ON resultados (publicacao_id, id DESC);
CREATE INDEX IF NOT EXISTS ix_resultados_canal
    ON resultados (canal_id_fonte);

-- Valores que a recalibração aprende e passa a mandar no pipeline.
--
-- Tabela e não .env de propósito: o .env é território do humano (guarda
-- segredo e é editado à mão), e um valor que o programa reescreve sozinho ali
-- viraria conflito na primeira vez que alguém abrisse o arquivo. Aqui o valor
-- fica auditável (`motivo`, `amostras`) e reversível — apagar a linha devolve
-- o default do settings.
--
-- Quem lê cada chave cai no settings quando ela não existe, então um banco sem
-- calibração nenhuma se comporta exatamente como antes da etapa 7.
CREATE TABLE IF NOT EXISTS calibracao (
    chave         TEXT PRIMARY KEY,
    valor         TEXT NOT NULL,
    amostras      INTEGER NOT NULL DEFAULT 0,
    motivo        TEXT NOT NULL DEFAULT '',
    atualizado_em TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
