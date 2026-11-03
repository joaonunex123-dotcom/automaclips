"""A tela 800×480 é servida pelo próprio backend, em / (pública: não tem dado)."""


def test_tela_servida_em_raiz_sem_token(client):
    resposta = client.get("/")
    assert resposta.status_code == 200
    assert 'id="tela"' in resposta.text
    assert "JARVIS" in resposta.text


def test_arquivos_da_tela_servidos(client):
    for caminho, trecho in [
        ("/config.js", "JARVIS_CONFIG"),
        ("/app.js", "/ws/chat"),
        ("/estilo.css", "width: 800px"),
    ]:
        resposta = client.get(caminho)
        assert resposta.status_code == 200, caminho
        assert trecho in resposta.text, caminho
