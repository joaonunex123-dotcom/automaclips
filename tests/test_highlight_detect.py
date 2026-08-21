"""Chamada ao Claude: formato da requisição, contrato da resposta, guardas."""
import json
from types import SimpleNamespace

import pytest

import settings
from pipeline import highlight_detect as hd

TRANSCRICAO = "[0.0] boa noite\n[12.4] e aí ele vira pra mim e fala"


def _trecho_api(**kwargs):
    base = {
        "start": 100.0, "end": 145.0, "score": 8.5,
        "motivo": "fecha na virada", "hook": "ele nunca contou isso",
    }
    base.update(kwargs)
    return base


# --- prompt -------------------------------------------------------------------

def test_sistema_e_estavel_byte_a_byte():
    # O cache de prompt é casamento de PREFIXO: um byte diferente entre duas
    # chamadas e o vídeo seguinte reprocessa o system inteiro em vez de lê-lo
    # do cache. Nada de timestamp, uuid ou dict desordenado aqui dentro.
    assert hd.montar_sistema() == hd.montar_sistema()


def test_sistema_declara_a_faixa_e_a_quantidade():
    sistema = hd.montar_sistema(duracao_min=30, duracao_max=60, quantidade=8)
    assert "8 trechos" in sistema
    assert "30 e 60 segundos" in sistema


def test_sistema_sem_exemplos_nao_inventa_nenhum():
    # Sem clip publicado ainda, qualquer exemplo aqui seria palpite
    # apresentado ao modelo como evidência.
    assert "performaram bem" not in hd.montar_sistema()


def test_exemplos_da_etapa_7_entram_no_sistema():
    sistema = hd.montar_sistema(
        exemplos=[{"hook_text": "não faça isso", "motivo": "contraintuitivo"}]
    )
    assert "performaram bem" in sistema
    assert "não faça isso" in sistema


def test_prompt_sem_linguagem_de_pressao():
    # Modelo atual segue instrução de perto; ênfase artificial faz a regra
    # disparar onde não devia.
    sistema = hd.montar_sistema()
    for marcador in ("CRÍTICO", "CRITICAL", "VOCÊ DEVE", "NUNCA ", "SEMPRE "):
        assert marcador not in sistema


def test_prompt_nao_pede_autoverificacao():
    # No Opus 5, pedir "confira sua resposta" produz verificação em excesso.
    for frase in ("confira", "verifique", "revise sua"):
        assert frase not in hd.montar_sistema().lower()


def test_esquema_respeita_o_contrato_de_structured_outputs():
    assert hd.ESQUEMA["additionalProperties"] is False
    item = hd.ESQUEMA["properties"]["trechos"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"start", "end", "score", "motivo", "hook"}


def test_esquema_nao_usa_restricao_numerica():
    # minimum/maximum não são suportados por structured outputs; a faixa é
    # validada em Python (ver _sanear).
    item = hd.ESQUEMA["properties"]["trechos"]["items"]
    for campo in item["properties"].values():
        assert "minimum" not in campo and "maximum" not in campo


# --- cliente ------------------------------------------------------------------

def test_construir_cliente_sem_chave_falha_com_mensagem_util():
    with pytest.raises(hd.ErroHighlight, match="ANTHROPIC_API_KEY"):
        hd.construir_cliente(api_key="")


# --- formato da requisição ----------------------------------------------------

def test_detectar_traduz_para_o_vocabulario_do_banco(cliente_claude):
    cliente = cliente_claude(trechos=[_trecho_api()])
    trechos = hd.detectar(TRANSCRICAO, cliente=cliente)

    assert trechos == [
        {
            "inicio_s": 100.0,
            "fim_s": 145.0,
            "score_claude": 8.5,
            "motivo": "fecha na virada",
            "hook_text": "ele nunca contou isso",
        }
    ]


