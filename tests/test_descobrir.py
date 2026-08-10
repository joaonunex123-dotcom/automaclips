"""Classificação de status e a varredura ponta a ponta (sem rede)."""
from datetime import timedelta

import pytest

import settings
from db import repositorio
from sourcing import descobrir, score
from tests.conftest import AGORA


def _pontuar(video, agora=AGORA, anteriores=None):
    return score.calcular(
        video.get("views", 0), anteriores, video["publicado_em"], agora=agora
    )


# --- classificar --------------------------------------------------------------

def test_video_curto_demais_e_ignorado(video):
    v = video(duracao_s=60, views=1_000_000)
    status, motivo = descobrir.classificar(v, _pontuar(v))
    assert status == repositorio.STATUS_IGNORADO
    assert "abaixo do mínimo" in motivo


def test_duracao_zero_e_ignorada(video):
    # É como youtube.duracao_para_segundos devolve live em andamento.
    v = video(duracao_s=0, views=500_000)
    assert descobrir.classificar(v, _pontuar(v))[0] == repositorio.STATUS_IGNORADO


def test_video_longo_demais_e_ignorado(video):
    v = video(duracao_s=settings.DURACAO_MAXIMA_S + 1, views=1_000_000)
    status, motivo = descobrir.classificar(v, _pontuar(v))
    assert status == repositorio.STATUS_IGNORADO
    assert "acima do máximo" in motivo


def test_duracao_manda_mesmo_com_score_altissimo(video):
    # A ordem dos testes importa: duração é propriedade do vídeo e vale
    # independentemente de tração.
    v = video(duracao_s=30, views=10_000_000)
    p = _pontuar(v)
    assert p.score > settings.SCORE_THRESHOLD
    assert descobrir.classificar(v, p)[0] == repositorio.STATUS_IGNORADO


def test_video_velho_demais_e_ignorado(video, publicado_ha):
    v = video(publicado_em=publicado_ha(settings.IDADE_MAXIMA_HORAS + 1), views=10_000_000)
    status, motivo = descobrir.classificar(v, _pontuar(v))
    assert status == repositorio.STATUS_IGNORADO
    assert "fora da janela" in motivo


def test_publicacao_no_futuro_e_ignorada(video, publicado_ha):
    v = video(publicado_em=publicado_ha(-3), views=10)
    status, motivo = descobrir.classificar(v, _pontuar(v))
    assert status == repositorio.STATUS_IGNORADO
    assert "futuro" in motivo


def test_abaixo_do_threshold(video, publicado_ha):
    v = video(publicado_em=publicado_ha(10), views=100)  # 10 views/h
    status, motivo = descobrir.classificar(v, _pontuar(v))
    assert status == repositorio.STATUS_ABAIXO_DO_LIMIAR
    assert "<" in motivo


def test_acima_do_threshold_entra_na_fila(video, publicado_ha):
    v = video(publicado_em=publicado_ha(10), views=20_000)  # 2000 views/h
    assert descobrir.classificar(v, _pontuar(v))[0] == repositorio.STATUS_DESCOBERTO


def test_score_exatamente_no_threshold_entra(video, publicado_ha):
    v = video(publicado_em=publicado_ha(10), views=int(settings.SCORE_THRESHOLD * 10))
    p = _pontuar(v)
    assert p.score == pytest.approx(settings.SCORE_THRESHOLD)
    assert descobrir.classificar(v, p)[0] == repositorio.STATUS_DESCOBERTO


def test_threshold_explicito_sobrescreve_o_settings(video, publicado_ha):
    v = video(publicado_em=publicado_ha(10), views=100)
    assert descobrir.classificar(v, _pontuar(v), threshold=5.0)[0] == (
        repositorio.STATUS_DESCOBERTO
    )


# --- processar_videos ---------------------------------------------------------

def test_processar_grava_e_conta(conn, video, publicado_ha):
    videos = [
        video(video_id="bom", publicado_em=publicado_ha(4), views=40_000),
        video(video_id="fraco", publicado_em=publicado_ha(4), views=100),
        video(video_id="curto", duracao_s=30, views=99_999),
    ]
    contagem = descobrir.processar_videos(conn, videos, agora=AGORA)
    assert contagem == {
        repositorio.STATUS_DESCOBERTO: 1,
        repositorio.STATUS_ABAIXO_DO_LIMIAR: 1,
        repositorio.STATUS_IGNORADO: 1,
    }
    fila = repositorio.listar_por_status(conn, repositorio.STATUS_DESCOBERTO)
    assert [l["video_id"] for l in fila] == ["bom"]
    assert fila[0]["score"] == pytest.approx(10_000.0)


