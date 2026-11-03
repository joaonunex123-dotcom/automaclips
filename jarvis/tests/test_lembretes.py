import asyncio
import time
from datetime import datetime, timedelta, timezone

from tests.constantes import CABECALHOS


def _futuro(segundos=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=segundos)).isoformat(timespec="seconds")


def test_crud_e_agendamento(client, app):
    servicos = app.state.servicos
    criado = client.post("/reminders", headers=CABECALHOS,
                         json={"texto": "tomar remédio", "quando": _futuro()})
    assert criado.status_code == 201
    lembrete = criado.json()
    id_ = lembrete["id"]
    assert lembrete["disparado_em"] is None
    assert f"lembrete-{id_}" in servicos.agendador.agendados()

    lido = client.get(f"/reminders/{id_}", headers=CABECALHOS).json()
    assert lido["texto"] == "tomar remédio"

    novo_horario = (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(timespec="seconds")
    atualizado = client.patch(f"/reminders/{id_}", headers=CABECALHOS, json={"quando": novo_horario})
    assert atualizado.status_code == 200
    assert atualizado.json()["quando"].endswith("-03:00")
    assert servicos.agendador.agendados().count(f"lembrete-{id_}") == 1  # reagendou, não duplicou

    assert client.delete(f"/reminders/{id_}", headers=CABECALHOS).status_code == 204
    assert f"lembrete-{id_}" not in servicos.agendador.agendados()
    assert client.get(f"/reminders/{id_}", headers=CABECALHOS).status_code == 404


def test_quando_no_passado_e_recusado(client):
    passado = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    resposta = client.post("/reminders", headers=CABECALHOS,
                           json={"texto": "tarde demais", "quando": passado})
    assert resposta.status_code == 422


def test_patch_para_passado_e_recusado(client):
    id_ = client.post("/reminders", headers=CABECALHOS,
                      json={"texto": "x", "quando": _futuro()}).json()["id"]
    passado = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    resposta = client.patch(f"/reminders/{id_}", headers=CABECALHOS, json={"quando": passado})
    assert resposta.status_code == 422


def test_horario_sem_fuso_e_lido_no_fuso_da_casa(client):
    # Data bem longe no futuro, sem fuso: tem que ser lida como horário de São Paulo.
    resposta = client.post("/reminders", headers=CABECALHOS,
                           json={"texto": "x", "quando": "2099-01-01T10:00:00"})
    assert resposta.status_code == 201
    assert resposta.json()["quando"] == "2099-01-01T10:00:00-03:00"


def test_lembrete_inexistente_da_404(client):
    assert client.get("/reminders/5", headers=CABECALHOS).status_code == 404
    assert client.patch("/reminders/5", headers=CABECALHOS, json={"texto": "x"}).status_code == 404
    assert client.delete("/reminders/5", headers=CABECALHOS).status_code == 404


def test_disparo_marca_no_banco_e_emite_evento(app):
    """Chama o callback direto: o que importa é o efeito (banco + evento)."""
    servicos = app.state.servicos
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        id_ = client.post("/reminders", headers=CABECALHOS,
                          json={"texto": "beba água", "quando": _futuro()}).json()["id"]

        async def cenario():
            fila = servicos.barramento.assinar()
            await servicos.disparar_lembrete(id_)
            return await asyncio.wait_for(fila.get(), timeout=1)

        evento = asyncio.run(cenario())
        assert evento["tipo"] == "lembrete_disparado"
        assert evento["dados"]["texto"] == "beba água"
        assert evento["dados"]["disparado_em"]

        lido = client.get(f"/reminders/{id_}", headers=CABECALHOS).json()
        assert lido["disparado_em"] is not None


def test_disparo_de_lembrete_apagado_nao_quebra(app):
    servicos = app.state.servicos
    asyncio.run(servicos.disparar_lembrete(12345))  # não existe: só registra e sai


def test_lembrete_dispara_no_horario_real(client):
    """Integração: o APScheduler roda de verdade no loop do app."""
    quando = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat(timespec="seconds")
    id_ = client.post("/reminders", headers=CABECALHOS,
                      json={"texto": "agora", "quando": quando}).json()["id"]
    limite = time.monotonic() + 8
    while time.monotonic() < limite:
        if client.get(f"/reminders/{id_}", headers=CABECALHOS).json()["disparado_em"]:
            break
        time.sleep(0.2)
    assert client.get(f"/reminders/{id_}", headers=CABECALHOS).json()["disparado_em"]


def test_pendentes_sao_reagendados_na_subida(cfg, tmp_path):
    """Um lembrete gravado com o serviço desligado volta a ser agendado."""
    from fastapi.testclient import TestClient

    from backend.main import criar_app

    app1 = criar_app(cfg)
    with TestClient(app1) as client:
        id_ = client.post("/reminders", headers=CABECALHOS,
                          json={"texto": "persistido", "quando": _futuro()}).json()["id"]

    app2 = criar_app(cfg)  # outro processo, mesmo banco
    with TestClient(app2):
        assert f"lembrete-{id_}" in app2.state.servicos.agendador.agendados()
