"""Provedores de LLM. Cada um gera o texto aos poucos, por um async generator.

Escolha pelo JARVIS_LLM:
  - mock: texto falso com atraso. Testa a interface sem modelo nenhum.
  - llamacpp: llama-server em LLAMA_URL, pela API compatível com OpenAI
    (POST /v1/chat/completions com stream=true).

Cada pedaço gerado conta como um token. Não é a contagem exata do tokenizador,
mas o llama-server manda um token por pedaço, e a métrica tokens/s fica boa.
"""
import asyncio
import json
import re

import httpx

from .config import Config, ErroConfiguracao


class ErroLLM(Exception):
    """Falha ao gerar: provedor fora do ar, resposta malformada, erro do modelo."""


class ProvedorMock:
    nome = "mock"

    def __init__(self, atraso_s: float = 0.05):
        self.atraso_s = atraso_s

    async def gerar(self, mensagens):
        ultima = next(
            (m["content"] for m in reversed(mensagens) if m["role"] == "user"), ""
        )
        resposta = (
            "Esta é uma resposta falsa do provedor mock. "
            f"Você disse: {ultima}"
        )
        for pedaco in re.findall(r"\S+\s*", resposta):
            await asyncio.sleep(self.atraso_s)
            yield pedaco


_FIM = object()


def _pedaco_do_evento(linha: str):
    """Uma linha do stream SSE -> texto, None (sem texto) ou _FIM."""
    linha = linha.strip()
    if not linha.startswith("data:"):
        return None
    dados = linha[len("data:"):].strip()
    if dados == "[DONE]":
        return _FIM
    try:
        evento = json.loads(dados)
    except json.JSONDecodeError as e:
        raise ErroLLM(f"evento inválido do llama-server: {dados[:120]!r}") from e
    if "error" in evento:
        raise ErroLLM(f"llama-server devolveu erro: {evento['error']}")
    escolhas = evento.get("choices") or []
    if not escolhas:
        return None
    delta = escolhas[0].get("delta") or {}
    return delta.get("content") or None


class ProvedorLlamaCpp:
    nome = "llamacpp"

    def __init__(self, url: str, timeout_s: float = 120.0, max_tokens: int = 512,
                 transporte=None):
        # `transporte` existe para os testes (httpx.MockTransport), sem rede.
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.transporte = transporte

    async def gerar(self, mensagens):
        corpo = {"messages": mensagens, "stream": True}
        if self.max_tokens:
            corpo["max_tokens"] = self.max_tokens
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s,
                                         transport=self.transporte) as cliente:
                async with cliente.stream(
                    "POST", f"{self.url}/v1/chat/completions", json=corpo
                ) as resposta:
                    if resposta.status_code != 200:
                        trecho = (await resposta.aread()).decode("utf-8", "replace")[:200]
                        raise ErroLLM(
                            f"llama-server respondeu {resposta.status_code}: {trecho}"
                        )
                    async for linha in resposta.aiter_lines():
                        pedaco = _pedaco_do_evento(linha)
                        if pedaco is _FIM:
                            break
                        if pedaco:
                            yield pedaco
        except httpx.HTTPError as e:
            raise ErroLLM(f"falha ao falar com o llama-server em {self.url}: {e}") from e


def criar_provedor(cfg: Config):
    if cfg.llm == "mock":
        return ProvedorMock(cfg.mock_atraso_s)
    if cfg.llm == "llamacpp":
        return ProvedorLlamaCpp(cfg.llama_url, cfg.llama_timeout_s, cfg.llama_max_tokens)
    raise ErroConfiguracao(f"JARVIS_LLM desconhecido: {cfg.llm!r}")
