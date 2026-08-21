"""TikTok: token de 24 h, limites do vídeo e o post que sai privado.

Nada aqui toca a rede: a Content Posting API entra pelo duplo `http_falso`, e
as respostas seguem a forma que a API documenta — inclusive o bloco `error`
com `code: "ok"` que vem junto de toda resposta boa.
"""
from datetime import datetime, timedelta

import pytest

import settings
from db import repositorio
from publish import tiktok

AGORA = datetime(2026, 8, 21, 9, 0, 0)

CRIADOR_LIBERADO = {
    "creator_username": "canal.teste",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "SELF_ONLY"],
    "max_video_post_duration_sec": 600,
}
CRIADOR_RESTRITO = {
    "creator_username": "canal.teste",
    # É assim que um app ainda não revisado aparece: uma opção só, e privada.
    "privacy_level_options": ["SELF_ONLY"],
    "max_video_post_duration_sec": 600,
}


@pytest.fixture
def token_valido(conn):
    """Access token no banco, longe de vencer — não dispara renovação."""
    repositorio.salvar_token(conn, tiktok.SERVICO, "tok-bom",
                             (AGORA + timedelta(hours=12)).isoformat())
    return "tok-bom"


@pytest.fixture
def clip(tmp_path):
    """Um .mp4 de verdade em disco, pequeno o bastante para um pedaço só."""
    caminho = tmp_path / "clip.mp4"
    caminho.write_bytes(b"0" * 2048)
    return str(caminho)


@pytest.fixture
def api(http_falso, resposta_falsa):
    """Sequência de respostas de uma publicação inteira que dá certo."""
    def _fn(criador=None, status="PUBLISH_COMPLETE", publicos=("777",),
            extras=None):
        respostas = [
            resposta_falsa({"data": criador or CRIADOR_LIBERADO,
                            "error": {"code": "ok"}}),
            resposta_falsa({"data": {"publish_id": "pub-1",
                                     "upload_url": "https://upload.tiktok/x"},
                            "error": {"code": "ok"}}),
            resposta_falsa({}),  # o PUT dos bytes
            resposta_falsa({"data": {
                "status": status,
                "publicaly_available_post_id": list(publicos),
            }, "error": {"code": "ok"}}),
        ]
        return http_falso(respostas + list(extras or []))
    return _fn


