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
import json
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
    picos_energia, score_final, status, motivo_descarte, e opcionalmente
    picos_instantes (lista de segundos relativos ao início do trecho, que a
    etapa 4 usa para posicionar os efeitos sonoros).
    """
    with escrita(conn):
        conn.execute("DELETE FROM clips WHERE fila_clip_id = ?", (fila_clip_id,))
        for clip in clips:
            cursor = conn.execute(
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
            conn.executemany(
                "INSERT INTO picos_clip (clip_id, instante_s) VALUES (?, ?)",
                [(cursor.lastrowid, float(t))
                 for t in (clip.get("picos_instantes") or [])],
            )


def picos_do_clip(conn, clip_id):
    """Instantes dos picos deste trecho, relativos ao início dele."""
    return [
        float(linha[0])
        for linha in conn.execute(
            "SELECT instante_s FROM picos_clip WHERE clip_id = ?"
            " ORDER BY instante_s",
            (clip_id,),
        ).fetchall()
    ]


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


# --- publicações --------------------------------------------------------------

PUB_AGENDADO = "agendado"
PUB_SIMULADO = "simulado"
PUB_PUBLICADO = "publicado"
PUB_FALHA = "falha"


def clips_para_agendar(conn, plataforma, limite=None):
    """Clips renderizados ainda sem agendamento NESTA plataforma.

    O JOIN com `renders` é o que garante que só entra na fila de publicação o
    que tem arquivo: agendar um clip que ainda não renderizou marcaria um
    horário para um vídeo que não existe.
    """
    sql = (
        "SELECT c.*, r.caminho AS render_path, r.duracao_s AS render_duracao_s,"
        "       f.video_id, f.titulo AS titulo_fonte, f.canal_nome,"
        "       f.url AS url_fonte, m.transcricao_path"
        " FROM clips c"
        " JOIN renders r ON r.clip_id = c.id"
        " JOIN fila_clips f ON f.id = c.fila_clip_id"
        " LEFT JOIN midia m ON m.fila_clip_id = c.fila_clip_id"
        " LEFT JOIN publicacoes p ON p.clip_id = c.id AND p.plataforma = ?"
        " WHERE c.status = ? AND p.id IS NULL"
        " ORDER BY c.score_final DESC, c.id"
    )
    parametros = [plataforma, CLIP_SELECIONADO]
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def agendar_publicacao(conn, clip_id, plataforma, agendado_para, titulo="",
                       descricao="", hashtags=None):
    """Cria a publicação em 'agendado'. Devolve o id."""
    with escrita(conn):
        cursor = conn.execute(
            "INSERT INTO publicacoes"
            " (clip_id, plataforma, titulo, descricao, hashtags, agendado_para,"
            "  status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                clip_id, plataforma, titulo, descricao,
                json.dumps(list(hashtags or []), ensure_ascii=False),
                agendado_para, PUB_AGENDADO,
            ),
        )
        return cursor.lastrowid


def horarios_agendados(conn, plataforma, desde=None):
    """Horários já ocupados numa plataforma — o scheduler não pode repetir.

    Inclui 'simulado' e 'publicado' de propósito: um horário que já rendeu um
    post, ainda que só no modo sombra, está gasto. Ignorá-los faria a fila real
    empilhar dois clips no mesmo minuto ao sair da sombra.
    """
    sql = "SELECT agendado_para FROM publicacoes WHERE plataforma = ?"
    parametros = [plataforma]
    if desde is not None:
        sql += " AND agendado_para >= ?"
        parametros.append(desde)
    return [linha[0] for linha in conn.execute(sql, parametros).fetchall()]


def publicacoes_vencidas(conn, plataforma=None, agora=None, limite=None):
    """Agendamentos cuja hora chegou, do mais antigo para o mais novo."""
    sql = "SELECT p.*, r.caminho AS render_path, f.video_id" \
          " FROM publicacoes p" \
          " JOIN clips c ON c.id = p.clip_id" \
          " LEFT JOIN renders r ON r.clip_id = p.clip_id" \
          " JOIN fila_clips f ON f.id = c.fila_clip_id" \
          " WHERE p.status = ?"
    parametros = [PUB_AGENDADO]
    if plataforma is not None:
        sql += " AND p.plataforma = ?"
        parametros.append(plataforma)
    if agora is not None:
        sql += " AND p.agendado_para <= ?"
        parametros.append(agora)
    sql += " ORDER BY p.agendado_para, p.id"
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def marcar_publicacao(conn, publicacao_id, status, id_externo="", url="",
                      erro=None):
    """Desfecho de uma tentativa de publicação."""
    with escrita(conn):
        conn.execute(
            "UPDATE publicacoes SET status = ?, id_externo = ?, url = ?,"
            " erro = ?, publicado_em = CASE WHEN ? IN ('publicado', 'simulado')"
            "   THEN datetime('now', 'localtime') ELSE publicado_em END"
            " WHERE id = ?",
            (status, id_externo, url, erro, status, publicacao_id),
        )


def reagendar_simulados(conn, plataforma=None):
    """Devolve as publicações do modo sombra para 'agendado'.

    É a ponte da etapa 5 para a 6: ao ligar AUTO_PUBLISH, o que já foi
    planejado e simulado volta para a fila em vez de ser reconstruído do zero
    — o metadado gerado (e pago) continua valendo.
    """
    sql = "UPDATE publicacoes SET status = ?, publicado_em = NULL WHERE status = ?"
    parametros = [PUB_AGENDADO, PUB_SIMULADO]
    if plataforma is not None:
        sql += " AND plataforma = ?"
        parametros.append(plataforma)
    with escrita(conn):
        return conn.execute(sql, parametros).rowcount


def contar_publicacoes(conn):
    """{(plataforma, status): quantidade}."""
    linhas = conn.execute(
        "SELECT plataforma, status, COUNT(*) AS n FROM publicacoes"
        " GROUP BY plataforma, status"
    ).fetchall()
    return {(l["plataforma"], l["status"]): l["n"] for l in linhas}


def proximas_publicacoes(conn, limite=10):
    """A agenda, para o resumo."""
    return conn.execute(
        "SELECT p.*, f.video_id FROM publicacoes p"
        " JOIN clips c ON c.id = p.clip_id"
        " JOIN fila_clips f ON f.id = c.fila_clip_id"
        " WHERE p.status IN (?, ?)"
        " ORDER BY p.agendado_para LIMIT ?",
        (PUB_AGENDADO, PUB_SIMULADO, limite),
    ).fetchall()


def posts_publicados_no_dia(conn, plataforma=None, dia=None):
    """Quantos posts SAIRAM de verdade num dia (YYYY-MM-DD local).

    Conta so 'publicado': 'simulado' nao gastou nada e nao pode consumir o teto
    do dia real — senao uma semana de modo sombra bloquearia o primeiro dia de
    publicacao de verdade.
    """
    sql = ("SELECT COUNT(*) FROM publicacoes WHERE status = ?"
           " AND publicado_em IS NOT NULL")
    parametros = [PUB_PUBLICADO]
    if plataforma is not None:
        sql += " AND plataforma = ?"
        parametros.append(plataforma)
    if dia is not None:
        sql += " AND date(publicado_em) = ?"
        parametros.append(dia)
    return int(conn.execute(sql, parametros).fetchone()[0])


def primeiro_post_publicado(conn):
    """Data do primeiro post real, ou None se nenhum saiu ainda.

    E daqui que sai a contagem do periodo de aquecimento: o relogio comeca no
    primeiro post que foi ao ar, nao na data em que alguem ligou a flag.
    """
    linha = conn.execute(
        "SELECT MIN(date(publicado_em)) FROM publicacoes"
        " WHERE status = ? AND publicado_em IS NOT NULL",
        (PUB_PUBLICADO,),
    ).fetchone()
    return linha[0] if linha and linha[0] else None


# --- quota de API -------------------------------------------------------------

def quota_usada(conn, servico, dia):
    linha = conn.execute(
        "SELECT unidades FROM quota_api WHERE servico = ? AND dia = ?",
        (servico, dia),
    ).fetchone()
    return int(linha[0]) if linha else 0


def registrar_quota(conn, servico, dia, unidades):
    """Soma consumo ao dia. Chamado DEPOIS da chamada que gastou.

    Antes reservaria quota de uma chamada que pode falhar — e quota, ao
    contrário de dinheiro, não volta: o teto é diário e uma reserva errada
    custa um upload que caberia.
    """
    with escrita(conn):
        conn.execute(
            "INSERT INTO quota_api (servico, dia, unidades) VALUES (?, ?, ?)"
            " ON CONFLICT(servico, dia) DO UPDATE SET"
            "   unidades = unidades + excluded.unidades",
            (servico, dia, int(unidades)),
        )


# --- resultados (etapa 7) -----------------------------------------------------

def publicacoes_para_medir(conn, idade_minima_h=0.0, agora=None, limite=None):
    """Posts que sairam de verdade e valem uma medicao.

    So 'publicado': 'simulado' nao existe em plataforma nenhuma, e medi-lo
    devolveria zero para sempre — zeros que entrariam na media e puxariam a
    recalibracao para baixo.
    """
    # `publicado_em` e gravado em hora LOCAL (convencao do schema para
    # timestamp de controle), entao o "agora" da subtracao tambem precisa ser
    # local. Comparar com julianday('now'), que e UTC, inflaria a idade de todo
    # post pelo deslocamento do fuso -- tres horas aqui. Ranking nao mudaria (o
    # erro e constante), mas os cortes de idade minima e maxima sao absolutos e
    # passariam a disparar cedo demais.
    if agora is None:
        instante = "julianday('now', 'localtime')"
        de_agora = []
    else:
        instante = "julianday(?)"
        de_agora = [agora]

    sql = (
        "SELECT p.*, c.inicio_s, c.fim_s, c.score_final, c.hook_text, c.motivo,"
        "       f.canal_id AS canal_id_fonte, f.canal_nome AS canal_fonte,"
        f"       ({instante} - julianday(p.publicado_em)) * 24.0 AS horas_publicado"
        " FROM publicacoes p"
        " JOIN clips c ON c.id = p.clip_id"
        " JOIN fila_clips f ON f.id = c.fila_clip_id"
        " WHERE p.status = ? AND p.publicado_em IS NOT NULL"
        "   AND p.id_externo <> ''"
        f"   AND ({instante} - julianday(p.publicado_em)) * 24.0 >= ?"
        " ORDER BY p.publicado_em DESC"
    )
    parametros = de_agora + [PUB_PUBLICADO] + de_agora + [float(idade_minima_h)]
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def registrar_resultado(conn, publicacao, metricas, horas_publicado=0.0):
    """Anexa uma medicao. `publicacao` e a linha de publicacoes_para_medir."""
    with escrita(conn):
        conn.execute(
            "INSERT INTO resultados"
            " (publicacao_id, clip_id, plataforma, canal_id_fonte, canal_fonte,"
            "  trecho_inicio_s, trecho_duracao_s, score_previsto,"
            "  views, likes, comentarios, retencao, horas_publicado)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                publicacao["id"], publicacao["clip_id"], publicacao["plataforma"],
                publicacao["canal_id_fonte"] or "", publicacao["canal_fonte"] or "",
                float(publicacao["inicio_s"] or 0),
                float(publicacao["fim_s"] or 0) - float(publicacao["inicio_s"] or 0),
                float(publicacao["score_final"] or 0),
                int(metricas.get("views") or 0),
                int(metricas.get("likes") or 0),
                int(metricas.get("comentarios") or 0),
                metricas.get("retencao"),
                float(horas_publicado or 0),
            ),
        )


def ultimos_resultados(conn, idade_minima_h=0.0):
    """A medicao MAIS RECENTE de cada publicacao.

    Uma linha por publicacao e nao por medicao: a recalibracao quer o estado
    atual de cada post, e somar o historico contaria o mesmo clip varias vezes,
    dando peso extra justamente aos posts mais antigos (que foram medidos mais
    vezes).
    """
    return conn.execute(
        "SELECT r.*, c.hook_text, c.motivo,"
        # A hora em que o post REALMENTE saiu, nao a que estava agendada: e a
        # que a audiencia viu, e e ela que o scheduler quer aprender.
        "       CAST(strftime('%H', p.publicado_em) AS INTEGER) AS hora_publicado,"
        "       CAST(strftime('%M', p.publicado_em) AS INTEGER) AS minuto_publicado"
        " FROM resultados r"
        " JOIN clips c ON c.id = r.clip_id"
        " JOIN publicacoes p ON p.id = r.publicacao_id"
        " WHERE r.id IN ("
        "   SELECT MAX(id) FROM resultados GROUP BY publicacao_id"
        " ) AND r.horas_publicado >= ?"
        " ORDER BY r.id DESC",
        (float(idade_minima_h),),
    ).fetchall()


def contar_resultados(conn):
    return int(conn.execute("SELECT COUNT(*) FROM resultados").fetchone()[0])


# --- calibracao aprendida (etapa 7) -------------------------------------------

def obter_calibracao(conn, chave, padrao=None):
    """O valor aprendido, ou `padrao` se a recalibracao ainda nao o produziu.

    Cair no padrao e o caminho normal e nao um erro: um banco sem calibracao
    nenhuma se comporta exatamente como antes da etapa 7.
    """
    linha = conn.execute(
        "SELECT valor FROM calibracao WHERE chave = ?", (chave,)
    ).fetchone()
    return linha[0] if linha else padrao


def salvar_calibracao(conn, chave, valor, amostras=0, motivo=""):
    with escrita(conn):
        conn.execute(
            "INSERT INTO calibracao (chave, valor, amostras, motivo)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(chave) DO UPDATE SET"
            "   valor = excluded.valor, amostras = excluded.amostras,"
            "   motivo = excluded.motivo,"
            "   atualizado_em = datetime('now', 'localtime')",
            (chave, str(valor), int(amostras), motivo),
        )


def toda_calibracao(conn):
    return conn.execute(
        "SELECT * FROM calibracao ORDER BY chave"
    ).fetchall()


# --- qual modelo gerou o quê --------------------------------------------------

ETAPA_HIGHLIGHT = "highlight"
ETAPA_METADATA = "metadata"
ETAPA_RECALIBRATE = "recalibrate"


def registrar_geracao(conn, etapa, modelo_pedido, referencia="",
                      modelo_respondeu="", usou_fallback=False,
                      tokens_entrada=None, tokens_saida=None):
    """Anota qual modelo produziu uma saída. Nunca derruba quem chamou.

    Registro é observabilidade, não trabalho: uma falha ao gravar aqui não
    pode custar o metadado que acabou de ser gerado e pago.
    """
    try:
        with escrita(conn):
            conn.execute(
                "INSERT INTO geracoes_llm"
                " (etapa, referencia, modelo_pedido, modelo_respondeu,"
                "  usou_fallback, tokens_entrada, tokens_saida)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (etapa, str(referencia), modelo_pedido,
                 modelo_respondeu or modelo_pedido, 1 if usou_fallback else 0,
                 tokens_entrada, tokens_saida),
            )
    except sqlite3.Error as e:
        log.warning("Não consegui registrar a geração de %s: %s", etapa, e)


def geracoes(conn, etapa=None, limite=None):
    sql = "SELECT * FROM geracoes_llm"
    parametros = []
    if etapa is not None:
        sql += " WHERE etapa = ?"
        parametros.append(etapa)
    sql += " ORDER BY id DESC"
    if limite is not None:
        sql += " LIMIT ?"
        parametros.append(limite)
    return conn.execute(sql, parametros).fetchall()


def modelos_por_etapa(conn):
    """{(etapa, modelo_respondeu): quantidade} — para o resumo e a etapa 7."""
    linhas = conn.execute(
        "SELECT etapa, modelo_respondeu, COUNT(*) AS n FROM geracoes_llm"
        " GROUP BY etapa, modelo_respondeu"
    ).fetchall()
    return {(l["etapa"], l["modelo_respondeu"]): l["n"] for l in linhas}


# --- tokens que giram ---------------------------------------------------------

def obter_token(conn, servico):
    """A linha de `tokens`, ou None se nunca foi salvo."""
    return conn.execute(
        "SELECT * FROM tokens WHERE servico = ?", (servico,)
    ).fetchone()


def salvar_token(conn, servico, token, expira_em=None):
    with escrita(conn):
        conn.execute(
            "INSERT INTO tokens (servico, token, expira_em) VALUES (?, ?, ?)"
            " ON CONFLICT(servico) DO UPDATE SET"
            "   token = excluded.token, expira_em = excluded.expira_em,"
            "   atualizado_em = datetime('now', 'localtime')",
            (servico, token, expira_em),
        )


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
