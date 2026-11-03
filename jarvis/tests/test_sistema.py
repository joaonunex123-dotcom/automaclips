"""Status da máquina e as rotas /status, /input/key e a tela estática."""
from backend.entrada import registrar_tecla
from backend.estado import Estado
from backend.sistema import ler_temperatura
from backend.main import criar_app
from fastapi.testclient import TestClient

from tests.constantes import CABECALHOS


def _zona(raiz, nome, conteudo):
    pasta = raiz / nome
    pasta.mkdir(parents=True, exist_ok=True)
    (pasta / "temp").write_text(conteudo, encoding="ascii")


def test_temperatura_pega_a_maior_zona_em_celsius(tmp_path):
    _zona(tmp_path, "thermal_zone0", "45500\n")
    _zona(tmp_path, "thermal_zone1", "52100\n")
    assert ler_temperatura(str(tmp_path)) == 52.1


def test_temperatura_ignora_leitura_corrompida(tmp_path):
    _zona(tmp_path, "thermal_zone0", "lixo")
    _zona(tmp_path, "thermal_zone1", "40000")
    assert ler_temperatura(str(tmp_path)) == 40.0


def test_temperatura_sem_sensor_e_none(tmp_path):
    assert ler_temperatura(str(tmp_path / "nao_existe")) is None
    assert ler_temperatura(str(tmp_path)) is None


def test_status_completo(client):
    resposta = client.get("/status", headers=CABECALHOS)
    assert resposta.status_code == 200
    corpo = resposta.json()
    assert set(corpo) == {"temperatura_c", "cpu_percent", "ram", "uptime_s",
                          "mascote", "tarefa_atual", "proximas"}
    assert corpo["temperatura_c"] is None  # sem sensor neste teste
    assert isinstance(corpo["cpu_percent"], float)
    assert set(corpo["ram"]) == {"total_mb", "usada_mb", "percent"}
    assert corpo["ram"]["total_mb"] > 0
    assert corpo["uptime_s"] >= 0
    assert corpo["mascote"] == "descansando"
    assert corpo["tarefa_atual"] is None
    assert corpo["proximas"] == []


def test_status_mostra_temperatura_do_sensor(cfg, tmp_path):
    _zona(tmp_path, "thermal_zone0", "61250\n")
    from dataclasses import replace
    app = criar_app(replace(cfg, thermal_dir=str(tmp_path)))
    with TestClient(app) as client:
        assert client.get("/status", headers=CABECALHOS).json()["temperatura_c"] == 61.2


def test_status_proximas_junta_agenda_e_lembretes_em_ordem(client):
    client.post("/agenda", headers=CABECALHOS,
                json={"titulo": "reunião", "quando": "2099-05-01T15:00:00"})
    client.post("/reminders", headers=CABECALHOS,
                json={"texto": "remédio", "quando": "2099-05-01T09:00:00"})
    client.post("/agenda", headers=CABECALHOS,
                json={"titulo": "almoço", "quando": "2099-05-01T12:00:00"})
    proximas = client.get("/status", headers=CABECALHOS).json()["proximas"]
    assert [(p["tipo"], p["titulo"]) for p in proximas] == [
        ("lembrete", "remédio"), ("agenda", "almoço"), ("agenda", "reunião"),
    ]
    assert proximas[0]["quando"] == "2099-05-01T09:00:00-03:00"


def test_status_nao_mostra_lembrete_ja_disparado(client, app):
    import asyncio
    id_ = client.post("/reminders", headers=CABECALHOS,
                      json={"texto": "velho", "quando": "2099-05-01T09:00:00"}).json()["id"]
    asyncio.run(app.state.servicos.disparar_lembrete(id_))
    assert client.get("/status", headers=CABECALHOS).json()["proximas"] == []


def test_input_key_aceita_teclas_validas(client):
    for tecla in (1, 2, 3):
        for acao in ("down", "up"):
            resposta = client.post("/input/key", headers=CABECALHOS,
                                   json={"key": tecla, "action": acao})
            assert resposta.status_code == 202
            assert resposta.json() == {"ok": True}


def test_input_key_recusa_tecla_e_acao_invalidas(client):
    assert client.post("/input/key", headers=CABECALHOS,
                       json={"key": 4, "action": "down"}).status_code == 422
    assert client.post("/input/key", headers=CABECALHOS,
                       json={"key": 1, "action": "press"}).status_code == 422
    assert client.post("/input/key", headers=CABECALHOS,
                       json={"key": "1", "action": "down"}).status_code == 422


def test_registrar_tecla_publica_evento_e_valida():
    import asyncio
    from backend.eventos import Barramento

    async def cenario():
        b = Barramento()
        fila = b.assinar()
        assert registrar_tecla(b, 2, "down") == {"tecla": 2, "acao": "down"}
        assert (await fila.get())["tipo"] == "tecla"
        for tecla, acao in ((0, "down"), (1, "press")):
            try:
                registrar_tecla(b, tecla, acao)
            except ValueError:
                pass
            else:
                raise AssertionError("deveria recusar")

    asyncio.run(cenario())


def test_estado_inicial_e_mascote_invalido_recusado():
    import asyncio
    from backend.eventos import Barramento

    async def cenario():
        estado = Estado(Barramento())
        assert estado.snapshot() == {"mascote": "descansando", "tarefa_atual": None}
        try:
            estado.atualizar(mascote="dormindo")
        except ValueError:
            return
        raise AssertionError("mascote inválido deveria levantar ValueError")

    asyncio.run(cenario())


def test_tela_estatica_e_servida_em_raiz(client):
    resposta = client.get("/")
    assert resposta.status_code == 200
    assert "text/html" in resposta.headers["content-type"]
    assert "JARVIS" in resposta.text


def test_rotas_da_api_nao_sao_engolidas_pela_tela(client):
    assert client.get("/status", headers=CABECALHOS).status_code == 200
    assert client.get("/notes", headers=CABECALHOS).status_code == 200
