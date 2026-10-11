"""Capa do clip: pontuação do quadro, texto, template, render e upload opcional."""
import copy

import numpy as np
import pytest

import settings
from db import repositorio
from editing import capa, editar, render, template as template_mod
from publish import publicar
from publish import youtube as yt


def _quadro(valor=128, ruido=0.0, t=0.0, seed=0):
    rng = np.random.default_rng(seed)
    base = np.full((216, 384, 3), valor, dtype="float32")
    if ruido:
        base += rng.normal(0, ruido, base.shape)
    return {"t": t, "bgr": np.clip(base, 0, 255).astype("uint8")}


def _xadrez(t=0.0, borrado=False):
    quadro = np.zeros((216, 384, 3), dtype="uint8")
    quadro[::8, :] = 200
    quadro[:, ::8] = 200
    quadro[:] = np.maximum(quadro, 60)
    if borrado:
        import cv2
        quadro = cv2.GaussianBlur(quadro, (0, 0), 6)
    return {"t": t, "bgr": quadro}


# --- pontuação ----------------------------------------------------------------

def test_quadro_nitido_vence_o_borrado():
    pont = capa.pontuar_quadros([_xadrez(0.0, borrado=True), _xadrez(1.0)])
    assert capa.escolher_melhor(pont)["t"] == 1.0


def test_quadro_escuro_perde_mesmo_sendo_nitido():
    escuro = _xadrez(0.0)
    escuro["bgr"] = (escuro["bgr"] * 0.12).astype("uint8")
    pont = capa.pontuar_quadros([escuro, _xadrez(1.0, borrado=True)])
    assert capa.escolher_melhor(pont)["t"] == 1.0


def test_quadro_de_corte_e_penalizado():
    # t=1 é o primeiro quadro da cena nova: muito diferente do vizinho de trás.
    pont = capa.pontuar_quadros([_quadro(10, t=0.0), _quadro(240, t=1.0),
                                 _quadro(240, t=2.0)])
    assert pont[0]["corte"] and pont[1]["corte"]
    assert not pont[2]["corte"]


def test_rosto_aumenta_a_nota():
    a, b = _xadrez(0.0), _xadrez(1.0)

    def detector(frame):
        return [(100, 40, 80, 100)] if frame is b["bgr"] else []

    pont = capa.pontuar_quadros([a, b], detectar_rosto=detector)
    assert pont[1]["nota"] > pont[0]["nota"]
    assert pont[1]["centro_rosto"] == pytest.approx(140 / 384)


def test_empate_fica_com_o_mais_cedo():
    pont = capa.pontuar_quadros([_xadrez(3.0), _xadrez(1.0)])
    assert capa.escolher_melhor(pont)["t"] == 1.0


def test_sem_quadros_nao_levanta():
    assert capa.pontuar_quadros([]) == []
    assert capa.escolher_melhor([]) is None


def test_melhor_quadro_degrada_para_o_meio_quando_amostragem_falha():
    def executar(*a, **k):
        raise OSError("sem ffmpeg")
    t, info = capa.melhor_quadro("x.mp4", 10.0, 30.0, executar=executar)
    assert t == 20.0 and info["motivo"] == "sem_amostra"


# --- texto --------------------------------------------------------------------

def test_titulo_curto_ganha_fonte_maior_que_o_longo():
    _, curto = capa.quebrar_titulo("Ele mentiu", 1080)
    _, longo = capa.quebrar_titulo(
        "Ela descobriu o segredo do trabalho de português", 1080)
    assert curto > longo


def test_titulo_respeita_o_maximo_de_linhas():
    texto, _ = capa.quebrar_titulo("a " * 60, 1080, maximo_linhas=4)
    assert texto.count("\\N") + 1 <= 4


def test_nenhuma_linha_estoura_a_largura():
    texto, tamanho = capa.quebrar_titulo("Inacreditável o que aconteceu", 1080)
    maior = max(len(l) for l in texto.split("\\N"))
    assert maior * 0.62 * tamanho <= 1080


def test_titulo_do_clip_prefere_hook_e_corta_palavras():
    linha = {"hook_text": "um dois tres quatro cinco seis sete oito nove",
             "motivo": "m", "titulo": "t"}
    assert capa.titulo_do_clip(linha, limite_palavras=3) == "um dois tres"
    assert capa.titulo_do_clip({"hook_text": "", "motivo": "", "titulo": "Vídeo"}) == "Vídeo"
    assert capa.titulo_do_clip({}) == ""


