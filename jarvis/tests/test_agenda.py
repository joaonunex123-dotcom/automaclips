from tests.constantes import CABECALHOS


def test_crud_completo_de_compromisso(client):
    criado = client.post("/agenda", headers=CABECALHOS, json={
        "titulo": "Dentista", "quando": "2031-03-10T14:30:00-03:00", "descricao": "limpeza",
    })
    assert criado.status_code == 201
    item = criado.json()
    assert item["quando"] == "2031-03-10T14:30:00-03:00"
    id_ = item["id"]

    assert client.get(f"/agenda/{id_}", headers=CABECALHOS).json()["titulo"] == "Dentista"
    assert len(client.get("/agenda", headers=CABECALHOS).json()) == 1

    mudou = client.patch(f"/agenda/{id_}", headers=CABECALHOS,
                         json={"quando": "2031-03-10T15:00:00-03:00"})
    assert mudou.status_code == 200
    assert mudou.json()["quando"] == "2031-03-10T15:00:00-03:00"
    assert mudou.json()["descricao"] == "limpeza"

    assert client.delete(f"/agenda/{id_}", headers=CABECALHOS).status_code == 204
    assert client.get(f"/agenda/{id_}", headers=CABECALHOS).status_code == 404


def test_horario_sem_fuso_usa_o_fuso_da_casa(client):
    # America/Sao_Paulo é UTC-3 sem horário de verão: 10:00 local vira 13:00 UTC.
    item = client.post("/agenda", headers=CABECALHOS, json={
        "titulo": "Reunião", "quando": "2031-06-01T10:00:00",
    }).json()
    assert item["quando"] == "2031-06-01T10:00:00-03:00"


def test_armazenado_em_utc(client, app):
    id_ = client.post("/agenda", headers=CABECALHOS, json={
        "titulo": "Reunião", "quando": "2031-06-01T10:00:00",
    }).json()["id"]
    bruto = app.state.servicos.banco.obter("agenda", id_)
    assert bruto["quando"] == "2031-06-01T13:00:00+00:00"


def test_titulo_e_data_obrigatorios(client):
    assert client.post("/agenda", headers=CABECALHOS,
                       json={"quando": "2031-01-01T10:00:00"}).status_code == 422
    assert client.post("/agenda", headers=CABECALHOS,
                       json={"titulo": "x", "quando": "amanhã"}).status_code == 422


def test_agenda_inexistente_da_404(client):
    assert client.get("/agenda/9", headers=CABECALHOS).status_code == 404
    assert client.patch("/agenda/9", headers=CABECALHOS, json={"titulo": "x"}).status_code == 404
    assert client.delete("/agenda/9", headers=CABECALHOS).status_code == 404


def test_listagem_em_ordem_de_horario(client):
    client.post("/agenda", headers=CABECALHOS, json={"titulo": "tarde", "quando": "2031-01-02T18:00:00"})
    client.post("/agenda", headers=CABECALHOS, json={"titulo": "cedo", "quando": "2031-01-02T08:00:00"})
    titulos = [i["titulo"] for i in client.get("/agenda", headers=CABECALHOS).json()]
    assert titulos == ["cedo", "tarde"]
