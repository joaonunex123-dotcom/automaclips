"""Instagram: token que gira, e a publicação em duas etapas."""
from datetime import datetime, timedelta

import pytest

import settings
from db import repositorio
from publish import instagram

AGORA = datetime(2026, 8, 11, 12, 0, 0)


# --- token --------------------------------------------------------------------

def test_sem_token_nenhum_falha_com_instrucao(conn, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "")
    with pytest.raises(instagram.ErroInstagram, match="INSTAGRAM_TOKEN_INICIAL"):
        instagram.token_atual(conn)


def test_cai_no_token_do_env_na_primeira_vez(conn, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "semente")
    assert instagram.token_atual(conn) == ("semente", None)


def test_token_do_banco_ganha_do_env(conn, monkeypatch):
    # Depois da primeira renovação o .env vira só semente — é o que evita o
    # humano colar um token novo a cada dois meses.
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "semente")
    repositorio.salvar_token(conn, "instagram", "renovado", "2026-10-01T00:00:00")
    assert instagram.token_atual(conn) == ("renovado", "2026-10-01T00:00:00")


def test_sem_validade_conhecida_renova(conn):
    # É o caso do token que veio do .env: renovar um token válido é barato,
    # deixá-lo morrer não é.
    assert instagram.precisa_renovar(None, agora=AGORA) is True


def test_validade_ilegivel_renova(conn, caplog):
    assert instagram.precisa_renovar("mês que vem", agora=AGORA) is True
    assert "ilegível" in caplog.text


def test_renova_com_antecedencia(conn):
    daqui_5_dias = (AGORA + timedelta(days=5)).isoformat()
    daqui_40_dias = (AGORA + timedelta(days=40)).isoformat()
    assert instagram.precisa_renovar(daqui_5_dias, AGORA, antecedencia_dias=10)
    assert not instagram.precisa_renovar(daqui_40_dias, AGORA, antecedencia_dias=10)


def test_renovacao_grava_o_novo_token(conn, http_falso, resposta_falsa):
    http = http_falso([
        resposta_falsa({"access_token": "novo", "expires_in": 5184000})
    ])
    novo = instagram.renovar(conn, token="antigo", http=http, agora=AGORA)

    assert novo == "novo"
    assert http.chamadas[0]["params"]["grant_type"] == "ig_refresh_token"
    linha = repositorio.obter_token(conn, "instagram")
    assert linha["token"] == "novo"
    assert linha["expira_em"].startswith("2026-10-10")


def test_renovacao_sem_token_na_resposta(conn, http_falso, resposta_falsa):
    http = http_falso([resposta_falsa({"nada": "aqui"})])
    with pytest.raises(instagram.ErroInstagram, match="sem access_token"):
        instagram.renovar(conn, token="antigo", http=http, agora=AGORA)


def test_erro_da_api_vira_excecao_legivel(conn, http_falso, resposta_falsa):
    http = http_falso([
        resposta_falsa({"error": {"message": "token inválido"}}, status_code=400)
    ])
    with pytest.raises(instagram.ErroInstagram, match="token inválido"):
        instagram.renovar(conn, token="ruim", http=http, agora=AGORA)


def test_falha_na_renovacao_nao_descarta_token_valido(conn, http_falso,
                                                      resposta_falsa, caplog):
    # A publicação de hoje ainda funciona; a próxima execução tenta de novo.
    repositorio.salvar_token(conn, "instagram", "ainda_bom",
                             (AGORA + timedelta(days=5)).isoformat())
    http = http_falso([resposta_falsa({"error": {"message": "instável"}},
                                      status_code=500)])
    assert instagram.garantir_token(conn, http=http, agora=AGORA) == "ainda_bom"
    assert "seguindo com o token atual" in caplog.text


