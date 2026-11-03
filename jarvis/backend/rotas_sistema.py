"""Rotas de sistema e tempo real: /status, /events, /input/key, /chat e /ws/chat."""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import chat, sistema, tempo
from .entrada import registrar_tecla
from .eventos import gerar_sse
from .seguranca import CABECALHO, exigir_token, exigir_token_em_stream, token_do_stream_confere
from .servicos import Servicos

router = APIRouter()

PROXIMAS_NO_STATUS = 5


def _servicos(request: Request) -> Servicos:
    return request.app.state.servicos


@router.get("/status", dependencies=[Depends(exigir_token)])
async def status(request: Request):
    s = _servicos(request)
    agora = tempo.agora_armazenado()
    proximas = [
        {"tipo": "agenda", "id": item["id"], "titulo": item["titulo"],
         "quando": tempo.para_api(item["quando"], s.cfg.zona)}
        for item in s.banco.proximos_agenda(agora, PROXIMAS_NO_STATUS)
    ] + [
        {"tipo": "lembrete", "id": item["id"], "titulo": item["texto"],
         "quando": tempo.para_api(item["quando"], s.cfg.zona)}
        for item in s.banco.proximos_lembretes(agora, PROXIMAS_NO_STATUS)
    ]
    proximas.sort(key=lambda item: item["quando"])
    return {
        "temperatura_c": sistema.ler_temperatura(s.cfg.thermal_dir),
        "cpu_percent": sistema.cpu_percent(),
        "ram": sistema.ram(),
        "uptime_s": sistema.uptime_s(),
        "mascote": s.estado.mascote,
        "tarefa_atual": s.estado.tarefa_atual,
        "proximas": proximas[:PROXIMAS_NO_STATUS],
    }


@router.get("/events", dependencies=[Depends(exigir_token_em_stream)])
async def eventos(request: Request):
    s = _servicos(request)
    return StreamingResponse(
        gerar_sse(s.barramento, s.estado, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


class TeclaEntrada(BaseModel):
    key: Literal[1, 2, 3]
    action: Literal["down", "up"]


@router.post("/input/key", status_code=202, dependencies=[Depends(exigir_token)])
async def tecla(corpo: TeclaEntrada, request: Request):
    registrar_tecla(_servicos(request).barramento, corpo.key, corpo.action)
    return {"ok": True}


@router.get("/chat/historico", dependencies=[Depends(exigir_token)])
async def historico(request: Request, limite: int = Query(50, ge=1, le=500)):
    return _servicos(request).banco.ultimas_mensagens(limite)


async def ws_chat(ws: WebSocket):
    """Handler do /ws/chat. Registrado em main.py, que tem o app na mão.

    Protocolo: o cliente manda {"text": "..."}. O servidor responde com
    {"tipo": "token", "texto"}, depois um {"tipo": "final", ...} com métricas,
    ou {"tipo": "erro", "erro"}.
    """
    s = ws.app.state.servicos
    recebido = ws.headers.get(CABECALHO)
    if not token_do_stream_confere(recebido, ws.query_params.get("token"), s.cfg.token):
        await ws.close(code=4401)
        return
    await ws.accept()

    async def enviar(mensagem):
        await ws.send_json(mensagem)

    try:
        while True:
            try:
                dados = await ws.receive_json()
            except ValueError:
                await enviar({"tipo": "erro", "erro": "JSON inválido"})
                continue
            texto = dados.get("text") if isinstance(dados, dict) else None
            if not isinstance(texto, str) or not texto.strip():
                await enviar({"tipo": "erro", "erro": "campo 'text' obrigatório"})
                continue
            if len(texto) > chat.TAMANHO_MAX_TEXTO:
                await enviar({"tipo": "erro", "erro": "mensagem longa demais"})
                continue
            await chat.responder(
                texto.strip(), enviar,
                banco=s.banco, provedor=s.provedor, estado=s.estado,
                barramento=s.barramento, lock=s.lock,
                prompt_sistema=s.cfg.system_prompt,
                historico=s.cfg.historico_msgs,
            )
    except WebSocketDisconnect:
        return
