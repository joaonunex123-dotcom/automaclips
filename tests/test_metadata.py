"""Geração de título, descrição, caption e hashtags."""
import json

import pytest

import settings
from db import repositorio
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


def _cliente(cliente_openrouter, dados=None, **kwargs):
    return cliente_openrouter([json.dumps(dados or RESPOSTA)], **kwargs)


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

def test_titulo_e_cortado_no_limite_da_plataforma(cliente_openrouter):
    # O schema não aceita maxLength, e "peça ao modelo para não passar de 100"
    # é o tipo de restrição cumprida quase sempre — o que significa post
    # recusado de vez em quando, no horário agendado, sem ninguém olhando.
    longo = {**RESPOSTA, "titulo": "palavra " * 40}
    meta = metadata.gerar(CLIP, cliente=_cliente(cliente_openrouter, longo))
    assert len(meta["titulo"]) <= settings.LIMITE_TITULO_YOUTUBE


def test_corte_nao_parte_palavra_no_meio():
    assert metadata._cortar("uma frase bem comprida aqui", 12) == "uma frase"


def test_corte_muito_curto_ainda_devolve_texto():
    # Respeitar a palavra não pode devolver string vazia.
    assert metadata._cortar("supercalifragilistico", 5) != ""


def test_espacos_sao_colapsados():
    assert metadata._cortar("  duas   linhas\n  aqui ", 100) == "duas linhas aqui"


# --- geração ------------------------------------------------------------------

def test_gera_o_metadado_completo(cliente_openrouter):
    meta = metadata.gerar(CLIP, cliente=_cliente(cliente_openrouter))
    assert meta["titulo"] == "O que ele nunca tinha contado"
    assert meta["hashtags"] == ["podcast", "entrevista", "bastidores"]


def test_system_e_contexto_vao_separados(cliente_openrouter):
    cliente = _cliente(cliente_openrouter)
    metadata.gerar(CLIP, cliente=cliente)
    mensagens = cliente.chamadas[0]["messages"]
    assert mensagens[0]["role"] == "system"
    assert mensagens[1]["role"] == "user"
    assert "ele nunca contou isso" in mensagens[1]["content"]


def test_pede_json_ao_modelo(cliente_openrouter):
    cliente = _cliente(cliente_openrouter)
    metadata.gerar(CLIP, cliente=cliente)
    assert cliente.chamadas[0]["response_format"] == {"type": "json_object"}


def test_usa_o_modelo_de_metadata_e_o_fallback(cliente_openrouter):
    cliente = _cliente(cliente_openrouter)
    metadata.gerar(CLIP, cliente=cliente)
    assert cliente.chamadas[0]["model"] == settings.MODEL_METADATA


def test_resposta_malformada_cai_no_fallback(cliente_openrouter):
    # Sem saída estruturada garantida, resposta ruim deixa de ser impossível e
    # passa a ser rara — o fallback impede que "rara" vire "perdeu o clip".
    cliente = cliente_openrouter(["desculpe, não posso", json.dumps(RESPOSTA)])
    meta = metadata.gerar(CLIP, cliente=cliente)
    assert meta["titulo"] == "O que ele nunca tinha contado"
    assert cliente.chamadas[1]["model"] == settings.MODEL_FALLBACK


def test_json_dentro_de_cerca_e_aceito_sem_fallback(cliente_openrouter):
    cliente = cliente_openrouter([f"```json\n{json.dumps(RESPOSTA)}\n```"])
    assert metadata.gerar(CLIP, cliente=cliente)["titulo"]
    assert len(cliente.chamadas) == 1


def test_formato_do_json_e_derivado_do_esquema():
    # Escrever a forma à mão no prompt criaria duas fontes de verdade que se
    # afastam na primeira vez que alguém acrescentar um campo.
    descricao = metadata.descricao_do_formato()
    for campo in metadata.ESQUEMA["properties"]:
        assert f'"{campo}"' in descricao
    assert descricao in metadata.montar_sistema()


def test_titulo_vazio_e_erro(cliente_openrouter):
    # Um título vazio só apareceria na hora do upload, no horário agendado.
    cliente = _cliente(cliente_openrouter, {**RESPOSTA, "titulo": "   "})
    with pytest.raises(metadata.ErroMetadata, match="título vazio"):
        metadata.gerar(CLIP, cliente=cliente)


def test_erro_da_api_vira_erro_do_modulo(cliente_openrouter):
    cliente = cliente_openrouter([RuntimeError("404: model not found")])
    with pytest.raises(metadata.ErroMetadata, match="model not found"):
        metadata.gerar(CLIP, cliente=cliente)


def test_resposta_ininteligivel_nos_dois_modelos(cliente_openrouter):
    cliente = cliente_openrouter(["não é json", "também não"])
    with pytest.raises(metadata.ErroMetadata, match="nem o fallback"):
        metadata.gerar(CLIP, cliente=cliente)


def test_json_que_nao_e_objeto(cliente_openrouter):
    cliente = cliente_openrouter(['["uma", "lista"]', '["ainda", "lista"]'])
    with pytest.raises(metadata.ErroMetadata, match="não é um objeto"):
        metadata.gerar(CLIP, cliente=cliente)