def test_ass_neutraliza_chaves_e_barra():
    doc = capa.ass_do_titulo("a{\\b1}b\\Nc", 1080, 1920, {})
    texto = doc.split("Dialogue:")[1]
    assert "{\\b1}" not in texto.split("}", 1)[1]
    assert "PlayResX: 1080" in doc and "PlayResY: 1920" in doc


# --- template -----------------------------------------------------------------

def test_template_traz_capa_desligada(template):
    assert template["capa"]["ativa"] is False
    assert template["capa"]["frame_inicial"]["ativo"] is False
    assert template["capa"]["frame_inicial"]["duracao_s"] == pytest.approx(0.3)


@pytest.mark.parametrize("campo, valor", [
    (("ativa",), "sim"),
    (("tamanho_max",), 5),
    (("frame_inicial", "ativo"), 1),
    (("frame_inicial", "duracao_s"), 3.0),
    (("caixa_cor",), "preto"),
])
def test_template_rejeita_capa_invalida(template, campo, valor):
    config = copy.deepcopy(template)
    alvo = config["capa"]
    for chave in campo[:-1]:
        alvo = alvo[chave]
    alvo[campo[-1]] = valor
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(config)


# --- render -------------------------------------------------------------------

def test_sem_quadro_de_titulo_o_grafo_e_o_de_antes(template):
    a = render.filtergraph(template, "x.ass", 30.0)
    b = render.filtergraph(template, "x.ass", 30.0, efeitos={"picos": []})
    assert a == b and "overlay=0:0" not in a


def test_quadro_de_titulo_entra_como_ultima_entrada_depois_do_filler(template):
    efeitos = {"capa_frame": {"caminho": "/c/capa.png", "duracao_s": 0.3}}
    grafo = render.filtergraph(
        template, "x.ass", 30.0, sons=[{"caminho": "a.wav", "instante_s": 1,
                                        "volume": 1}],
        filler={"caminho": "f.mp4", "inicio_s": 0, "loop": False},
        efeitos=efeitos)
    # 0 = clip, 1 = som, 2 = filler, 3 = capa
    assert "[vass][3:v]overlay=0:0:eof_action=pass[vout]" in grafo
    # a legenda fica sob o quadro de título
    assert grafo.index("ass=x.ass[vass]") < grafo.index("[vass][3:v]")


def test_comando_inclui_a_imagem_em_loop_com_duracao(template):
    efeitos = {"capa_frame": {"caminho": "/c/capa.png", "duracao_s": 0.3}}
    cmd = render.comando(template, "v.mp4", 0, 30, "x.ass", "o.mp4",
                         efeitos=efeitos)
    i = cmd.index("/c/capa.png")
    assert cmd[i - 1] == "-i"
    assert cmd[cmd.index("-loop") + 1] == "1"
    assert cmd[i - 2] == "0.300"      # -t imediatamente antes do -i


# --- integração com editar ----------------------------------------------------

@pytest.fixture
def clip(conn, video, tmp_path):
    fonte = tmp_path / "vid1.mp4"
    fonte.write_text("v", encoding="utf-8")
    fila_id = repositorio.registrar_observacao(
        conn, video(video_id="vid1"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO)
    repositorio.registrar_midia(conn, fila_id, video_path=str(fonte),
                                audio_path=str(tmp_path / "a.wav"),
                                duracao_real_s=600.0)
    repositorio.registrar_clips(conn, fila_id, [{
        "inicio_s": 100.0, "fim_s": 130.0, "score_claude": 9.0, "motivo": "m",
        "hook_text": "ele nunca contou isso", "picos_energia": 0,
        "score_final": 9.0, "picos_instantes": []}])
    return repositorio.clips_para_renderizar(conn)[0]


def test_capa_desligada_nao_chama_o_gerador(conn, template, clip):
    chamadas = []
    editar.renderizar_clip(
        conn, clip, template, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: "/r/x.mp4",
        gerar_capa=lambda *a: chamadas.append(a))
    assert chamadas == []
    assert repositorio.render(conn, clip["id"])["capa_path"] == ""


def test_capa_ativa_grava_o_caminho_e_nao_mexe_no_render(conn, template, clip):
    config = copy.deepcopy(template)
    config["capa"]["ativa"] = True
    recebido = {}

    def renderizar(*a, **k):
        recebido.update(k)
        return "/r/x.mp4"

    editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=renderizar, destino_dir="/tmp/nao-existe-xyz",
        gerar_capa=lambda cfg, v, i, f, titulo, d, n: {
            "vertical": "/r/x_capa/capa_1080x1920.png"})
    assert repositorio.render(conn, clip["id"])["capa_path"].endswith("1080x1920.png")
    assert "efeitos" not in recebido     # só `ativa`: o vídeo não muda


