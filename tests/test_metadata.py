"""Geração de título, descrição, caption e hashtags."""
import json

import pytest

import settings
from publish import metadata

CLIP = {
    "hook_text": "ele nunca contou isso",
    "motivo": "fecha na virada",
    "inicio_s": 100.0,
    "fim_s": 145.0,
}

RESPOSTA = {
    "titulo": "O que ele nunca tinha contado",
    "descricao": "A história completa do que aconteceu depois da reunião.",
    "caption": "não dava pra prever esse final",
    "hashtags": ["#Podcast", "entrevista", "bastidores"],
}


def _cliente(cliente_claude, dados=None, **kwargs):
    return cliente_claude(texto=json.dumps(dados or RESPOSTA), **kwargs)


# --- prompt -------------------------------------------------------------------

def test_sistema_e_estavel_byte_a_byte():
    # Cache de prompt é casamento de prefixo: um byte diferente e cada clip
    # reprocessa o system em vez de lê-lo do cache.
    assert metadata.montar_sistema() == metadata.montar_sistema()


def test_sistema_declara_os_limites():
    sistema = metadata.montar_sistema(max_hashtags=6, limite_titulo=80)
    assert "80 caracteres" in sistema
    assert "6 hashtags" in sistema


def test_prompt_sem_linguagem_de_pressao():
    sistema = metadata.montar_sistema()
    for marcador in ("CRÍTICO", "CRITICAL", "VOCÊ DEVE", "NUNCA ", "SEMPRE "):
        assert marcador not in sistema


def test_prompt_desaconselha_hashtag_generica():
    assert "#viral" in metadata.montar_sistema()


def test_contexto_traz_o_clip_e_a_origem():
    contexto = metadata.montar_contexto(
        CLIP, fala="a virada acontece aqui", titulo_fonte="Entrevista X",
        canal="Canal Y",
    )
    assert "Canal Y" in contexto
    assert "Entrevista X" in contexto
    assert "ele nunca contou isso" in contexto
    assert "a virada acontece aqui" in contexto


def test_contexto_sem_transcricao_avisa():
    assert "sem transcrição" in metadata.montar_contexto(CLIP)


# --- fala do trecho -----------------------------------------------------------

def test_fala_pega_so_o_trecho(transcricao):
    # Mandar as quatro horas do vídeo custaria caro para piorar o resultado: o
    # modelo passaria a resumir o vídeo, não o clip.
    t = transcricao(
        (10.0, 14.0, "abertura do programa"),
        (100.0, 120.0, "a virada"),
        (500.0, 505.0, "encerramento"),
    )
    assert metadata.fala_do_trecho(t, 100.0, 145.0) == "a virada"


def test_fala_de_transcricao_vazia():
    assert metadata.fala_do_trecho({"segmentos": []}, 0, 45) == ""
    assert metadata.fala_do_trecho(None, 0, 45) == ""


# --- normalização de hashtags -------------------------------------------------

def test_hashtags_perdem_cerquilha_e_pontuacao():
    assert metadata.normalizar_hashtags(["#Meu Assunto!", "Outro-Tema"]) == [
        "meuassunto", "outrotema"
    ]


def test_hashtags_repetidas_saem_uma_vez():
    assert metadata.normalizar_hashtags(["#Podcast", "podcast", "PODCAST"]) == [
        "podcast"
    ]


def test_hashtags_preservam_acento():
    assert metadata.normalizar_hashtags(["educação"]) == ["educação"]


def test_hashtags_respeitam_o_teto():
    assert len(metadata.normalizar_hashtags([f"tag{i}" for i in range(20)],
                                            maximo=5)) == 5


def test_hashtag_que_vira_vazia_e_descartada():
    assert metadata.normalizar_hashtags(["#", "!!!", "boa"]) == ["boa"]


# --- limites ------------------------------------------------------------------

