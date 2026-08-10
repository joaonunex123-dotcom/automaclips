"""Camada de repositório sobre o clips.db — acesso a dado puro.

Sem regra de negócio: o score chega calculado de sourcing/score.py, e a
decisão de status chega tomada de sourcing/descobrir.py. Este módulo só grava
e lê. A separação é o que deixa a fórmula do score testável sem banco e o
banco testável sem API.

Concorrência: toda escrita roda em BEGIN IMMEDIATE ... COMMIT. O WAL permite
leitura durante escrita, e busy_timeout faz um segundo processo esperar em vez
de estourar 'database is locked'. PRAGMA foreign_keys é por conexão — o SQLite
não liga sozinho.

O banco fica na raiz do projeto (clips.db), sobrescrevível por CLIPS_DB_PATH
no ambiente; os testes apontam para um arquivo temporário.
"""
import logging
import sqlite3
from contextlib import contextmanager

import settings

log = logging.getLogger(__name__)

# Vocabulário de `fila_clips.status`. Constantes em vez de string solta para
# que um typo vire NameError na hora, e não uma linha invisível para todo
# SELECT que filtra por status.
#
# Etapa 1 — sourcing:
STATUS_DESCOBERTO = "descoberto"            # passou no corte, aguarda a etapa 2
STATUS_ABAIXO_DO_LIMIAR = "abaixo_do_limiar"  # pontuado, fora do threshold
STATUS_IGNORADO = "ignorado"                # fora da faixa de duração/idade

# Etapa 2 — pipeline. Cada um é o estado ALCANÇADO, não o em andamento: a
# linha só sai de 'descoberto' quando o download terminou de verdade, então
# uma queda no meio do processo deixa o vídeo no último ponto concluído e a
# retomada sabe exatamente onde continuar.
STATUS_BAIXADO = "baixado"
STATUS_TRANSCRITO = "transcrito"
STATUS_ANALISADO = "analisado"        # highlight_detect + seleção rodaram
STATUS_SEM_CLIPS = "sem_clips"        # analisado, nenhum trecho passou no corte
STATUS_FALHA = "falha"                # ver a coluna `erro`

# Estados que a varredura pode reavaliar numa passada seguinte. Um vídeo que
# hoje está abaixo do corte pode engatar amanhã; um que já entrou em
# processamento (etapas 2+) não pode voltar para trás, ou o pipeline
# reprocessaria material já consumido.
STATUS_REAVALIAVEIS = frozenset(
    {STATUS_DESCOBERTO, STATUS_ABAIXO_DO_LIMIAR, STATUS_IGNORADO}
)

# Vocabulário de `clips.status`.
CLIP_SELECIONADO = "selecionado"
CLIP_DESCARTADO = "descartado"

# Espera máxima por um lock de escrita, em milissegundos.
BUSY_TIMEOUT_MS = 5000


