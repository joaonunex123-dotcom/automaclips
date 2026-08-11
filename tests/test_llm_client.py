"""Cliente do OpenRouter: formato da requisição, parsing tolerante, fallback."""
import json

import pytest

import llm_client
import settings


# --- configuração -------------------------------------------------------------

def test_sem_chave_falha_com_mensagem_util(monkeypatch):
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", "")
    with pytest.raises(llm_client.ErroLLM, match="OPENROUTER_API_KEY"):
        llm_client.construir_cliente()


# --- formato da requisição ----------------------------------------------------

def test_system_vai_como_mensagem_separada(cliente_openrouter):
    cliente = cliente_openrouter(['{"ok": 1}'])
    llm_client.call_llm("o contexto", model="m", system="as regras",
                        cliente=cliente)

    mensagens = cliente.chamadas[0]["messages"]
    assert mensagens[0] == {"role": "system", "content": "as regras"}
    assert mensagens[1] == {"role": "user", "content": "o contexto"}


def test_sem_system_manda_so_o_user(cliente_openrouter):
    cliente = cliente_openrouter(['{"ok": 1}'])
    llm_client.call_llm("só isso", model="m", cliente=cliente)
    assert len(cliente.chamadas[0]["messages"]) == 1


def test_pede_json_quando_espera_json(cliente_openrouter):
    cliente = cliente_openrouter(['{"ok": 1}'])
    llm_client.call_llm("p", model="m", cliente=cliente)
    assert cliente.chamadas[0]["response_format"] == {"type": "json_object"}


def test_texto_livre_nao_pede_json(cliente_openrouter):
    cliente = cliente_openrouter(["um texto qualquer"])
    saida = llm_client.call_llm("p", model="m", expect_json=False,
                                cliente=cliente)
    assert saida == "um texto qualquer"
    assert "response_format" not in cliente.chamadas[0]


def test_parametros_opcionais_so_vao_quando_informados(cliente_openrouter):
    cliente = cliente_openrouter(['{"ok": 1}'])
    llm_client.call_llm("p", model="m", cliente=cliente)
    assert "max_tokens" not in cliente.chamadas[0]
    assert "temperature" not in cliente.chamadas[0]


# --- parsing tolerante --------------------------------------------------------

def test_json_limpo():
    assert llm_client.extrair_json('{"a": 1}') == {"a": 1}


def test_json_dentro_de_cerca():
    # Modelo menor costuma embrulhar em ```json.
    assert llm_client.extrair_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_json_dentro_de_cerca_sem_rotulo():
    assert llm_client.extrair_json('```\n{"a": 1}\n```') == {"a": 1}


def test_json_com_prosa_em_volta():
    bruto = 'Claro! Aqui está:\n{"a": 1}\nEspero ter ajudado.'
    assert llm_client.extrair_json(bruto) == {"a": 1}


def test_resposta_vazia():
    with pytest.raises(ValueError, match="vazia"):
        llm_client.extrair_json("   ")


def test_texto_sem_json_nenhum():
    with pytest.raises(ValueError, match="não há JSON"):
        llm_client.extrair_json("desculpe, não posso ajudar com isso")


def test_json_quebrado_nao_e_consertado():
    # O extrator descasca o que está em volta; não inventa sintaxe.
    with pytest.raises(ValueError):
        llm_client.extrair_json('{"a": 1,}')


# --- fallback -----------------------------------------------------------------

def test_resposta_malformada_refaz_no_fallback(cliente_openrouter, caplog):
    cliente = cliente_openrouter(["não vou responder isso", '{"a": 1}'])
    saida = llm_client.call_llm("p", model="barato", fallback_model="forte",
                                cliente=cliente)

    assert saida == {"a": 1}
    assert [c["model"] for c in cliente.chamadas] == ["barato", "forte"]
    assert "refazendo" in caplog.text


def test_fallback_so_entra_quando_precisa(cliente_openrouter):
    cliente = cliente_openrouter(['{"a": 1}'])
    llm_client.call_llm("p", model="barato", fallback_model="forte",
                        cliente=cliente)
    assert len(cliente.chamadas) == 1


def test_sem_fallback_a_falha_sobe(cliente_openrouter):
    cliente = cliente_openrouter(["texto solto"])
    with pytest.raises(llm_client.ErroLLM, match="ininteligível"):
        llm_client.call_llm("p", model="barato", cliente=cliente)


def test_fallback_igual_ao_principal_nao_repete(cliente_openrouter):
    # Refazer no mesmo modelo é pagar duas vezes pelo mesmo resultado.
    cliente = cliente_openrouter(["texto solto"])
    with pytest.raises(llm_client.ErroLLM):
        llm_client.call_llm("p", model="m", fallback_model="m", cliente=cliente)
    assert len(cliente.chamadas) == 1


def test_fallback_tambem_falhando(cliente_openrouter):
    cliente = cliente_openrouter(["lixo", "mais lixo"])
    with pytest.raises(llm_client.ErroLLM, match="nem o fallback"):
        llm_client.call_llm("p", model="barato", fallback_model="forte",
                            cliente=cliente)


# --- detalhes da geração ------------------------------------------------------

def test_detalhes_dizem_quem_respondeu(cliente_openrouter):
    cliente = cliente_openrouter([{"content": '{"a": 1}', "model": "variante-x"}])
    _, detalhes = llm_client.call_llm("p", model="pedido", cliente=cliente,
                                      com_detalhes=True)
    assert detalhes["modelo_pedido"] == "pedido"
    # O OpenRouter roteia para variantes; é quem respondeu que conta.
    assert detalhes["modelo_respondeu"] == "variante-x"
    assert detalhes["usou_fallback"] is False


def test_detalhes_marcam_o_fallback(cliente_openrouter):
    cliente = cliente_openrouter(["lixo", '{"a": 1}'])
    _, detalhes = llm_client.call_llm("p", model="barato", fallback_model="forte",
                                      cliente=cliente, com_detalhes=True)
    assert detalhes["usou_fallback"] is True
    assert detalhes["modelo_pedido"] == "forte"


def test_detalhes_trazem_o_consumo(cliente_openrouter):
    cliente = cliente_openrouter(['{"a": 1}'])
    _, detalhes = llm_client.call_llm("p", model="m", cliente=cliente,
                                      com_detalhes=True)
    assert detalhes["tokens_entrada"] == 120
    assert detalhes["tokens_saida"] == 45


# --- erros da API -------------------------------------------------------------

def test_erro_da_api_preserva_a_mensagem(cliente_openrouter):
    # Slug de modelo errado volta 404 com o nome dentro; deixar a mensagem
    # original passar é o que torna isso diagnosticável.
    cliente = cliente_openrouter([RuntimeError("404: model not found: z-ai/glm-5")])
    with pytest.raises(llm_client.ErroLLM, match="glm-5"):
        llm_client.call_llm("p", model="z-ai/glm-5", cliente=cliente)


def test_resposta_sem_choices(cliente_openrouter):
    from types import SimpleNamespace

    cliente = cliente_openrouter()
    cliente.chat.completions.create = lambda **k: SimpleNamespace(choices=[])
    with pytest.raises(llm_client.ErroLLM, match="sem choices"):
        llm_client.call_llm("p", model="m", cliente=cliente)


def test_erro_na_chamada_do_fallback_sobe(cliente_openrouter):
    cliente = cliente_openrouter(["lixo", RuntimeError("fallback fora do ar")])
    with pytest.raises(llm_client.ErroLLM, match="fora do ar"):
        llm_client.call_llm("p", model="barato", fallback_model="forte",
                            cliente=cliente)