def test_cache_control_fica_no_system_e_nao_na_transcricao(cliente_claude):
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)

    chamada = cliente.chamadas[0]
    assert chamada["system"][-1]["cache_control"] == {"type": "ephemeral"}
    # A transcrição é o que muda a cada vídeo: marcá-la faria cada vídeo gravar
    # uma entrada nova e nunca ler nenhuma.
    assert "cache_control" not in json.dumps(chamada["messages"])


def test_usa_streaming(cliente_claude):
    # Transcrição longa + raciocínio adaptativo passa do timeout de HTTP da
    # chamada não-streaming.
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert len(cliente.chamadas) == 1        # o duplo só expõe .stream


def test_fallback_usa_a_superficie_beta_com_o_header(cliente_claude):
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente, usar_fallbacks=True)

    chamada = cliente.chamadas[0]
    assert chamada["superficie"] == "beta"
    assert chamada["betas"] == [hd.BETA_FALLBACK]
    assert chamada["fallbacks"] == "default"


def test_sem_fallback_usa_a_superficie_padrao(cliente_claude):
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente, usar_fallbacks=False)

    chamada = cliente.chamadas[0]
    assert chamada["superficie"] == "padrao"
    assert "betas" not in chamada and "fallbacks" not in chamada


def test_pede_json_estruturado_e_raciocinio_adaptativo(cliente_claude):
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente, effort="medium")

    chamada = cliente.chamadas[0]
    assert chamada["thinking"] == {"type": "adaptive"}
    assert chamada["output_config"]["effort"] == "medium"
    assert chamada["output_config"]["format"]["schema"] is hd.ESQUEMA


# --- guarda de custo ----------------------------------------------------------

def test_transcricao_acima_do_teto_nao_chega_a_chamar_a_api(cliente_claude):
    cliente = cliente_claude(trechos=[], input_tokens=500_000)
    with pytest.raises(hd.ErroHighlight, match="excede o teto"):
        hd.detectar(TRANSCRICAO, cliente=cliente, limite_tokens=200_000)
    assert cliente.chamadas == []            # nenhum token pago


def test_teto_zero_desliga_a_guarda(cliente_claude):
    cliente = cliente_claude(trechos=[], input_tokens=10**9)
    hd.detectar(TRANSCRICAO, cliente=cliente, limite_tokens=0)
    assert len(cliente.chamadas) == 1


def test_contagem_de_tokens_inclui_o_system(cliente_claude):
    # Contar só a transcrição subestimaria o prompt e deixaria a guarda passar
    # requisições maiores do que o teto.
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert cliente.contagens[0]["system"][-1]["text"] == hd.montar_sistema()


def test_transcricao_vazia_nao_chama_a_api(cliente_claude):
    cliente = cliente_claude(trechos=[])
    with pytest.raises(hd.ErroHighlight, match="vazia"):
        hd.detectar("   ", cliente=cliente)
    assert cliente.contagens == [] and cliente.chamadas == []


# --- contrato da resposta -----------------------------------------------------

def test_recusa_vira_erro_com_a_categoria(cliente_claude):
    cliente = cliente_claude(
        blocos=[], stop_reason="refusal",
        stop_details=SimpleNamespace(category="cyber"),
    )
    with pytest.raises(hd.ErroHighlight, match="cyber"):
        hd.detectar(TRANSCRICAO, cliente=cliente)


def test_recusa_sem_stop_details_nao_quebra(cliente_claude):
    # stop_details é informativo e pode vir nulo — ramificar por ele em vez de
    # por stop_reason daria AttributeError em vez de mensagem.
    cliente = cliente_claude(blocos=[], stop_reason="refusal")
    with pytest.raises(hd.ErroHighlight, match="recusou"):
        hd.detectar(TRANSCRICAO, cliente=cliente)


def test_resposta_truncada_diz_qual_ajuste_fazer(cliente_claude):
    cliente = cliente_claude(texto='{"trechos":[', stop_reason="max_tokens")
    with pytest.raises(hd.ErroHighlight, match="CLAUDE_MAX_TOKENS"):
        hd.detectar(TRANSCRICAO, cliente=cliente)