def test_segunda_varredura_pontua_pelo_ganho_nao_pelo_total(conn, video, publicado_ha):
    # 1ª passada: 3000 views em 6 h = 500 v/h, no corte.
    descobrir.processar_videos(
        conn, [video(publicado_em=publicado_ha(6), views=3000)], agora=AGORA
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["score"] == pytest.approx(500.0)
    assert linha["status"] == repositorio.STATUS_DESCOBERTO

    # 6 h depois o vídeo tem 4000 views. O total subiu, mas só ganhou 1000 na
    # janela e já tem 12 h de idade: o score CAI, e o vídeo sai do corte.
    depois = AGORA + timedelta(hours=6)
    descobrir.processar_videos(
        conn, [video(publicado_em=publicado_ha(6), views=4000)], agora=depois
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["views"] == 4000
    assert linha["score"] == pytest.approx(1000 / 12)
    assert linha["status"] == repositorio.STATUS_ABAIXO_DO_LIMIAR

    obs = conn.execute(
        "SELECT ganho FROM observacoes_video ORDER BY id"
    ).fetchall()
    assert [o["ganho"] for o in obs] == [3000, 1000]


def test_video_fora_do_corte_fica_gravado_e_pode_engatar(conn, video, publicado_ha):
    # É o motivo de gravar o que ficou de fora: sem a linha, a passada seguinte
    # pontuaria o total de novo e o ganho real ficaria invisível.
    descobrir.processar_videos(
        conn, [video(publicado_em=publicado_ha(2), views=100)], agora=AGORA
    )
    assert repositorio.buscar_video(conn, "youtube", "vid1")["status"] == (
        repositorio.STATUS_ABAIXO_DO_LIMIAR
    )

    depois = AGORA + timedelta(hours=2)
    descobrir.processar_videos(
        conn, [video(publicado_em=publicado_ha(2), views=20_100)], agora=depois
    )
    linha = repositorio.buscar_video(conn, "youtube", "vid1")
    assert linha["status"] == repositorio.STATUS_DESCOBERTO
    assert linha["score"] == pytest.approx(20_000 / 4)


def test_processar_lista_vazia(conn):
    assert descobrir.processar_videos(conn, [], agora=AGORA) == {}


# --- varrer + resumo ----------------------------------------------------------

def test_varrer_ponta_a_ponta(conn, cliente_falso):
    cliente = cliente_falso(
        canais={"UC1": {"nome": "Canal 1", "uploads": "UU1"}},
        playlists={"UU1": ["v0", "v1"]},
        videos={
            "v0": {
                "snippet": {
                    "channelId": "UC1", "channelTitle": "Canal 1", "title": "Viral",
                    "publishedAt": (AGORA - timedelta(hours=4)).isoformat(),
                },
                "contentDetails": {"duration": "PT20M"},
                "statistics": {"viewCount": "40000"},
            },
            "v1": {
                "snippet": {
                    "channelId": "UC1", "channelTitle": "Canal 1", "title": "Parado",
                    "publishedAt": (AGORA - timedelta(hours=4)).isoformat(),
                },
                "contentDetails": {"duration": "PT20M"},
                "statistics": {"viewCount": "50"},
            },
        },
    )
    contagem = descobrir.varrer(
        conn, cliente, [{"id": "UC1"}], max_por_canal=10, agora=AGORA
    )
    assert contagem[repositorio.STATUS_DESCOBERTO] == 1
    assert contagem[repositorio.STATUS_ABAIXO_DO_LIMIAR] == 1

    fila = repositorio.listar_por_status(conn, repositorio.STATUS_DESCOBERTO)
    assert fila[0]["titulo"] == "Viral"
    assert fila[0]["canal_nome"] == "Canal 1"
    assert fila[0]["url"] == "https://www.youtube.com/watch?v=v0"


def test_resumo_e_legivel(conn, video, publicado_ha):
    contagem = descobrir.processar_videos(
        conn, [video(publicado_em=publicado_ha(4), views=40_000)], agora=AGORA
    )
    texto = descobrir._resumo(conn, contagem)
    assert "descobertos       1" in texto
    assert "topo da fila" in texto
    assert "Um título" in texto