def test_registra_qual_modelo_respondeu(conn, cliente_openrouter):
    # Sem isso, a etapa 7 compararia clips sem saber que metade do texto veio
    # de um modelo e metade de outro.
    cliente = cliente_openrouter([json.dumps(RESPOSTA)],
                                 modelo_respondeu="variante-x")
    metadata.gerar({**CLIP, "id": 7}, cliente=cliente, conn=conn)

    linha = repositorio.geracoes(conn, repositorio.ETAPA_METADATA)[0]
    assert linha["modelo_pedido"] == settings.MODEL_METADATA
    assert linha["modelo_respondeu"] == "variante-x"
    assert linha["referencia"] == "7"
    assert linha["usou_fallback"] == 0


def test_registra_quando_o_fallback_entra(conn, cliente_openrouter):
    cliente = cliente_openrouter(["lixo", json.dumps(RESPOSTA)])
    metadata.gerar({**CLIP, "id": 7}, cliente=cliente, conn=conn)
    assert repositorio.geracoes(conn, repositorio.ETAPA_METADATA)[0][
        "usou_fallback"] == 1


def test_gera_sem_banco(cliente_openrouter):
    # A geração não depende de conn; é o que a mantém testável isolada.
    assert metadata.gerar(CLIP, cliente=_cliente(cliente_openrouter))["titulo"]


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


# --- por plataforma: TikTok ---------------------------------------------------

POR_PLATAFORMA = {
    **RESPOSTA,
    "caption_tiktok": "ele não esperava essa resposta",
    "hashtags_instagram": ["bastidores"],
    "hashtags_tiktok": ["cortesdepodcast", "entrevista"],
}


def test_prompt_cobre_as_tres_plataformas():
    sistema = metadata.montar_sistema()
    for plataforma in ("YouTube", "Instagram", "TikTok"):
        assert plataforma in sistema


def test_hashtags_por_plataforma_ficam_separadas():
    meta = metadata.normalizar(POR_PLATAFORMA)
    assert meta["hashtags_tiktok"] == ["cortesdepodcast", "entrevista"]
    assert meta["hashtags_instagram"] == ["bastidores"]
    assert meta["hashtags"] == ["podcast", "entrevista", "bastidores"]


def test_sem_campo_por_plataforma_cai_nas_gerais():
    # O modelo de fallback é mais velho e mais simples, e devolve só
    # `hashtags`. Um clip sem hashtag do TikTok publica com as gerais; um
    # clip sem hashtag nenhuma não publica bem em lugar nenhum.
    meta = metadata.normalizar(RESPOSTA)
    assert meta["hashtags_tiktok"] == meta["hashtags"]
    assert meta["hashtags_instagram"] == meta["hashtags"]


def test_caption_do_tiktok_e_cortada_bem_antes_do_teto_da_api():
    # 2200 caracteres cabem na API, mas o leitor vê duas linhas antes do
    # "mais". Cortar só pelo teto daria um texto válido que ninguém lê.
    meta = metadata.normalizar({**RESPOSTA, "caption_tiktok": "palavra " * 200})
    assert len(meta["caption_tiktok"]) <= settings.LIMITE_CORPO_TIKTOK
    assert len(meta["caption_tiktok"]) < settings.LIMITE_CAPTION_TIKTOK


def test_tiktok_usa_a_propria_caption_e_as_proprias_hashtags():
    saida = metadata.para_tiktok(metadata.normalizar(POR_PLATAFORMA))
    assert saida["caption"].startswith("ele não esperava")
    assert "#cortesdepodcast" in saida["caption"]
    assert "#bastidores" not in saida["caption"]


def test_tiktok_cai_na_caption_do_instagram_quando_falta_a_sua():
    saida = metadata.para_tiktok(metadata.normalizar(RESPOSTA))
    assert saida["caption"].startswith("não dava pra prever")


def test_tiktok_respeita_o_teto_da_api():
    meta = metadata.normalizar(POR_PLATAFORMA)
    meta["hashtags_tiktok"] = ["x" * 400] * 8
    assert len(metadata.para_tiktok(meta)["caption"]) <= settings.LIMITE_CAPTION_TIKTOK


def test_instagram_nao_leva_as_hashtags_do_tiktok():
    saida = metadata.para_instagram(metadata.normalizar(POR_PLATAFORMA))
    assert saida["caption"].rstrip().endswith("#bastidores")
    assert "#cortesdepodcast" not in saida["caption"]


def test_hashtags_de_escolhe_pela_plataforma():
    meta = metadata.normalizar(POR_PLATAFORMA)
    assert metadata.hashtags_de(meta, settings.PLATAFORMA_TIKTOK) == [
        "cortesdepodcast", "entrevista"]
    assert metadata.hashtags_de(meta, settings.PLATAFORMA_INSTAGRAM) == ["bastidores"]
    # O YouTube fica com o conjunto geral: lá a hashtag entra na descrição e
    # como tag, e não é a mesma disputa de busca.
    assert metadata.hashtags_de(meta, settings.PLATAFORMA_YOUTUBE) == meta["hashtags"]


def test_formato_do_json_declara_os_campos_por_plataforma():
    formato = metadata.descricao_do_formato()
    assert "hashtags_tiktok" in formato
    assert "hashtags_instagram" in formato
    assert "caption_tiktok" in formato