def test_bloco_de_fallback_antes_do_texto_nao_atrapalha(cliente_claude, bloco):
    # Com fallback ligado, content pode começar com um bloco `fallback`; pegar
    # content[0] cegamente perderia o JSON.
    cliente = cliente_claude(
        blocos=[
            bloco("fallback"),
            bloco("text", json.dumps({"trechos": [_trecho_api()]})),
        ]
    )
    assert len(hd.detectar(TRANSCRICAO, cliente=cliente)) == 1


def test_resposta_sem_bloco_de_texto(cliente_claude, bloco):
    cliente = cliente_claude(blocos=[bloco("fallback")])
    with pytest.raises(hd.ErroHighlight, match="sem bloco de texto"):
        hd.detectar(TRANSCRICAO, cliente=cliente)


def test_json_invalido_vira_erro_explicito(cliente_claude):
    cliente = cliente_claude(texto="isto não é json")
    with pytest.raises(hd.ErroHighlight, match="JSON"):
        hd.detectar(TRANSCRICAO, cliente=cliente)


def test_resposta_sem_a_chave_trechos(cliente_claude):
    cliente = cliente_claude(texto=json.dumps({"outra_coisa": []}))
    assert hd.detectar(TRANSCRICAO, cliente=cliente) == []


# --- saneamento ---------------------------------------------------------------

def test_score_e_grampeado_na_escala_0_10(cliente_claude):
    # O schema não aceita minimum/maximum, então o corte é aqui.
    cliente = cliente_claude(
        trechos=[_trecho_api(score=42), _trecho_api(start=200, end=245, score=-5)]
    )
    scores = [t["score_claude"] for t in hd.detectar(TRANSCRICAO, cliente=cliente)]
    assert sorted(scores) == [0.0, 10.0]


@pytest.mark.parametrize(
    "invalido",
    [
        {"start": 100, "end": 50, "score": 8},          # fim antes do início
        {"start": -10, "end": 40, "score": 8},          # início negativo
        {"start": 100, "end": 100, "score": 8},         # duração zero
        {"start": "abc", "end": 145, "score": 8},       # não numérico
        {"end": 145, "score": 8},                       # sem start
    ],
)
def test_trechos_impossiveis_sao_descartados(cliente_claude, invalido):
    cliente = cliente_claude(trechos=[invalido, _trecho_api()])
    trechos = hd.detectar(TRANSCRICAO, cliente=cliente)
    assert len(trechos) == 1
    assert trechos[0]["inicio_s"] == 100.0


def test_campos_de_texto_ausentes_viram_string_vazia(cliente_claude):
    cliente = cliente_claude(trechos=[{"start": 10, "end": 50, "score": 7}])
    trecho = hd.detectar(TRANSCRICAO, cliente=cliente)[0]
    assert trecho["motivo"] == "" and trecho["hook_text"] == ""


def test_duracao_nao_e_filtrada_aqui(cliente_claude):
    # Duração é decisão de SELEÇÃO (select_clips), não de integridade do dado:
    # um trecho de 80 s é interpretável, só precisa ser encurtado.
    cliente = cliente_claude(trechos=[_trecho_api(start=100, end=180)])
    assert hd.detectar(TRANSCRICAO, cliente=cliente)[0]["fim_s"] == 180.0


def test_modelo_padrao_e_o_de_settings(cliente_claude):
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert cliente.chamadas[0]["model"] == settings.CLAUDE_MODELO


def test_continua_falando_com_a_anthropic(cliente_claude):
    # A migração para o OpenRouter NÃO alcança esta etapa: escolher o trecho é
    # a decisão que define o produto. O duplo aqui é o cliente da Anthropic
    # (superfície .messages.stream), não o OpenAI-compatível.
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert cliente.chamadas[0]["superficie"] in ("beta", "padrao")
    assert "output_config" in cliente.chamadas[0]


