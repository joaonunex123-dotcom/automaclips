from tests.constantes import CABECALHOS


def test_crud_completo_de_nota(client):
    criada = client.post("/notes", json={"titulo": "Compras", "texto": "pão"},
                         headers=CABECALHOS)
    assert criada.status_code == 201
    nota = criada.json()
    assert nota["titulo"] == "Compras"
    assert nota["texto"] == "pão"
    assert nota["criada_em"] == nota["atualizada_em"]
    id_ = nota["id"]

    assert client.get(f"/notes/{id_}", headers=CABECALHOS).json()["titulo"] == "Compras"
    assert [n["id"] for n in client.get("/notes", headers=CABECALHOS).json()] == [id_]

    atualizada = client.patch(f"/notes/{id_}", json={"texto": "pão e leite"},
                              headers=CABECALHOS)
    assert atualizada.status_code == 200
    assert atualizada.json()["texto"] == "pão e leite"
    assert atualizada.json()["titulo"] == "Compras"  # PATCH não apaga o que não veio

    assert client.delete(f"/notes/{id_}", headers=CABECALHOS).status_code == 204
    assert client.get(f"/notes/{id_}", headers=CABECALHOS).status_code == 404


def test_nota_inexistente_da_404(client):
    assert client.get("/notes/999", headers=CABECALHOS).status_code == 404
    assert client.patch("/notes/999", json={"texto": "x"}, headers=CABECALHOS).status_code == 404
    assert client.delete("/notes/999", headers=CABECALHOS).status_code == 404


def test_titulo_vazio_e_recusado(client):
    assert client.post("/notes", json={"titulo": ""}, headers=CABECALHOS).status_code == 422
    assert client.post("/notes", json={}, headers=CABECALHOS).status_code == 422


def test_patch_com_titulo_null_nao_quebra_o_banco(client):
    id_ = client.post("/notes", json={"titulo": "a"}, headers=CABECALHOS).json()["id"]
    resposta = client.patch(f"/notes/{id_}", json={"titulo": None, "texto": "b"},
                            headers=CABECALHOS)
    assert resposta.status_code == 200
    assert resposta.json()["titulo"] == "a"
    assert resposta.json()["texto"] == "b"


def test_listagem_mais_recente_primeiro(client):
    primeira = client.post("/notes", json={"titulo": "1"}, headers=CABECALHOS).json()["id"]
    segunda = client.post("/notes", json={"titulo": "2"}, headers=CABECALHOS).json()["id"]
    ids = [n["id"] for n in client.get("/notes", headers=CABECALHOS).json()]
    assert ids == [segunda, primeira]