def test_titulo_e_cortado_no_limite_da_plataforma(cliente_claude):
    # O schema não aceita maxLength, e "peça ao modelo para não passar de 100"
    # é o tipo de restrição cumprida quase sempre — o que significa post
    # recusado de vez em quando, no horário agendado, sem ninguém olhando.
    longo = {**RESPOSTA, "titulo": "palavra " * 40}
    meta = metadata.gerar(CLIP, cliente=_cliente(cliente_claude, longo))
    assert len(meta["titulo"]) <= settings.LIMITE_TITULO_YOUTUBE


def test_corte_nao_parte_palavra_no_meio():
    assert metadata._cortar("uma frase bem comprida aqui", 12) == "uma frase"


def test_corte_muito_curto_ainda_devolve_texto():
    # Respeitar a palavra não pode devolver string vazia.
    assert metadata._cortar("supercalifragilistico", 5) != ""


def test_espacos_sao_colapsados():
    assert metadata._cortar("  duas   linhas\n  aqui ", 100) == "duas linhas aqui"


# --- geração ------------------------------------------------------------------

def test_gera_o_metadado_completo(cliente_claude):
    meta = metadata.gerar(CLIP, cliente=_cliente(cliente_claude))
    assert meta["titulo"] == "O que ele nunca tinha contado"
    assert meta["hashtags"] == ["podcast", "entrevista", "bastidores"]


def test_cache_control_fica_no_system(cliente_claude):
    cliente = _cliente(cliente_claude)
    metadata.gerar(CLIP, cliente=cliente)
    chamada = cliente.chamadas[0]
    assert chamada["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in json.dumps(chamada["messages"])


def test_usa_saida_estruturada(cliente_claude):
    cliente = _cliente(cliente_claude)
    metadata.gerar(CLIP, cliente=cliente)
    formato = cliente.chamadas[0]["output_config"]["format"]
    assert formato["schema"] is metadata.ESQUEMA


def test_titulo_vazio_e_erro(cliente_claude):
    # Um título vazio só apareceria na hora do upload, no horário agendado.
    cliente = _cliente(cliente_claude, {**RESPOSTA, "titulo": "   "})
    with pytest.raises(metadata.ErroMetadata, match="título vazio"):
        metadata.gerar(CLIP, cliente=cliente)


def test_recusa_vira_erro_do_modulo(cliente_claude):
    cliente = cliente_claude(blocos=[], stop_reason="refusal")
    with pytest.raises(metadata.ErroMetadata, match="recusou"):
        metadata.gerar(CLIP, cliente=cliente)


def test_json_invalido(cliente_claude):
    with pytest.raises(metadata.ErroMetadata, match="JSON"):
        metadata.gerar(CLIP, cliente=cliente_claude(texto="não é json"))


# --- montagem por plataforma --------------------------------------------------

def test_youtube_junta_credito_e_hashtags():
    meta = metadata.normalizar(RESPOSTA)
    saida = metadata.para_youtube(meta, url_fonte="https://y.tube/abc")
    assert saida["titulo"] == "O que ele nunca tinha contado"
    assert "https://y.tube/abc" in saida["descricao"]
    assert "#podcast" in saida["descricao"]
    assert saida["tags"] == ["podcast", "entrevista", "bastidores"]


def test_youtube_sem_url_de_origem_nao_deixa_credito_vazio():
    saida = metadata.para_youtube(metadata.normalizar(RESPOSTA))
    assert "Trecho de:" not in saida["descricao"]


def test_instagram_poe_as_hashtags_no_fim():
    saida = metadata.para_instagram(metadata.normalizar(RESPOSTA))
    assert saida["caption"].startswith("não dava pra prever")
    assert saida["caption"].rstrip().endswith("#bastidores")


def test_instagram_cai_no_titulo_sem_caption():
    meta = metadata.normalizar({**RESPOSTA, "caption": ""})
    assert meta["titulo"] in metadata.para_instagram(meta)["caption"]


def test_instagram_respeita_o_limite():
    meta = metadata.normalizar({**RESPOSTA, "caption": "x" * 3000})
    saida = metadata.para_instagram(meta)
    assert len(saida["caption"]) <= settings.LIMITE_CAPTION_INSTAGRAM
