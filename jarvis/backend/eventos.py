"""Barramento de eventos: cada cliente SSE tem a sua fila.

Tudo roda no mesmo event loop (FastAPI, WebSocket, APScheduler e SSE), então não
há lock. Publicar só enfileira, e precisa ser chamado do loop, não de threads.

Fila cheia (cliente lento ou travado) = evento descartado para aquele cliente.
Preferimos perder um token na tela a deixar a memória do Pi crescer sem limite.
"""
import asyncio
import json

from .tempo import agora_armazenado

TAMANHO_FILA = 200


class Barramento:
    def __init__(self):
        self._assinantes = set()

    @property
    def assinantes(self) -> int:
        return len(self._assinantes)

    def assinar(self) -> asyncio.Queue:
        fila = asyncio.Queue(maxsize=TAMANHO_FILA)
        self._assinantes.add(fila)
        return fila

    def cancelar(self, fila) -> None:
        self._assinantes.discard(fila)

    def publicar(self, tipo: str, dados=None) -> None:
        evento = {"tipo": tipo, "dados": dados if dados is not None else {},
                  "quando": agora_armazenado()}
        for fila in list(self._assinantes):
            try:
                fila.put_nowait(evento)
            except asyncio.QueueFull:
                pass


def formatar_sse(evento: dict) -> str:
    """Um evento no formato do Server-Sent Events."""
    dados = json.dumps(evento, ensure_ascii=False)
    return f"event: {evento['tipo']}\ndata: {dados}\n\n"


async def gerar_sse(barramento, estado, desconectou, intervalo_ping=15.0):
    """Gera o corpo do /events: snapshot do status, depois cada evento.

    `desconectou` é uma corrotina sem argumentos que diz se o cliente foi embora.
    É checada a cada evento ou ping, então a queda é notada em até
    `intervalo_ping` segundos. Quando o gerador fecha, a fila sai do barramento.
    """
    fila = barramento.assinar()
    try:
        yield formatar_sse({"tipo": "status", "dados": estado.snapshot(),
                            "quando": agora_armazenado()})
        while True:
            if await desconectou():
                break
            try:
                evento = await asyncio.wait_for(fila.get(), timeout=intervalo_ping)
            except asyncio.TimeoutError:
                # Comentário SSE: mantém a conexão viva pelo Tailscale/proxies.
                yield ": ping\n\n"
                continue
            yield formatar_sse(evento)
    finally:
        barramento.cancelar(fila)