def conectar(caminho=None):
    """Abre conexão, liga os PRAGMAs e aplica o schema (idempotente).

    isolation_level=None desliga a gestão implícita de transação do módulo
    sqlite3 — sem isso ele abriria transações sozinho em pontos que não
    controlamos, e o BEGIN IMMEDIATE de escrita() falharia com "cannot start a
    transaction within a transaction".
    """
    caminho = caminho or settings.DB_PATH
    conn = sqlite3.connect(caminho, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    with open(settings.SCHEMA_PATH, encoding="utf-8") as f:
        conn.executescript(f.read())
    return conn


@contextmanager
def escrita(conn):
    """BEGIN IMMEDIATE ... COMMIT, com rollback em qualquer exceção.

    IMMEDIATE (e não o BEGIN adiado padrão) pega o lock de escrita já na
    abertura: se dois processos varrerem ao mesmo tempo, o segundo espera o
    busy_timeout aqui, em vez de descobrir o conflito só no COMMIT — quando já
    fez o trabalho todo e teria de refazê-lo.

    Reentrante: se já houver transação aberta, o bloco interno participa dela
    em vez de abrir outra. É o que permite a descobrir.py envolver
    "ler observação anterior + gravar a nova" numa transação só, sem que
    registrar_observacao() precise saber se foi chamada de dentro ou de fora.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def buscar_video(conn, plataforma, video_id):
    """A linha de fila_clips deste vídeo, ou None se nunca foi visto."""
    return conn.execute(
        "SELECT * FROM fila_clips WHERE plataforma = ? AND video_id = ?",
        (plataforma, video_id),
    ).fetchone()


def views_da_ultima_observacao(conn, fila_clip_id):
    """Views lidas na observação mais recente, ou None se não houver nenhuma.

    None e 0 são coisas diferentes e o chamador precisa distinguir: None é
    "nunca observado" (o ganho é o total de views), 0 é "observado e estava
    zerado" (o ganho é a diferença). Ver sourcing/score.py.
    """
    linha = conn.execute(
        "SELECT views FROM observacoes_video WHERE fila_clip_id = ?"
        " ORDER BY id DESC LIMIT 1",
        (fila_clip_id,),
    ).fetchone()
    return linha["views"] if linha else None


def registrar_observacao(conn, video, views, ganho, score, status):
    """Grava (ou atualiza) o vídeo na fila e anexa a observação desta varredura.

    `video` é o dict que sourcing/youtube.py monta: video_id, canal_id,
    canal_nome, titulo, url, publicado_em, duracao_s, plataforma.

    As duas escritas vão na MESMA transação de propósito: uma fila atualizada
    sem a observação correspondente faria a próxima varredura calcular o ganho
    contra um ponto que não existe no histórico.

    Devolve o id da linha em fila_clips.
    """
    with escrita(conn):
        existente = buscar_video(conn, video["plataforma"], video["video_id"])
        if existente is None:
            cur = conn.execute(
                "INSERT INTO fila_clips"
                " (plataforma, video_id, canal_id, canal_nome, titulo, url,"
                "  publicado_em, duracao_s, views, score, status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    video["plataforma"],
                    video["video_id"],
                    video["canal_id"],
                    video.get("canal_nome", ""),
                    video.get("titulo", ""),
                    video.get("url", ""),
                    video["publicado_em"],
                    video.get("duracao_s", 0),
                    views,
                    score,
                    status,
                ),
            )
            fila_clip_id = cur.lastrowid
        else:
            fila_clip_id = existente["id"]
            # O status só é reescrito enquanto o vídeo ainda está na fase de
            # triagem. Depois que o pipeline pegou o vídeo, uma varredura nova
            # atualiza views e score (o histórico continua valendo) mas NÃO
            # empurra o vídeo de volta para 'descoberto' — isso o faria ser
            # baixado e transcrito outra vez.
            if existente["status"] in STATUS_REAVALIAVEIS:
                conn.execute(
                    "UPDATE fila_clips SET titulo = ?, views = ?, score = ?,"
                    " status = ? WHERE id = ?",
                    (video.get("titulo", ""), views, score, status, fila_clip_id),
                )
            else:
                conn.execute(
                    "UPDATE fila_clips SET titulo = ?, views = ?, score = ?"
                    " WHERE id = ?",
                    (video.get("titulo", ""), views, score, fila_clip_id),
                )

        # Observação só é anexada quando a contagem MUDOU. A varredura roda a
        # cada 6 h sobre os mesmos uploads recentes, e um vídeo parado geraria
        # uma linha idêntica por varredura, para sempre — histórico que não
        # registra nada. A invariante de que a última observação concorda com
        # fila_clips.views continua valendo, já que o valor é o mesmo.
        if views != views_da_ultima_observacao(conn, fila_clip_id):
            conn.execute(
                "INSERT INTO observacoes_video (fila_clip_id, views, ganho, score)"
                " VALUES (?, ?, ?, ?)",
                (fila_clip_id, views, ganho, score),
            )
    return fila_clip_id


def listar_por_status(conn, status, limite=None):
    """Vídeos num status, do melhor score para o pior."""
    sql = "SELECT * FROM fila_clips WHERE status = ? ORDER BY score DESC, id"
    parametros = [status]
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def contar_por_status(conn):
    """{status: quantidade} sobre a fila inteira — usado no resumo da varredura."""
    linhas = conn.execute(
        "SELECT status, COUNT(*) AS n FROM fila_clips GROUP BY status"
    ).fetchall()
    return {linha["status"]: linha["n"] for linha in linhas}


def marcar_erro(conn, fila_clip_id, mensagem):
    """Anota a falha na linha sem mudar o status.

    Quem decide o status de falha é a etapa que falhou; aqui só fica o texto,
    para que uma etapa futura não perca o motivo ao reordenar a fila.
    """
    with escrita(conn):
        conn.execute(
            "UPDATE fila_clips SET erro = ? WHERE id = ?", (mensagem, fila_clip_id)
        )


def definir_status(conn, fila_clip_id, status, erro=None):
    """Move o vídeo de estado. `erro=None` limpa a falha anterior.

    Limpar é o comportamento certo no caminho feliz: uma etapa que passou
    invalida o motivo da falha anterior, e deixar o texto velho ali faria a
    linha parecer quebrada para sempre. Para anotar a falha SEM mexer no
    estado, use marcar_erro.
    """
    with escrita(conn):
        conn.execute(
            "UPDATE fila_clips SET status = ?, erro = ? WHERE id = ?",
            (status, erro, fila_clip_id),
        )


# --- mídia --------------------------------------------------------------------

CAMPOS_MIDIA = (
    "video_path",
    "audio_path",
    "transcricao_path",
    "duracao_real_s",
    "baixado_em",
    "transcrito_em",
)


def midia(conn, fila_clip_id):
    """A linha de `midia` deste vídeo, ou None se nada foi baixado ainda."""
    return conn.execute(
        "SELECT * FROM midia WHERE fila_clip_id = ?", (fila_clip_id,)
    ).fetchone()


def registrar_midia(conn, fila_clip_id, **campos):
    """Grava/atualiza APENAS os campos informados.

    Parcial de propósito: transcribe.py grava transcricao_path sem saber (nem
    precisar saber) o video_path que download.py gravou antes. Um upsert de
    linha inteira apagaria o trabalho da etapa anterior a cada chamada.
    """
    desconhecidos = set(campos) - set(CAMPOS_MIDIA)
    if desconhecidos:
        raise ValueError(f"campos de mídia desconhecidos: {sorted(desconhecidos)}")
    with escrita(conn):
        conn.execute(
            "INSERT OR IGNORE INTO midia (fila_clip_id) VALUES (?)", (fila_clip_id,)
        )
        if campos:
            atribuicoes = ", ".join(f"{nome} = ?" for nome in campos)
            conn.execute(
                f"UPDATE midia SET {atribuicoes} WHERE fila_clip_id = ?",
                (*campos.values(), fila_clip_id),
            )


# --- clips --------------------------------------------------------------------

def registrar_clips(conn, fila_clip_id, clips):
    """Substitui os trechos deste vídeo pelos informados, numa transação só.

    Substitui em vez de acumular: reprocessar um vídeo (prompt novo, few-shot
    recalibrado na etapa 7) deve produzir a análise vigente, não a união de
    todas as análises que já rodaram. O DELETE e os INSERTs vão juntos — uma
    falha no meio deixaria o vídeo sem trecho nenhum.

    Cada item é um dict: inicio_s, fim_s, score_claude, motivo, hook_text,
    picos_energia, score_final, status, motivo_descarte.
    """
    with escrita(conn):
        conn.execute("DELETE FROM clips WHERE fila_clip_id = ?", (fila_clip_id,))
        for clip in clips:
            conn.execute(
                "INSERT INTO clips"
                " (fila_clip_id, inicio_s, fim_s, score_claude, motivo, hook_text,"
                "  picos_energia, score_final, status, motivo_descarte)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    fila_clip_id,
                    clip["inicio_s"],
                    clip["fim_s"],
                    clip["score_claude"],
                    clip.get("motivo", ""),
                    clip.get("hook_text", ""),
                    clip.get("picos_energia", 0),
                    clip["score_final"],
                    clip.get("status", CLIP_SELECIONADO),
                    clip.get("motivo_descarte", ""),
                ),
            )


def clips_do_video(conn, fila_clip_id, status=None):
    """Trechos de um vídeo, do melhor score_final para o pior."""
    sql = "SELECT * FROM clips WHERE fila_clip_id = ?"
    parametros = [fila_clip_id]
    if status is not None:
        sql += " AND status = ?"
        parametros.append(status)
    sql += " ORDER BY score_final DESC, inicio_s"
    return conn.execute(sql, parametros).fetchall()


def listar_clips(conn, status=CLIP_SELECIONADO, limite=None):
    """Fila de edição: os melhores trechos de todos os vídeos, juntos."""
    sql = (
        "SELECT c.*, f.video_id, f.titulo, f.canal_nome"
        " FROM clips c JOIN fila_clips f ON f.id = c.fila_clip_id"
        " WHERE c.status = ? ORDER BY c.score_final DESC, c.id"
    )
    parametros = [status]
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


# --- custos de API paga -------------------------------------------------------

def registrar_custo(conn, servico, custo_usd, referencia="", quantidade=0.0,
                    unidade=""):
    """Anota uma chamada cobrada. Chamado DEPOIS de a chamada ter sucesso.

    Depois, e não antes, de propósito: uma chamada que falhou não é cobrada, e
    registrá-la reservaria orçamento que nunca foi gasto — com um teto de dez
    dólares, alguns erros bastariam para travar a fila sozinhos.
    """
    with escrita(conn):
        conn.execute(
            "INSERT INTO custos (servico, referencia, quantidade, unidade, custo_usd)"
            " VALUES (?, ?, ?, ?, ?)",
            (servico, referencia, quantidade, unidade, custo_usd),
        )


def custo_acumulado(conn, servico=None):
    """Total gasto, em USD. `servico=None` soma tudo."""
    if servico is None:
        linha = conn.execute("SELECT COALESCE(SUM(custo_usd), 0) FROM custos").fetchone()
    else:
        linha = conn.execute(
            "SELECT COALESCE(SUM(custo_usd), 0) FROM custos WHERE servico = ?",
            (servico,),
        ).fetchone()
    return float(linha[0])


# --- renders ------------------------------------------------------------------

def registrar_render(conn, clip_id, caminho, template_versao="", duracao_s=0.0):
    """Grava (ou substitui) o artefato renderizado de um clip."""
    with escrita(conn):
        conn.execute(
            "INSERT INTO renders (clip_id, caminho, template_versao, duracao_s)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(clip_id) DO UPDATE SET"
            "   caminho = excluded.caminho,"
            "   template_versao = excluded.template_versao,"
            "   duracao_s = excluded.duracao_s,"
            "   renderizado_em = datetime('now', 'localtime')",
            (clip_id, caminho, template_versao, duracao_s),
        )


def render(conn, clip_id):
    """A linha de `renders` deste clip, ou None se ainda não foi renderizado."""
    return conn.execute(
        "SELECT * FROM renders WHERE clip_id = ?", (clip_id,)
    ).fetchone()


def clips_para_renderizar(conn, limite=None):
    """Clips selecionados que ainda não têm arquivo, do melhor score para o pior.

    A ausência de linha em `renders` é o que define "pendente" — não um status
    novo em clips. Refazer um render é apagar a linha, não reabrir uma máquina
    de estados.
    """
    sql = (
        "SELECT c.*, f.video_id, f.titulo, f.canal_nome,"
        "       m.video_path, m.transcricao_path"
        " FROM clips c"
        " JOIN fila_clips f ON f.id = c.fila_clip_id"
        " LEFT JOIN midia m ON m.fila_clip_id = c.fila_clip_id"
        " LEFT JOIN renders r ON r.clip_id = c.id"
        " WHERE c.status = ? AND r.clip_id IS NULL"
        " ORDER BY c.score_final DESC, c.id"
    )
    parametros = [CLIP_SELECIONADO]
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()
