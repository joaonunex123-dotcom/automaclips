"""Barramento, formato SSE e o endpoint /events (com servidor real, porque o
TestClient espera a resposta inteira e um stream infinito travaria)."""
import asyncio
import json
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from backend.estado import Estado
from backend.eventos import Barramento, formatar_sse, gerar_sse
from backend.main import criar_app

from tests.constantes import CABECALHOS, TOKEN


def test_publicar_chega_a_todos_os_assinantes_e_cancelar_para_de_receber():
    async def cenario():
        b = Barramento()
        f1, f2 = b.assinar(), b.assinar()
        b.publicar("nota_criada", {"id": 1})
        assert (await f1.get())["dados"] == {"id": 1}
        assert (await f2.get())["tipo"] == "nota_criada"
        b.cancelar(f2)
        b.publicar("nota_removida", {"id": 1})
        assert f1.qsize() == 1
        assert f2.qsize() == 0
        assert b.assinantes == 1

    asyncio.run(cenario())


def test_fila_cheia_descarta_sem_travar_o_publicador():
    async def cenario():
        b = Barramento()
        b.assinar()  # fila que ninguém consome
        for i in range(1000):
            b.publicar("token", {"texto": str(i)})  # não pode levantar nem bloquear

    asyncio.run(cenario())


def test_formatar_sse_e_json_valido_em_uma_linha():
    texto = formatar_sse({"tipo": "token", "dados": {"texto": "linha\nquebrada"}, "quando": "x"})
    assert texto.startswith("event: token\ndata: ")
    assert texto.endswith("\n\n")
    linha_data = [l for l in texto.split("\n") if l.startswith("data: ")][0]
    assert json.loads(linha_data[len("data: "):])["dados"]["texto"] == "linha\nquebrada"


def test_gerar_sse_manda_snapshot_depois_eventos_e_ping():
    async def cenario():
        b = Barramento()
        estado = Estado(b)
        saida = []
        gerador = gerar_sse(b, estado, desconectou=_nunca, intervalo_ping=0.01)
        saida.append(await gerador.__anext__())
        assert "event: status" in saida[0]
        assert "descansando" in saida[0]

        saida.append(await gerador.__anext__())  # sem evento, vem o ping
        assert saida[1] == ": ping\n\n"

        b.publicar("nota_criada", {"id": 7})
        proximo = await gerador.__anext__()
        assert proximo.startswith("event: nota_criada")

        await gerador.aclose()
        assert b.assinantes == 0  # fechar o stream solta a fila

    asyncio.run(cenario())


def test_gerar_sse_para_quando_o_cliente_cai():
    async def cenario():
        b = Barramento()
        saiu = {"sim": False}

        async def desconectou():
            return saiu["sim"]

        gerador = gerar_sse(b, Estado(b), desconectou=desconectou, intervalo_ping=0.01)
        await gerador.__anext__()
        saiu["sim"] = True
        restante = [x async for x in gerador]
        assert restante == []
        assert b.assinantes == 0

    asyncio.run(cenario())


async def _nunca():
    return False


def test_mudanca_de_status_so_publica_quando_muda():
    async def cenario():
        b = Barramento()
        estado = Estado(b)
        fila = b.assinar()
        estado.atualizar(mascote="ouvindo")
        estado.atualizar(mascote="ouvindo")  # igual: nada a publicar
        estado.atualizar(tarefa="x")
        assert fila.qsize() == 2
        evento = await fila.get()
        assert evento["tipo"] == "status"
        assert evento["dados"] == {"mascote": "ouvindo", "tarefa_atual": None}

    asyncio.run(cenario())


@pytest.fixture
def servidor_real(cfg):
    """Sobe o app num uvicorn de verdade, numa thread, em porta livre."""
    app = criar_app(cfg)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        porta = s.getsockname()[1]
    servidor = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=porta,
                                             log_level="warning"))
    thread = threading.Thread(target=servidor.run, daemon=True)
    thread.start()
    limite = time.monotonic() + 10
    while not servidor.started and time.monotonic() < limite:
        time.sleep(0.05)
    assert servidor.started, "uvicorn não subiu"
    yield f"http://127.0.0.1:{porta}"
    servidor.should_exit = True
    thread.join(10)


def _ler_ate(linhas, evento_esperado):
    """Lê o stream até o evento pedido. Devolve a linha `data:` dele, já sem o prefixo."""
    tipo_atual = None
    for linha in linhas:
        if linha.startswith("event: "):
            tipo_atual = linha[len("event: "):]
        elif linha.startswith("data: ") and tipo_atual == evento_esperado:
            return json.loads(linha[len("data: "):])
    raise AssertionError(f"evento {evento_esperado!r} não chegou")


def test_events_real_entrega_nota_criada_ao_vivo(servidor_real):
    with httpx.stream("GET", f"{servidor_real}/events", params={"token": TOKEN},
                      timeout=10) as resposta:
        assert resposta.status_code == 200
        assert resposta.headers["content-type"].startswith("text/event-stream")
        linhas = resposta.iter_lines()
        _ler_ate(linhas, "status")  # snapshot inicial

        criada = httpx.post(f"{servidor_real}/notes", headers=CABECALHOS,
                            json={"titulo": "ao vivo", "texto": "oi"}, timeout=10)
        assert criada.status_code == 201

        evento = _ler_ate(linhas, "nota_criada")
        assert evento["dados"]["titulo"] == "ao vivo"


def test_events_real_entrega_tecla(servidor_real):
    with httpx.stream("GET", f"{servidor_real}/events", params={"token": TOKEN},
                      timeout=10) as resposta:
        linhas = resposta.iter_lines()
        _ler_ate(linhas, "status")
        httpx.post(f"{servidor_real}/input/key", headers=CABECALHOS,
                   json={"key": 3, "action": "up"}, timeout=10)
        evento = _ler_ate(linhas, "tecla")
        assert evento["dados"] == {"tecla": 3, "acao": "up"}