@pytest.fixture(autouse=True)
def modo_arquivo(monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_MODO_UPLOAD", tiktok.MODO_ARQUIVO)
    monkeypatch.setattr(settings, "TIKTOK_PRIVACIDADE", tiktok.PUBLICO)


# --- token --------------------------------------------------------------------

def test_sem_token_nenhum_falha_com_instrucao(conn, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_ACCESS_TOKEN", "")
    with pytest.raises(tiktok.ErroTikTok, match="TIKTOK_ACCESS_TOKEN"):
        tiktok.token_atual(conn)


def test_token_do_banco_ganha_do_env(conn, monkeypatch, token_valido):
    monkeypatch.setattr(settings, "TIKTOK_ACCESS_TOKEN", "semente")
    assert tiktok.token_atual(conn)[0] == "tok-bom"


def test_sem_validade_conhecida_renova(conn):
    # É o caso do token colado no .env: ele pode ter sido gerado ontem, e
    # ontem já venceu.
    assert tiktok.precisa_renovar(None, agora=AGORA) is True


def test_token_de_24h_renova_perto_do_fim(conn):
    daqui_10_min = (AGORA + timedelta(minutes=10)).isoformat()
    daqui_6_horas = (AGORA + timedelta(hours=6)).isoformat()
    assert tiktok.precisa_renovar(daqui_10_min, AGORA, antecedencia_s=1800)
    assert not tiktok.precisa_renovar(daqui_6_horas, AGORA, antecedencia_s=1800)


def test_renovacao_grava_access_e_refresh(conn, monkeypatch, http_falso,
                                          resposta_falsa):
    # Gravar só o access faria a renovação seguinte falhar com um refresh já
    # queimado: a TikTok invalida o antigo ao emitir o novo.
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_KEY", "chave")
    monkeypatch.setattr(settings, "TIKTOK_CLIENT_SECRET", "segredo")
    http = http_falso([resposta_falsa({
        "access_token": "novo", "refresh_token": "refresh-2",
        "expires_in": 86400,
    })])

    assert tiktok.renovar(conn, refresh="refresh-1", http=http, agora=AGORA) == "novo"
    assert http.chamadas[0]["data"]["grant_type"] == "refresh_token"
    assert repositorio.obter_token(conn, tiktok.SERVICO)["token"] == "novo"
    assert repositorio.obter_token(conn, tiktok.SERVICO_REFRESH)["token"] == "refresh-2"


def test_sem_refresh_token_a_renovacao_explica(conn, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_REFRESH_TOKEN", "")
    with pytest.raises(tiktok.ErroTikTok, match="TIKTOK_REFRESH_TOKEN"):
        tiktok.renovar(conn, agora=AGORA)


def test_falha_na_renovacao_nao_descarta_token_ainda_valido(conn, monkeypatch,
                                                            caplog):
    repositorio.salvar_token(conn, tiktok.SERVICO, "tok",
                             (AGORA + timedelta(minutes=5)).isoformat())
    monkeypatch.setattr(settings, "TIKTOK_REFRESH_TOKEN", "")

    assert tiktok.garantir_token(conn, agora=AGORA) == "tok"
    assert "seguindo com o token atual" in caplog.text


def test_token_expirado_e_sem_renovacao_falha_alto(conn, monkeypatch):
    # Sem validade conhecida não há o que apostar: gastar o upload numa
    # chamada que volta 401 é pior que parar aqui com a mensagem certa.
    monkeypatch.setattr(settings, "TIKTOK_ACCESS_TOKEN", "tok-do-env")
    monkeypatch.setattr(settings, "TIKTOK_REFRESH_TOKEN", "")
    with pytest.raises(tiktok.ErroTikTok, match="refresh token"):
        tiktok.garantir_token(conn, agora=AGORA)


def test_token_recusado_pela_api_vira_erro_legivel(conn, token_valido,
                                                   http_falso, resposta_falsa):
    http = http_falso([resposta_falsa(
        {"error": {"code": "access_token_invalid",
                   "message": "The access token is invalid or expired"}},
        status_code=401,
    )])
    with pytest.raises(tiktok.ErroTikTok, match="access_token_invalid"):
        tiktok.consultar_criador("tok-bom", http=http)


def test_resposta_boa_traz_error_ok_e_nao_e_falha(token_valido, http_falso,
                                                  resposta_falsa):
    # A TikTok manda o bloco `error` em TODA resposta. Tratar a presença da
    # chave como falha (que é a regra do Instagram) recusaria tudo.
    http = http_falso([resposta_falsa({"data": CRIADOR_LIBERADO,
                                       "error": {"code": "ok", "message": ""}})])
    assert tiktok.consultar_criador("tok-bom", http=http) == CRIADOR_LIBERADO


# --- modo restrito: o post sai privado ----------------------------------------

def test_app_nao_revisado_rebaixa_para_privado():
    escolhida, motivo = tiktok.resolver_privacidade(CRIADOR_RESTRITO,
                                                    pedida=tiktok.PUBLICO)
    assert escolhida == tiktok.PRIVADO
    assert "revisão" in motivo


def test_conta_liberada_publica_como_pedido():
    escolhida, motivo = tiktok.resolver_privacidade(CRIADOR_LIBERADO,
                                                    pedida=tiktok.PUBLICO)
    assert (escolhida, motivo) == (tiktok.PUBLICO, "")


def test_conta_sem_opcoes_informadas_segue_o_pedido():
    # Inventar um rebaixamento aqui esconderia um post público.
    escolhida, motivo = tiktok.resolver_privacidade({}, pedida=tiktok.PUBLICO)
    assert (escolhida, motivo) == (tiktok.PUBLICO, "")


def test_modo_restrito_publica_e_avisa_alto(conn, token_valido, clip, api,
                                            caplog):
    # O ponto do teste: publica (não falha) E deixa rastro. Falhar perderia o
    # clip por uma limitação que não se resolve sozinha; sair calado faria o
    # primeiro post privado parecer bug de código.
    http = api(criador=CRIADOR_RESTRITO, publicos=())
    id_externo, url = tiktok.publicar(conn, clip, "legenda", http=http,
                                      agora=AGORA)

    assert "modo restrito" in caplog.text
    corpo_init = http.chamadas[1]["json"]
    assert corpo_init["post_info"]["privacy_level"] == tiktok.PRIVADO
    # Post privado não ganha id público: fica o publish_id, e a URL some em
    # vez de apontar para uma página que ninguém abre.
    assert (id_externo, url) == ("pub-1", "")


def test_post_publico_grava_id_e_url(conn, token_valido, clip, api):
    http = api()
    id_externo, url = tiktok.publicar(conn, clip, "legenda", http=http,
                                      agora=AGORA)
    assert id_externo == "777"
    assert url == "https://www.tiktok.com/@canal.teste/video/777"


def test_criador_que_desligou_comentario_manda_no_post(conn, token_valido,
                                                       clip, api):
    # Tentar ligar comentário num perfil que os desligou faz a API recusar o
    # post inteiro.
    criador = dict(CRIADOR_LIBERADO, comment_disabled=True)
    http = api(criador=criador)
    tiktok.publicar(conn, clip, "legenda", http=http, agora=AGORA)
    assert http.chamadas[1]["json"]["post_info"]["disable_comment"] is True


# --- limites do vídeo ---------------------------------------------------------

def test_formato_nao_aceito_para_antes_de_subir(tmp_path):
    caminho = tmp_path / "clip.avi"
    caminho.write_bytes(b"0" * 100)
    with pytest.raises(tiktok.VideoForaDosLimites, match="formato"):
        tiktok.validar_video(str(caminho))


def test_clip_longo_demais_para_a_conta(clip):
    with pytest.raises(tiktok.VideoForaDosLimites, match="passa do máximo"):
        tiktok.validar_video(clip, duracao_s=700, max_duracao_s=600)


def test_arquivo_grande_demais(clip, monkeypatch):
    monkeypatch.setattr(settings, "TIKTOK_TAMANHO_MAXIMO_MB", 0.001)
    with pytest.raises(tiktok.VideoForaDosLimites, match="MB"):
        tiktok.validar_video(clip)


def test_arquivo_que_sumiu(tmp_path):
    with pytest.raises(tiktok.VideoForaDosLimites, match="não encontrado"):
        tiktok.validar_video(str(tmp_path / "nao_existe.mp4"))


def test_arquivo_vazio(tmp_path):
    caminho = tmp_path / "vazio.mp4"
    caminho.write_bytes(b"")
    with pytest.raises(tiktok.VideoForaDosLimites, match="vazio"):
        tiktok.validar_video(str(caminho))


def test_limite_da_conta_ganha_do_default(conn, token_valido, clip, api):
    # A conta manda: o default do settings é o teto de quem ainda não
    # perguntou, e perguntar é o creator_info.
    criador = dict(CRIADOR_LIBERADO, max_video_post_duration_sec=15)
    http = api(criador=criador)
    with pytest.raises(tiktok.VideoForaDosLimites, match="15s"):
        tiktok.publicar(conn, clip, "legenda", duracao_s=45, http=http,
                        agora=AGORA)


def test_video_fora_dos_limites_nao_chega_a_subir(conn, token_valido, clip, api):
    criador = dict(CRIADOR_LIBERADO, max_video_post_duration_sec=15)
    http = api(criador=criador)
    with pytest.raises(tiktok.VideoForaDosLimites):
        tiktok.publicar(conn, clip, "legenda", duracao_s=45, http=http,
                        agora=AGORA)
    # Só o creator_info foi chamado: nem init, nem upload.
    assert len(http.chamadas) == 1


# --- upload em pedaços --------------------------------------------------------

def test_clip_pequeno_vai_num_pedaco_so():
    chunk, quantidade, faixas = tiktok.plano_de_chunks(2048)
    assert (chunk, quantidade, faixas) == (2048, 1, [(0, 2047)])


def test_ultimo_pedaco_engole_a_sobra():
    # Fatiar em partes iguais deixaria uma sobra menor que o mínimo de 5 MB,
    # que a API recusa.
    tamanho = 150 * tiktok.MB
    chunk, quantidade, faixas = tiktok.plano_de_chunks(tamanho, chunk_mb=64)
    assert quantidade == 2
    assert faixas[-1][1] == tamanho - 1
    assert faixas[0][1] - faixas[0][0] + 1 == chunk


def test_upload_manda_content_range_de_cada_pedaco(conn, token_valido, clip,
                                                   api):
    http = api()
    tiktok.publicar(conn, clip, "legenda", http=http, agora=AGORA)
    put = http.chamadas[2]
    assert put["metodo"] == "PUT"
    assert put["headers"]["Content-Range"] == "bytes 0-2047/2048"
    assert put["data"] == b"0" * 2048


def test_init_declara_o_tamanho_do_arquivo(conn, token_valido, clip, api):
    http = api()
    tiktok.publicar(conn, clip, "legenda", http=http, agora=AGORA)
    origem = http.chamadas[1]["json"]["source_info"]
    assert origem["source"] == "FILE_UPLOAD"
    assert origem["video_size"] == 2048
    assert origem["total_chunk_count"] == 1


# --- modo url -----------------------------------------------------------------

def test_modo_url_manda_a_tiktok_baixar(conn, monkeypatch, token_valido, clip,
                                        http_falso, resposta_falsa):
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "https://cdn/clips")
    http = http_falso([
        resposta_falsa({"data": CRIADOR_LIBERADO, "error": {"code": "ok"}}),
        resposta_falsa({"data": {"publish_id": "pub-1"}, "error": {"code": "ok"}}),
        resposta_falsa({"data": {"status": "PUBLISH_COMPLETE",
                                 "publicaly_available_post_id": ["777"]},
                        "error": {"code": "ok"}}),
    ])
    tiktok.publicar(conn, clip, "legenda", http=http, agora=AGORA,
                    modo=tiktok.MODO_URL)

    origem = http.chamadas[1]["json"]["source_info"]
    assert origem["source"] == "PULL_FROM_URL"
    assert origem["video_url"] == "https://cdn/clips/clip.mp4"
    # Nenhum PUT: quem baixa é a TikTok.
    assert [c["metodo"] for c in http.chamadas] == ["POST", "POST", "POST"]


def test_modo_url_sem_base_url_explica_a_saida(monkeypatch):
    monkeypatch.setattr(settings, "CLIPS_BASE_URL", "")
    with pytest.raises(tiktok.ErroTikTok, match="TIKTOK_MODO_UPLOAD=arquivo"):
        tiktok.url_publica("/render/clip.mp4")


# --- acompanhamento -----------------------------------------------------------

def test_publicacao_que_falha_no_processamento(conn, token_valido, http_falso,
                                               resposta_falsa):
    http = http_falso([resposta_falsa(
        {"data": {"status": "FAILED", "fail_reason": "video_format_invalid"},
         "error": {"code": "ok"}}
    )])
    with pytest.raises(tiktok.ErroTikTok, match="video_format_invalid"):
        tiktok.esperar_publicacao("pub-1", "tok", http=http, dormir=lambda _s: None)


def test_espera_tem_teto(conn, token_valido, http_falso, resposta_falsa):
    esperas = []
    http = http_falso([
        resposta_falsa({"data": {"status": "PROCESSING_UPLOAD"},
                        "error": {"code": "ok"}}) for _ in range(3)
    ])
    with pytest.raises(tiktok.ErroTikTok, match="não terminou"):
        tiktok.esperar_publicacao("pub-1", "tok", http=http, tentativas=3,
                                  espera_s=5, dormir=esperas.append)
    assert esperas == [5, 5]


def test_rascunho_na_caixa_de_entrada_avisa_do_escopo(conn, http_falso,
                                                      resposta_falsa, caplog):
    # Sem video.publish o vídeo vira rascunho e espera alguém postar pelo
    # celular. Não é falha, mas também não é post.
    http = http_falso([resposta_falsa(
        {"data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"}}
    )])
    tiktok.esperar_publicacao("pub-1", "tok", http=http, dormir=lambda _s: None)
    assert "video.upload" in caplog.text