def test_falha_na_renovacao_sem_validade_conhecida_propaga(conn, http_falso,
                                                           resposta_falsa,
                                                           monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_TOKEN_INICIAL", "semente")
    http = http_falso([resposta_falsa({"error": {"message": "morto"}},
                                      status_code=400)])
    with pytest.raises(instagram.ErroInstagram):
        instagram.garantir_token(conn, http=http, agora=AGORA)


def test_token_novo_nao_e_renovado_de_novo(conn, http_falso):
    repositorio.salvar_token(conn, "instagram", "novinho",
                             (AGORA + timedelta(days=50)).isoformat())
    http = http_falso()
    assert instagram.garantir_token(conn, http=http, agora=AGORA) == "novinho"
    assert http.chamadas == []


# --- URL pública --------------------------------------------------------------

def test_sem_base_url_a_publicacao_e_impossivel(monkeypatch):
    # A API baixa o vídeo por HTTP e não aceita upload de arquivo.
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "")
    with pytest.raises(instagram.ErroInstagram, match="não aceita upload"):
        instagram.url_publica("/render/vid1_7.mp4")


def test_monta_a_url_a_partir_do_nome_do_arquivo():
    assert instagram.url_publica(
        "/render/vid1_7.mp4", base="https://cdn.exemplo/clips/"
    ) == "https://cdn.exemplo/clips/vid1_7.mp4"


# --- publicação em duas etapas ------------------------------------------------

def test_cria_o_container_como_reels(http_falso, resposta_falsa, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "123")
    http = http_falso([resposta_falsa({"id": "cont1"})])

    assert instagram.criar_container(
        "https://cdn/x.mp4", "legenda", "tok", http=http
    ) == "cont1"
    params = http.chamadas[0]["params"]
    assert params["media_type"] == "REELS"
    assert params["video_url"] == "https://cdn/x.mp4"


def test_sem_user_id_falha(monkeypatch, http_falso):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "")
    with pytest.raises(instagram.ErroInstagram, match="INSTAGRAM_USER_ID"):
        instagram.criar_container("u", "c", "t", http=http_falso())


def test_espera_o_processamento_terminar(http_falso, resposta_falsa):
    # Publicar antes do FINISHED devolve erro: a espera é protocolo, não
    # otimização.
    http = http_falso([
        resposta_falsa({"status_code": "IN_PROGRESS"}),
        resposta_falsa({"status_code": "IN_PROGRESS"}),
        resposta_falsa({"status_code": "FINISHED"}),
    ])
    dormidas = []
    assert instagram.esperar_processamento(
        "cont1", "tok", http=http, dormir=dormidas.append
    ) is True
    assert len(http.chamadas) == 3
    assert dormidas == [15, 15]


def test_container_com_erro_falha_na_hora(http_falso, resposta_falsa):
    http = http_falso([resposta_falsa({"status_code": "ERROR"})])
    with pytest.raises(instagram.ErroInstagram, match="ERROR"):
        instagram.esperar_processamento("cont1", "tok", http=http,
                                        dormir=lambda _: None)


def test_espera_tem_teto(http_falso, resposta_falsa):
    http = http_falso([resposta_falsa({"status_code": "IN_PROGRESS"})
                       for _ in range(5)])
    with pytest.raises(instagram.ErroInstagram, match="não ficou pronto"):
        instagram.esperar_processamento(
            "cont1", "tok", http=http, tentativas=3, espera_s=1,
            dormir=lambda _: None,
        )


def test_publica_o_container(http_falso, resposta_falsa, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "123")
    http = http_falso([resposta_falsa({"id": "media9"})])
    media_id, url = instagram.publicar_container("cont1", "tok", http=http)
    assert media_id == "media9"
    assert url.endswith("/reel/media9/")


def test_fluxo_completo(conn, http_falso, resposta_falsa, monkeypatch):
    monkeypatch.setattr(settings, "INSTAGRAM_USER_ID", "123")
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "https://cdn/clips")
    repositorio.salvar_token(conn, "instagram", "tok",
                             (AGORA + timedelta(days=50)).isoformat())
    http = http_falso([
        resposta_falsa({"id": "cont1"}),
        resposta_falsa({"status_code": "FINISHED"}),
        resposta_falsa({"id": "media9"}),
    ])

    media_id, url = instagram.publicar(
        conn, "/render/vid1_7.mp4", "legenda", http=http,
        dormir=lambda _: None, agora=AGORA,
    )
    assert media_id == "media9"
    assert [c["url"].rsplit("/", 1)[-1] for c in http.chamadas] == [
        "media", "cont1", "media_publish"
    ]