def test_frame_inicial_passa_o_quadro_ao_render(conn, template, clip):
    config = copy.deepcopy(template)
    config["capa"]["frame_inicial"]["ativo"] = True
    recebido = {}
    editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: recebido.update(k) or "/r/x.mp4",
        destino_dir="/tmp/nao-existe-xyz",
        gerar_capa=lambda *a: {"vertical": "/r/capa.png"})
    assert recebido["efeitos"]["capa_frame"] == {
        "caminho": "/r/capa.png", "duracao_s": 0.3}


def test_falha_da_capa_nao_derruba_o_clip(conn, template, clip):
    config = copy.deepcopy(template)
    config["capa"]["ativa"] = True

    def quebra(*a):
        raise RuntimeError("sem opencv")

    caminho = editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: "/r/x.mp4", destino_dir="/tmp/nao-existe-xyz",
        gerar_capa=quebra)
    assert caminho == "/r/x.mp4"
    assert repositorio.render(conn, clip["id"])["capa_path"] == ""


# --- YouTube thumbnails.set ---------------------------------------------------

class ClienteThumb:
    def __init__(self, erro=None):
        self.erro, self.chamadas = erro, []

    def thumbnails(self):
        return self

    def set(self, videoId=None, media_body=None):
        self.chamadas.append((videoId, media_body))
        return self

    def execute(self):
        if self.erro:
            raise self.erro
        return {}


def test_definir_thumbnail_envia_o_arquivo(tmp_path):
    png = tmp_path / "c.png"
    png.write_bytes(b"png")
    cliente = ClienteThumb()
    assert yt.definir_thumbnail(cliente, "abc", str(png),
                                criar_midia=lambda p: p) is True
    assert cliente.chamadas == [("abc", str(png))]


def test_definir_thumbnail_sem_arquivo_levanta(tmp_path):
    with pytest.raises(yt.ErroYouTubeUpload):
        yt.definir_thumbnail(ClienteThumb(), "abc", str(tmp_path / "nao.png"))


def test_upload_da_capa_vem_desligado(conn, tmp_path, monkeypatch):
    assert settings.YOUTUBE_ENVIAR_CAPA is False
    png = tmp_path / "c.png"
    png.write_bytes(b"x")
    cliente = ClienteThumb()
    assert publicar._enviar_capa(conn, cliente, "vid", {"capa_path": str(png)}) is False
    assert cliente.chamadas == []


def test_upload_da_capa_ligado_cobra_quota_e_perdoa_falha(conn, tmp_path,
                                                          monkeypatch):
    from publish import quota
    monkeypatch.setattr(settings, "YOUTUBE_ENVIAR_CAPA", True)
    png = tmp_path / "c.png"
    png.write_bytes(b"x")
    monkeypatch.setattr(yt, "definir_thumbnail", lambda *a, **k: True)
    assert publicar._enviar_capa(conn, object(), "vid", {"capa_path": str(png)})
    assert quota.usada(conn) == settings.YOUTUBE_CUSTO_THUMBNAIL

    def recusa(*a, **k):
        raise yt.ErroYouTubeUpload("403")
    monkeypatch.setattr(yt, "definir_thumbnail", recusa)
    assert publicar._enviar_capa(conn, object(), "vid", {"capa_path": str(png)}) is False
    assert quota.usada(conn) == settings.YOUTUBE_CUSTO_THUMBNAIL


def test_coluna_capa_path_existe_e_migra_banco_antigo(conn):
    colunas = {r["name"] for r in conn.execute("PRAGMA table_info(renders)")}
    assert "capa_path" in colunas
