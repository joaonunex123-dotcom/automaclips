"""Autenticação simples por token compartilhado.

REST usa o cabeçalho X-Jarvis-Token. Mas EventSource e WebSocket do navegador não
deixam mandar cabeçalho, então /events e /ws/chat também aceitam ?token=.
O token fica em log de proxy nesse caso; na Tailscale isso é aceitável, e está
registrado em docs/decisoes.md.
"""
import hmac

from fastapi import HTTPException, Request

CABECALHO = "X-Jarvis-Token"


def token_confere(recebido, esperado: str) -> bool:
    if not recebido:
        return False
    return hmac.compare_digest(recebido.encode("utf-8"), esperado.encode("utf-8"))


def exigir_token(request: Request) -> None:
    """Dependência FastAPI para rotas REST: só aceita o cabeçalho."""
    esperado = request.app.state.servicos.cfg.token
    if not token_confere(request.headers.get(CABECALHO), esperado):
        raise HTTPException(status_code=401, detail="token inválido ou ausente")


def token_do_stream_confere(cabecalho, query, esperado: str) -> bool:
    """Para SSE e WebSocket: cabeçalho ou ?token=."""
    return token_confere(cabecalho, esperado) or token_confere(query, esperado)


def exigir_token_em_stream(request: Request) -> None:
    esperado = request.app.state.servicos.cfg.token
    if not token_do_stream_confere(
        request.headers.get(CABECALHO), request.query_params.get("token"), esperado
    ):
        raise HTTPException(status_code=401, detail="token inválido ou ausente")
