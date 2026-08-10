"""Cliente da API do YouTube — montagem, paginação e custo de quota."""
import pytest

from sourcing import youtube


@pytest.mark.parametrize(
    "iso, segundos",
    [
        ("PT4M13S", 253),
        ("PT1H2M3S", 3723),
        ("PT0S", 0),
        ("PT45S", 45),
        ("PT2H", 7200),
        ("P1DT2H30M", 95400),
        ("P0D", 0),        # live em andamento
        ("", 0),
        (None, 0),
        ("lixo", 0),
    ],
)
def test_duracao_para_segundos(iso, segundos):
    assert youtube.duracao_para_segundos(iso) == segundos


def _cliente_com_um_canal(cliente_falso, n_videos=2):
    videos = {
        f"v{i}": {
            "snippet": {
                "channelId": "UC1",
                "channelTitle": "Canal 1",
                "title": f"Vídeo {i}",
                "publishedAt": "2026-08-10T06:00:00Z",
            },
            "contentDetails": {"duration": "PT10M"},
            "statistics": {"viewCount": str(1000 * (i + 1))},
        }
        for i in range(n_videos)
    }
    return cliente_falso(
        canais={"UC1": {"nome": "Canal 1", "uploads": "UU1"}},
        playlists={"UU1": list(videos)},
        videos=videos,
    )


def test_coletar_monta_o_dict_do_repositorio(cliente_falso):
    cliente = _cliente_com_um_canal(cliente_falso)
    videos = youtube.coletar(cliente, [{"id": "UC1"}], max_por_canal=10)

    assert len(videos) == 2
    v = videos[0]
    assert v == {
        "plataforma": "youtube",
        "video_id": "v0",
        "canal_id": "UC1",
        "canal_nome": "Canal 1",
        "titulo": "Vídeo 0",
        "url": "https://www.youtube.com/watch?v=v0",
        "publicado_em": "2026-08-10T06:00:00Z",
        "duracao_s": 600,
        "views": 1000,
    }


def test_coletar_nao_usa_search_e_gasta_quota_minima(cliente_falso):
    # 1 channels + 1 playlistItems + 1 videos = 3 unidades para um canal.
    # search.list sozinho custaria 100.
    cliente = _cliente_com_um_canal(cliente_falso, n_videos=5)
    youtube.coletar(cliente, [{"id": "UC1"}], max_por_canal=5)
    assert cliente.chamadas == {"channels": 1, "playlistItems": 1, "videos": 1}


def test_um_lote_de_channels_para_varios_canais(cliente_falso):
    canais = {f"UC{i}": {"nome": f"C{i}", "uploads": f"UU{i}"} for i in range(10)}
    cliente = cliente_falso(canais=canais, playlists={f"UU{i}": [] for i in range(10)})
    youtube.playlists_de_uploads(cliente, list(canais))
    assert cliente.chamadas["channels"] == 1


def test_canal_inexistente_e_avisado_e_pulado(cliente_falso, caplog):
    cliente = _cliente_com_um_canal(cliente_falso)
    mapa = youtube.playlists_de_uploads(cliente, ["UC1", "UC_INEXISTENTE"])
    assert set(mapa) == {"UC1"}
    assert "UC_INEXISTENTE" in caplog.text


def test_max_por_canal_limita(cliente_falso):
    cliente = _cliente_com_um_canal(cliente_falso, n_videos=10)
    videos = youtube.coletar(cliente, [{"id": "UC1"}], max_por_canal=3)
    assert len(videos) == 3


def test_lista_de_canais_vazia_nao_chama_a_api(cliente_falso):
    cliente = cliente_falso()
    assert youtube.coletar(cliente, [], max_por_canal=10) == []
    assert cliente.chamadas["channels"] == 0


def test_falha_num_canal_nao_derruba_os_outros(cliente_falso, caplog):
    cliente = _cliente_com_um_canal(cliente_falso)
    cliente._canais["UC2"] = {"nome": "Canal 2", "uploads": "UU_PRIVADA"}

    original = cliente.playlistItems

    def quebrado():
        recurso = original()
        list_original = recurso.list

        def list_talvez(**kwargs):
            if kwargs.get("playlistId") == "UU_PRIVADA":
                raise RuntimeError("playlist privada")
            return list_original(**kwargs)

        recurso.list = list_talvez
        return recurso

    cliente.playlistItems = quebrado
    videos = youtube.coletar(cliente, [{"id": "UC1"}, {"id": "UC2"}], max_por_canal=5)
    assert [v["video_id"] for v in videos] == ["v0", "v1"]
    assert "playlist privada" in caplog.text


def test_video_sem_view_count_entra_com_zero(cliente_falso):
    cliente = cliente_falso(
        canais={"UC1": {"nome": "C", "uploads": "UU1"}},
        playlists={"UU1": ["v0"]},
        videos={
            "v0": {
                "snippet": {"channelId": "UC1", "title": "t", "publishedAt": "2026-08-10T06:00:00Z"},
                "contentDetails": {"duration": "PT10M"},
                "statistics": {},
            }
        },
    )
    assert youtube.coletar(cliente, [{"id": "UC1"}])[0]["views"] == 0


def test_construir_cliente_sem_chave_falha_com_mensagem_util(monkeypatch):
    monkeypatch.setattr("settings.YOUTUBE_API_KEY", "")
    with pytest.raises(youtube.ErroYouTube, match="YOUTUBE_API_KEY"):
        youtube.construir_cliente()
