"""Provedores de LLM. O llama-server é simulado com httpx.MockTransport: o código
de streaming e de parsing de SSE é o de produção, só a rede é trocada."""
import asyncio
import json

import httpx
import pytest

from backend.llm import ErroLLM, ProvedorLlamaCpp, ProvedorMock, criar_provedor
from backend.config import ErroConfiguracao, carregar_config

from tests.constantes import TOKEN


async def _coletar(gerador):
    return [pedaco async for pedaco in gerador]


def _sse(*conteudos, fim=True):
    linhas = []
    for c in conteudos:
        evento = {"choices": [{"delta": {"content": c}}]}
        linhas.append(f"data: {json.dumps(evento)}\n\n")
    if fim:
        linhas.append("data: [DONE]\n\n")
    return "".join(linhas).encode("utf-8")


def _provedor_com(handler):
    return ProvedorLlamaCpp("http://llama.test:8080/", timeout_s=5,
                            max_tokens=64, transporte=httpx.MockTransport(handler))


MENSAGENS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "oi"}]


def test_mock_ecoa_a_ultima_pergunta_em_pedacos():
    texto = "".join(asyncio.run(_coletar(ProvedorMock(0).gerar(MENSAGENS))))
    assert "Você disse: oi" in texto


def test_mock_faz_streaming_com_atraso():
    tempos = []
    provedor = ProvedorMock(0.01)

    async def cenario():
        loop = asyncio.get_running_loop()
        inicio = loop.time()
        async for _ in provedor.gerar(MENSAGENS):
            tempos.append(loop.time() - inicio)

    asyncio.run(cenario())
    assert len(tempos) > 3
    assert tempos[-1] >= tempos[0] + 0.02  # os pedaços não chegaram de uma vez


def test_llamacpp_le_stream_sse_e_ignora_delta_vazio():
    capturado = {}

    def handler(request):
        capturado["caminho"] = request.url.path
        capturado["corpo"] = json.loads(request.content)
        corpo = (
            'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}\n\n'
            + _sse("Olá", ", ", "mundo").decode("utf-8")
        )
        return httpx.Response(200, content=corpo.encode("utf-8"),
                              headers={"content-type": "text/event-stream"})

    pedacos = asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS)))
    assert pedacos == ["Olá", ", ", "mundo"]
    assert capturado["caminho"] == "/v1/chat/completions"
    assert capturado["corpo"]["stream"] is True
    assert capturado["corpo"]["max_tokens"] == 64
    assert capturado["corpo"]["messages"] == MENSAGENS


def test_llamacpp_para_no_done_mesmo_com_lixo_depois():
    def handler(request):
        corpo = _sse("a", fim=True) + b'data: {"choices":[{"delta":{"content":"fora"}}]}\n\n'
        return httpx.Response(200, content=corpo)

    assert asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS))) == ["a"]


def test_llamacpp_status_de_erro_vira_erro_llm():
    def handler(request):
        return httpx.Response(503, content=b"modelo carregando")

    with pytest.raises(ErroLLM, match="503"):
        asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS)))


def test_llamacpp_evento_invalido_vira_erro_llm():
    def handler(request):
        return httpx.Response(200, content=b"data: {isto nao e json\n\n")

    with pytest.raises(ErroLLM, match="evento inválido"):
        asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS)))


def test_llamacpp_erro_dentro_do_stream_vira_erro_llm():
    def handler(request):
        return httpx.Response(200, content=b'data: {"error": "contexto estourado"}\n\n')

    with pytest.raises(ErroLLM, match="contexto estourado"):
        asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS)))


def test_llamacpp_fora_do_ar_vira_erro_llm():
    def handler(request):
        raise httpx.ConnectError("conexão recusada")

    with pytest.raises(ErroLLM, match="falha ao falar com o llama-server"):
        asyncio.run(_coletar(_provedor_com(handler).gerar(MENSAGENS)))


def test_criar_provedor_segue_a_config():
    mock = criar_provedor(carregar_config({"JARVIS_TOKEN": TOKEN}))
    assert isinstance(mock, ProvedorMock)
    real = criar_provedor(carregar_config({"JARVIS_TOKEN": TOKEN, "JARVIS_LLM": "llamacpp"}))
    assert isinstance(real, ProvedorLlamaCpp)
    assert real.url == "http://127.0.0.1:8080"


def test_criar_provedor_rejeita_nome_desconhecido():
    cfg = carregar_config({"JARVIS_TOKEN": TOKEN})
    cfg = type(cfg)(**{**cfg.__dict__, "llm": "outro"})
    with pytest.raises(ErroConfiguracao):
        criar_provedor(cfg)
