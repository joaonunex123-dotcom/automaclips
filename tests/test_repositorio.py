"""Fila e histórico de observações no SQLite."""
import sqlite3

import pytest

from db import repositorio


def _observacoes(conn, fila_clip_id):
    return conn.execute(
        "SELECT * FROM observacoes_video WHERE fila_clip_id = ? ORDER BY id",
        (fila_clip_id,),
    ).fetchall()


def test_pragmas_ligados(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == repositorio.BUSY_TIMEOUT_MS


def test_schema_e_idempotente(conn, tmp_path):
    # conectar() roda o executescript de novo sobre um banco que já existe.
    outra = repositorio.conectar()
    try:
        assert outra.execute("SELECT COUNT(*) FROM fila_clips").fetchone()[0] == 0
    finally:
        outra.close()


def test_insere_e_le(conn, video):
    v = video(views=1000)
    clip_id = repositorio.registrar_observacao(
        conn, v, views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["id"] == clip_id
    assert linha["views"] == 1000
    assert linha["score"] == pytest.approx(250.0)
    assert linha["status"] == repositorio.STATUS_DESCOBERTO
    assert linha["canal_nome"] == "Canal de Teste"
    assert linha["duracao_s"] == 1200


def test_video_repetido_vira_update_nao_linha_nova(conn, video):
    v = video(views=1000)
    primeiro = repositorio.registrar_observacao(
        conn, v, views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    segundo = repositorio.registrar_observacao(
        conn, video(views=3000, titulo="Título editado"), views=3000, ganho=2000,
        score=400.0, status=repositorio.STATUS_DESCOBERTO,
    )
    assert primeiro == segundo
    assert conn.execute("SELECT COUNT(*) FROM fila_clips").fetchone()[0] == 1
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["views"] == 3000
    assert linha["titulo"] == "Título editado"


def test_unique_impede_duplicata_por_sql_cru(conn, video):
    repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO fila_clips (plataforma, video_id, canal_id, publicado_em)"
            " VALUES ('youtube', 'vid1', 'UC_teste', '2026-08-10T00:00:00Z')"
        )


def test_mesmo_video_id_em_plataformas_diferentes_coexiste(conn, video):
    repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_observacao(
        conn, video(plataforma="twitch"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    assert conn.execute("SELECT COUNT(*) FROM fila_clips").fetchone()[0] == 2


def test_historico_acumula_quando_as_views_mudam(conn, video):
    clip_id = repositorio.registrar_observacao(
        conn, video(views=1000), views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_observacao(
        conn, video(views=2500), views=2500, ganho=1500, score=300.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    obs = _observacoes(conn, clip_id)
    assert [o["views"] for o in obs] == [1000, 2500]
    assert [o["ganho"] for o in obs] == [1000, 1500]


def test_views_iguais_nao_geram_observacao_repetida(conn, video):
    # Vídeo parado, varrido de novo 6 h depois: nada mudou, nada a registrar.
    clip_id = repositorio.registrar_observacao(
        conn, video(views=1000), views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_observacao(
        conn, video(views=1000), views=1000, ganho=0, score=0.0,
        status=repositorio.STATUS_ABAIXO_DO_LIMIAR,
    )
    assert len(_observacoes(conn, clip_id)) == 1
    # ...mas o status e o score da fila acompanham a reavaliação.
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["status"] == repositorio.STATUS_ABAIXO_DO_LIMIAR
    assert linha["score"] == 0.0


def test_views_da_ultima_observacao_distingue_ausencia_de_zero(conn, video):
    clip_id = repositorio.registrar_observacao(
        conn, video(views=0), views=0, ganho=0, score=0.0,
        status=repositorio.STATUS_ABAIXO_DO_LIMIAR,
    )
    # Observado, e estava zerado: 0, não None.
    assert repositorio.views_da_ultima_observacao(conn, clip_id) == 0
    # Nunca observado: None.
    assert repositorio.views_da_ultima_observacao(conn, 999) is None


def test_status_de_processamento_nao_regride(conn, video):
    clip_id = repositorio.registrar_observacao(
        conn, video(views=1000), views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    # A etapa 2 pegou o vídeo.
    with repositorio.escrita(conn):
        conn.execute("UPDATE fila_clips SET status = 'transcrito' WHERE id = ?", (clip_id,))

    # Varredura seguinte reencontra o vídeo e tentaria remarcá-lo.
    repositorio.registrar_observacao(
        conn, video(views=9000), views=9000, ganho=8000, score=900.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    # Status preservado — reprocessar material já consumido seria pagar
    # download e transcrição de novo.
    assert linha["status"] == "transcrito"
    # Mas views e score continuam sendo atualizados.
    assert linha["views"] == 9000
    assert linha["score"] == pytest.approx(900.0)


def test_status_de_triagem_e_reavaliado(conn, video):
    repositorio.registrar_observacao(
        conn, video(views=100), views=100, ganho=100, score=10.0,
        status=repositorio.STATUS_ABAIXO_DO_LIMIAR,
    )
    # Engatou no dia seguinte.
    repositorio.registrar_observacao(
        conn, video(views=50000), views=50000, ganho=49900, score=2000.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    assert repositorio.buscar_video(conn, "youtube", "vid1")["status"] == (
        repositorio.STATUS_DESCOBERTO
    )


def test_trigger_atualiza_atualizado_em(conn, video):
    clip_id = repositorio.registrar_observacao(
        conn, video(views=1), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    with repositorio.escrita(conn):
        conn.execute(
            "UPDATE fila_clips SET atualizado_em = '2000-01-01 00:00:00' WHERE id = ?",
            (clip_id,),
        )
        conn.execute("UPDATE fila_clips SET views = 2 WHERE id = ?", (clip_id,))
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["atualizado_em"] != "2000-01-01 00:00:00"


def test_escrita_faz_rollback(conn, video):
    repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    with pytest.raises(RuntimeError):
        with repositorio.escrita(conn):
            conn.execute("UPDATE fila_clips SET views = 999")
            raise RuntimeError("falha no meio da transação")
    assert repositorio.buscar_video(conn, "youtube", "vid1")["views"] == 1
    assert not conn.in_transaction


def test_escrita_e_reentrante(conn, video):
    # O bloco externo abre a transação; registrar_observacao participa dela.
    with repositorio.escrita(conn):
        repositorio.registrar_observacao(
            conn, video(), views=5, ganho=5, score=5.0,
            status=repositorio.STATUS_DESCOBERTO,
        )
        assert conn.in_transaction
    assert not conn.in_transaction
    assert repositorio.buscar_video(conn, "youtube", "vid1")["views"] == 5


def test_rollback_do_bloco_externo_desfaz_a_observacao(conn, video):
    with pytest.raises(RuntimeError):
        with repositorio.escrita(conn):
            repositorio.registrar_observacao(
                conn, video(), views=5, ganho=5, score=5.0,
                status=repositorio.STATUS_DESCOBERTO,
            )
            raise RuntimeError("falha depois de gravar")
    assert repositorio.buscar_video(conn, "youtube", "vid1") is None
    assert conn.execute("SELECT COUNT(*) FROM observacoes_video").fetchone()[0] == 0


def test_listar_por_status_ordena_por_score(conn, video):
    for i, s in enumerate([10.0, 900.0, 300.0]):
        repositorio.registrar_observacao(
            conn, video(video_id=f"v{i}"), views=1, ganho=1, score=s,
            status=repositorio.STATUS_DESCOBERTO,
        )
    scores = [l["score"] for l in repositorio.listar_por_status(
        conn, repositorio.STATUS_DESCOBERTO)]
    assert scores == [900.0, 300.0, 10.0]
    assert len(repositorio.listar_por_status(
        conn, repositorio.STATUS_DESCOBERTO, limite=2)) == 2


def test_contar_por_status(conn, video):
    repositorio.registrar_observacao(
        conn, video(video_id="a"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_observacao(
        conn, video(video_id="b"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_observacao(
        conn, video(video_id="c"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_IGNORADO,
    )
    assert repositorio.contar_por_status(conn) == {
        repositorio.STATUS_DESCOBERTO: 2,
        repositorio.STATUS_IGNORADO: 1,
    }


def test_marcar_erro_nao_mexe_no_status(conn, video):
    clip_id = repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.marcar_erro(conn, clip_id, "download falhou")
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["erro"] == "download falhou"
    assert linha["status"] == repositorio.STATUS_DESCOBERTO


def test_foreign_key_de_observacao_e_aplicada(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO observacoes_video (fila_clip_id, views) VALUES (12345, 1)"
        )
