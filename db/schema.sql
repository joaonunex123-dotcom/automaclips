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
-- Este arquivo cobre as etapas 1 (sourcing + fila) e 2 (pipeline). A tabela
-- `resultados` (analytics) entra na etapa 7, quando houver código que a
-- escreva — schema adiantado é schema adivinhado.

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