def test_registra_qual_modelo_escolheu(conn, cliente_claude):
    # Ajuda a etapa 7 a não atribuir ao trecho uma diferença que era do modelo.
    from db import repositorio

    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente, conn=conn, referencia="vid1")

    linha = repositorio.geracoes(conn, repositorio.ETAPA_HIGHLIGHT)[0]
    assert linha["modelo_pedido"] == settings.CLAUDE_MODELO
    assert linha["referencia"] == "vid1"


def test_registro_e_opcional(cliente_claude):
    cliente = cliente_claude(trechos=[])
    assert hd.detectar(TRANSCRICAO, cliente=cliente) == []


# --- provedor alternativo (OpenRouter / OpenAI) -------------------------------

def test_padrao_continua_na_anthropic(cliente_claude):
    # Escolher o trecho é a decisão que define o produto; trocar de provedor é
    # decisão de custo consciente, não o caminho por omissão.
    assert settings.HIGHLIGHT_PROVEDOR == "anthropic"
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert cliente.chamadas[0]["superficie"] in ("beta", "padrao")


def test_provedor_openai_usa_o_llm_client(cliente_openrouter):
    cliente = cliente_openrouter([json.dumps({"trechos": [_trecho_api()]})])
    trechos = hd.detectar(TRANSCRICAO, cliente=cliente, provedor="openai")

    assert len(trechos) == 1
    assert trechos[0]["inicio_s"] == 100.0
    assert cliente.chamadas[0]["model"] == settings.MODEL_HIGHLIGHT
    assert cliente.chamadas[0]["response_format"] == {"type": "json_object"}


def test_fora_da_anthropic_o_formato_vai_no_prompt(cliente_openrouter):
    # Sem output_config.format não há JSON garantido: o formato precisa ser
    # DITO, e é derivado do ESQUEMA para os dois não divergirem.
    cliente = cliente_openrouter([json.dumps({"trechos": []})])
    hd.detectar(TRANSCRICAO, cliente=cliente, provedor="openai")

    sistema = cliente.chamadas[0]["messages"][0]["content"]
    for campo in hd.ESQUEMA["properties"]["trechos"]["items"]["properties"]:
        assert f'"{campo}"' in sistema


def test_o_caminho_do_claude_nao_carrega_o_formato_no_prompt(cliente_claude):
    # Lá o schema vai declarado; repeti-lo no texto seria ruído.
    cliente = cliente_claude(trechos=[])
    hd.detectar(TRANSCRICAO, cliente=cliente)
    assert hd.descricao_do_formato() not in cliente.chamadas[0]["system"][0]["text"]


def test_resposta_malformada_cai_no_fallback(cliente_openrouter):
    cliente = cliente_openrouter(
        ["desculpe, nao consigo", json.dumps({"trechos": [_trecho_api()]})]
    )
    assert len(hd.detectar(TRANSCRICAO, cliente=cliente, provedor="openai")) == 1
    assert cliente.chamadas[1]["model"] == settings.MODEL_FALLBACK


def test_guarda_de_custo_aproximada_por_caracteres(cliente_openrouter):
    # Sem contador de tokens do provedor à mão, o teto vira estimativa.
    cliente = cliente_openrouter([json.dumps({"trechos": []})])
    with pytest.raises(hd.ErroHighlight, match="teto aproximado"):
        hd.detectar("x" * 5000, cliente=cliente, provedor="openai",
                    limite_tokens=100)
    assert cliente.chamadas == []


def test_registra_o_modelo_no_caminho_alternativo(conn, cliente_openrouter):
    from db import repositorio

    cliente = cliente_openrouter([json.dumps({"trechos": []})])
    hd.detectar(TRANSCRICAO, cliente=cliente, provedor="openai", conn=conn,
                referencia="vid9")
    linha = repositorio.geracoes(conn, repositorio.ETAPA_HIGHLIGHT)[0]
    assert linha["modelo_pedido"] == settings.MODEL_HIGHLIGHT
    assert linha["referencia"] == "vid9"
