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


# --- etapa 2: mídia e clips ---------------------------------------------------

@pytest.fixture
def clip_id(conn, video):
    return repositorio.registrar_observacao(
        conn, video(), views=1000, ganho=1000, score=250.0,
        status=repositorio.STATUS_DESCOBERTO,
    )


def test_definir_status_move_e_limpa_o_erro(conn, clip_id):
    repositorio.marcar_erro(conn, clip_id, "download falhou")
    repositorio.definir_status(conn, clip_id, repositorio.STATUS_BAIXADO)

    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["status"] == repositorio.STATUS_BAIXADO
    # Uma etapa que passou invalida o motivo da falha anterior; deixá-lo ali
    # faria a linha parecer quebrada para sempre.
    assert linha["erro"] is None


def test_definir_status_com_erro_registra_os_dois(conn, clip_id):
    repositorio.definir_status(
        conn, clip_id, repositorio.STATUS_FALHA, erro="ffmpeg morreu"
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["status"] == repositorio.STATUS_FALHA
    assert linha["erro"] == "ffmpeg morreu"


def test_midia_ausente_e_none(conn, clip_id):
    assert repositorio.midia(conn, clip_id) is None


def test_registrar_midia_e_parcial(conn, clip_id):
    # transcribe.py grava transcricao_path sem saber o video_path que
    # download.py gravou antes; um upsert de linha inteira apagaria aquele.
    repositorio.registrar_midia(
        conn, clip_id, video_path="/v/vid1.mp4", audio_path="/v/vid1.wav"
    )
    repositorio.registrar_midia(conn, clip_id, transcricao_path="/t/vid1.json")

    midia = repositorio.midia(conn, clip_id)
    assert midia["video_path"] == "/v/vid1.mp4"
    assert midia["audio_path"] == "/v/vid1.wav"
    assert midia["transcricao_path"] == "/t/vid1.json"


def test_registrar_midia_sem_campos_so_cria_a_linha(conn, clip_id):
    repositorio.registrar_midia(conn, clip_id)
    assert repositorio.midia(conn, clip_id)["video_path"] == ""


def test_campo_de_midia_desconhecido_e_recusado(conn, clip_id):
    # Sem a validação, um typo viraria SQL malformado ou coluna criada por
    # engano em vez de erro na hora.
    with pytest.raises(ValueError, match="desconhecidos"):
        repositorio.registrar_midia(conn, clip_id, vidoe_path="/typo.mp4")


def _clip(**kwargs):
    base = {
        "inicio_s": 100.0, "fim_s": 145.0, "score_claude": 8.0,
        "motivo": "fecha na virada", "hook_text": "ele nunca contou isso",
        "picos_energia": 3, "score_final": 8.8,
    }
    base.update(kwargs)
    return base


def test_registrar_clips_grava_o_trecho_inteiro(conn, clip_id):
    repositorio.registrar_clips(conn, clip_id, [_clip()])
    clip = repositorio.clips_do_video(conn, clip_id)[0]

    assert clip["inicio_s"] == 100.0
    assert clip["score_final"] == 8.8
    assert clip["picos_energia"] == 3
    assert clip["hook_text"] == "ele nunca contou isso"
    assert clip["status"] == repositorio.CLIP_SELECIONADO


def test_reprocessar_substitui_em_vez_de_acumular(conn, clip_id):
    # Reprocessar com prompt novo (etapa 7) deve produzir a análise vigente,
    # não a união de todas as análises que já rodaram.
    repositorio.registrar_clips(conn, clip_id, [_clip(inicio_s=100.0)])
    repositorio.registrar_clips(conn, clip_id, [_clip(inicio_s=300.0)])

    clips = repositorio.clips_do_video(conn, clip_id)
    assert [c["inicio_s"] for c in clips] == [300.0]


def test_clips_de_videos_diferentes_nao_se_misturam(conn, clip_id, video):
    outro = repositorio.registrar_observacao(
        conn, video(video_id="vid2"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_clips(conn, clip_id, [_clip()])
    repositorio.registrar_clips(conn, outro, [_clip(inicio_s=50.0)])

    assert len(repositorio.clips_do_video(conn, clip_id)) == 1
    assert len(repositorio.clips_do_video(conn, outro)) == 1


def test_clips_do_video_filtra_por_status(conn, clip_id):
    repositorio.registrar_clips(conn, clip_id, [
        _clip(inicio_s=100.0),
        _clip(inicio_s=300.0, score_final=1.0,
              status=repositorio.CLIP_DESCARTADO, motivo_descarte="score baixo"),
    ])
    assert len(repositorio.clips_do_video(conn, clip_id)) == 2
    selecionados = repositorio.clips_do_video(
        conn, clip_id, status=repositorio.CLIP_SELECIONADO
    )
    assert [c["inicio_s"] for c in selecionados] == [100.0]


def test_listar_clips_junta_os_videos_e_ordena(conn, clip_id, video):
    outro = repositorio.registrar_observacao(
        conn, video(video_id="vid2", titulo="Outro vídeo"), views=1, ganho=1,
        score=1.0, status=repositorio.STATUS_DESCOBERTO,
    )
    repositorio.registrar_clips(conn, clip_id, [_clip(score_final=5.0)])
    repositorio.registrar_clips(conn, outro, [_clip(score_final=9.5)])

    fila = repositorio.listar_clips(conn)
    assert [c["score_final"] for c in fila] == [9.5, 5.0]
    # A junção traz o contexto do vídeo, que a etapa 4 usa no metadado.
    assert fila[0]["titulo"] == "Outro vídeo"
    assert fila[0]["video_id"] == "vid2"


def test_listar_clips_respeita_o_limite(conn, clip_id):
    repositorio.registrar_clips(conn, clip_id, [
        _clip(inicio_s=float(i * 100), score_final=float(i)) for i in range(1, 5)
    ])
    assert len(repositorio.listar_clips(conn, limite=2)) == 2


def test_unique_impede_dois_trechos_no_mesmo_instante(conn, clip_id):
    with pytest.raises(sqlite3.IntegrityError):
        repositorio.registrar_clips(conn, clip_id, [_clip(), _clip()])


def test_foreign_key_de_clip_e_aplicada(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO clips (fila_clip_id, inicio_s, fim_s, score_claude,"
            " score_final) VALUES (12345, 0, 30, 8, 8)"
        )


# --- custos e renders ---------------------------------------------------------

def test_custo_acumulado_comeca_zerado(conn):
    assert repositorio.custo_acumulado(conn) == 0.0


def test_custos_somam_e_filtram_por_servico(conn):
    repositorio.registrar_custo(conn, "openai:whisper-1", 0.36, quantidade=60,
                               unidade="minuto")
    repositorio.registrar_custo(conn, "openai:whisper-1", 0.12)
    repositorio.registrar_custo(conn, "anthropic:opus", 0.25)

    assert repositorio.custo_acumulado(conn) == pytest.approx(0.73)
    assert repositorio.custo_acumulado(conn, "openai:whisper-1") == pytest.approx(0.48)
    assert repositorio.custo_acumulado(conn, "nada") == 0.0


def test_custo_guarda_o_consumo_alem_do_valor(conn):
    # O preço unitário muda; o consumo não. Guardar os dois permite reconferir
    # o gasto contra a tabela de preços vigente depois.
    repositorio.registrar_custo(
        conn, "openai:whisper-1", 0.36, referencia="vid1",
        quantidade=60.0, unidade="minuto",
    )
    linha = conn.execute("SELECT * FROM custos").fetchone()
    assert linha["quantidade"] == 60.0
    assert linha["unidade"] == "minuto"
    assert linha["referencia"] == "vid1"


def test_render_ausente_e_none(conn, clip_id):
    repositorio.registrar_clips(conn, clip_id, [_clip()])
    id_do_clip = repositorio.clips_do_video(conn, clip_id)[0]["id"]
    assert repositorio.render(conn, id_do_clip) is None


def test_registrar_render_substitui_o_anterior(conn, clip_id):
    repositorio.registrar_clips(conn, clip_id, [_clip()])
    id_do_clip = repositorio.clips_do_video(conn, clip_id)[0]["id"]

    repositorio.registrar_render(conn, id_do_clip, "/r/v1.mp4", template_versao="1")
    repositorio.registrar_render(conn, id_do_clip, "/r/v2.mp4", template_versao="2")

    linha = repositorio.render(conn, id_do_clip)
    assert linha["caminho"] == "/r/v2.mp4"
    assert linha["template_versao"] == "2"
    assert conn.execute("SELECT COUNT(*) FROM renders").fetchone()[0] == 1


def test_clips_para_renderizar_traz_as_fontes(conn, clip_id):
    repositorio.registrar_midia(
        conn, clip_id, video_path="/v/vid1.mp4", transcricao_path="/t/vid1.json"
    )
    repositorio.registrar_clips(conn, clip_id, [_clip()])

    linha = repositorio.clips_para_renderizar(conn)[0]
    assert linha["video_path"] == "/v/vid1.mp4"
    assert linha["transcricao_path"] == "/t/vid1.json"
    assert linha["video_id"] == "vid1"


def test_clip_sem_midia_ainda_aparece_na_fila(conn, clip_id):
    # LEFT JOIN de propósito: quem decide o que fazer com a fonte ausente é o
    # editar.py, com mensagem. Sumir da fila em silêncio seria pior.
    repositorio.registrar_clips(conn, clip_id, [_clip()])
    assert len(repositorio.clips_para_renderizar(conn)) == 1
