"""Autenticação: REST pelo cabeçalho; SSE e WS pelo cabeçalho ou ?token=."""
import pytest
from starlette.websockets import WebSocketDisconnect

from tests.constantes import CABECALHOS, TOKEN

ROTAS_REST = [
    ("GET", "/status"),
    ("GET", "/notes"),
    ("GET", "/agenda"),
    ("GET", "/reminders"),
    ("GET", "/chat/historico"),
]


@pytest.mark.parametrize("metodo, caminho", ROTAS_REST)
def test_rest_sem_token_da_401(client, metodo, caminho):
    assert client.request(metodo, caminho).status_code == 401


@pytest.mark.parametrize("metodo, caminho", ROTAS_REST)
def test_rest_com_token_errado_da_401(client, metodo, caminho):
    resposta = client.request(metodo, caminho, headers={"X-Jarvis-Token": "errado" * 4})
    assert resposta.status_code == 401


def test_post_input_sem_token_da_401(client):
    resposta = client.post("/input/key", json={"key": 1, "action": "down"})
    assert resposta.status_code == 401


def test_rest_aceita_cabecalho_correto(client):
    assert client.get("/notes", headers=CABECALHOS).status_code == 200


def test_rest_nao_aceita_token_na_query(client):
    # Na query só SSE e WS, para o token não ir parar em log de todo endpoint.
    assert client.get(f"/notes?token={TOKEN}").status_code == 401


def test_events_sem_token_da_401(client):
    assert client.get("/events").status_code == 401


def test_events_com_token_errado_da_401(client):
    assert client.get("/events?token=errado").status_code == 401


def test_ws_sem_token_e_fechado(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/chat") as ws:
            ws.receive_json()


def test_ws_com_token_na_query_conecta(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"text": "oi"})
        assert ws.receive_json()["tipo"] == "token"
