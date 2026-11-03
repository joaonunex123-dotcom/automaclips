"""Chat pelo WebSocket, com o provedor mock e com provedores de teste."""
import asyncio

from backend.llm import ErroLLM
from backend.main import criar_app
from fastapi.testclient import TestClient

from tests.constantes import CABECALHOS, TOKEN


def _rodada(ws, texto):
    """Manda uma pergunta e lê até o final ou o erro. Devolve todas as mensagens."""
    ws.send_json({"text": texto})
    recebidas = []
    while True:
        mensagem = ws.receive_json()
        recebidas.append(mensagem)
        if mensagem["tipo"] in ("final", "erro"):
            return recebidas


def test_resposta_em_tokens_e_mensagem_final_com_metricas(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        mensagens = _rodada(ws, "quanto é dois mais dois")

    tokens = [m for m in mensagens if m["tipo"] == "token"]
    final = mensagens[-1]
    assert len(tokens) > 1
    assert final["tipo"] == "final"
    assert final["texto"] == "".join(t["texto"] for t in tokens)
    assert "quanto é dois mais dois" in final["texto"]
    assert final["provedor"] == "mock"
    assert final["tokens"] == len(tokens)
    assert final["tempo_total_s"] >= 0
    assert final["tempo_ate_primeiro_token_s"] >= 0
    assert isinstance(final["tokens_por_s"], float) and final["tokens_por_s"] > 0


def test_conversa_e_salva_no_historico(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        final = _rodada(ws, "primeira pergunta")[-1]

    historico = client.get("/chat/historico", headers=CABECALHOS).json()
    assert [m["papel"] for m in historico] == ["user", "assistant"]
    assert historico[0]["texto"] == "primeira pergunta"
    assert historico[1]["id"] == final["mensagem_id"]
    assert historico[1]["provedor"] == "mock"
    assert historico[1]["tokens"] == final["tokens"]


def test_historico_vai_como_contexto_para_o_provedor(cfg):
    gravados = []

    class ProvedorGravador:
        nome = "gravador"

        async def gerar(self, mensagens):
            gravados.append([m["role"] for m in mensagens])
            yield "ok"

    app = criar_app(cfg, provedor=ProvedorGravador())
    with TestClient(app) as client, client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        _rodada(ws, "um")
        _rodada(ws, "dois")

    assert gravados[0] == ["system", "user"]
    assert gravados[1] == ["system", "user", "assistant", "user"]


def test_mensagem_sem_text_e_json_invalido_viram_erro_sem_derrubar_a_conexao(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"outro": "campo"})
        assert ws.receive_json()["tipo"] == "erro"

        ws.send_json({"text": "   "})
        assert ws.receive_json()["tipo"] == "erro"

        ws.send_text("isto não é json")
        assert ws.receive_json()["tipo"] == "erro"

        # A conexão continua viva e responde normalmente.
        assert _rodada(ws, "ainda aqui")[-1]["tipo"] == "final"


def test_mensagem_longa_demais_e_recusada(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        ws.send_json({"text": "x" * 4001})
        mensagem = ws.receive_json()
    assert mensagem["tipo"] == "erro"
    assert "longa" in mensagem["erro"]


def test_falha_do_provedor_vira_erro_e_mascote_fica_em_erro(cfg):
    class ProvedorQuebrado:
        nome = "quebrado"

        async def gerar(self, mensagens):
            raise ErroLLM("modelo fora do ar")
            yield  # pragma: no cover - torna a função um gerador

    app = criar_app(cfg, provedor=ProvedorQuebrado())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
            mensagens = _rodada(ws, "oi")
        assert mensagens[-1] == {"tipo": "erro", "erro": "modelo fora do ar"}
        assert client.get("/status", headers=CABECALHOS).json()["mascote"] == "erro"


def test_erro_inesperado_nao_vaza_detalhe_para_o_cliente(cfg):
    class ProvedorBugado:
        nome = "bugado"

        async def gerar(self, mensagens):
            raise RuntimeError("segredo interno: /etc/algo")
            yield  # pragma: no cover

    app = criar_app(cfg, provedor=ProvedorBugado())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
            erro = _rodada(ws, "oi")[-1]
    assert erro["tipo"] == "erro"
    assert "segredo" not in erro["erro"]


def test_mascote_volta_a_descansar_depois_da_resposta(client):
    with client.websocket_connect(f"/ws/chat?token={TOKEN}") as ws:
        _rodada(ws, "oi")
    status = client.get("/status", headers=CABECALHOS).json()
    assert status["mascote"] == "descansando"
    assert status["tarefa_atual"] is None


def test_estados_do_mascote_passam_pelo_barramento(app):
    """Ouvindo -> pensando -> falando -> descansando, na ordem certa."""
    from backend.chat import responder
    from backend.llm import ProvedorMock

    servicos = app.state.servicos

    async def cenario():
        fila = servicos.barramento.assinar()
        saida = []

        async def enviar(mensagem):
            saida.append(mensagem)

        await responder(
            "oi", enviar, banco=servicos.banco, provedor=ProvedorMock(0),
            estado=servicos.estado, barramento=servicos.barramento, lock=servicos.lock,
            prompt_sistema="sys", historico=10,
        )
        estados = []
        while not fila.empty():
            evento = fila.get_nowait()
            if evento["tipo"] == "status":
                estados.append(evento["dados"]["mascote"])
        return estados

    estados = asyncio.run(cenario())
    assert estados == ["ouvindo", "pensando", "falando", "descansando"]


def test_cliente_que_cai_no_meio_nao_salva_resposta_parcial(app):
    from backend.chat import responder
    from backend.llm import ProvedorMock

    servicos = app.state.servicos

    async def cenario():
        contador = {"n": 0}

        async def enviar(mensagem):
            contador["n"] += 1
            if contador["n"] == 2:
                raise ConnectionError("cliente sumiu")

        return await responder(
            "uma pergunta longa", enviar, banco=servicos.banco,
            provedor=ProvedorMock(0), estado=servicos.estado,
            barramento=servicos.barramento, lock=servicos.lock,
            prompt_sistema="sys", historico=10,
        )

    assert asyncio.run(cenario()) is None
    papeis = [m["papel"] for m in servicos.banco.ultimas_mensagens(10)]
    assert papeis == ["user"]
