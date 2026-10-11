"""Reframe com rosto: planejamento, expressão do ffmpeg e degradação.

O detector real (OpenCV) só é exercitado num vídeo sintético sem rosto; o
resto usa amostras fabricadas. Nada aqui baixa mídia.
"""
import copy
import shutil
import subprocess

import pytest

from editing import render, rosto

DIM = (1920, 1080)


def _config(template, **rosto_cfg):
    config = copy.deepcopy(template)
    config["reframe"]["rosto"].update({"ativo": True, **rosto_cfg})
    return config


def _amostras(centros, passo=0.5):
    return [(i * passo, c) for i, c in enumerate(centros)]


# --- geometria ----------------------------------------------------------------

def test_ancora_centraliza_o_rosto_na_janela():
    # janela 9:16 de 1080 de altura = 607,5 px; rosto no meio -> âncora 0,5
    assert rosto.ancora_para_centro(0.5, 1920, 1080, 9 / 16) == pytest.approx(0.5)


def test_ancora_nas_bordas_e_grampeada():
    assert rosto.ancora_para_centro(0.01, 1920, 1080, 9 / 16) == 0.0
    assert rosto.ancora_para_centro(0.99, 1920, 1080, 9 / 16) == 1.0


def test_janela_cheia_nao_tem_para_onde_andar():
    # vídeo já vertical: crop == original
    assert rosto.ancora_para_centro(0.9, 1080, 1920, 9 / 16) == 0.5


# --- planejar -----------------------------------------------------------------

def test_rosto_parado_gera_uma_ancora_so(template):
    trilha = rosto.planejar(_amostras([0.3] * 10), DIM, _config(template))
    assert len(trilha) == 1 and trilha[0][0] == 0.0


def test_tremida_dentro_da_zona_morta_nao_move(template):
    centros = [0.50, 0.51, 0.49, 0.50, 0.52, 0.50, 0.49, 0.51]
    trilha = rosto.planejar(_amostras(centros), DIM, _config(template))
    assert len(trilha) == 1


def test_troca_de_lugar_gera_um_movimento(template):
    centros = [0.3] * 6 + [0.7] * 6
    trilha = rosto.planejar(_amostras(centros), DIM, _config(template))
    assert len(trilha) == 2
    assert trilha[1][1] > trilha[0][1]


def test_detecao_espuria_isolada_e_ignorada(template):
    centros = [0.5] * 4 + [0.95] + [0.5] * 4
    trilha = rosto.planejar(_amostras(centros), DIM, _config(template))
    assert len(trilha) == 1


def test_buraco_herda_a_ultima_posicao(template):
    centros = [0.3, 0.3, 0.3, None, None, None, 0.3, 0.3, 0.3, 0.3]
    trilha = rosto.planejar(_amostras(centros), DIM, _config(template))
    assert len(trilha) == 1


def test_sem_rosto_suficiente_devolve_none(template):
    centros = [0.5] + [None] * 9          # 10% < minimo_deteccoes (30%)
    assert rosto.planejar(_amostras(centros), DIM, _config(template)) is None
    assert rosto.planejar([], DIM, _config(template)) is None


def test_max_movimentos_limita_a_trilha(template):
    centros = ([0.2] * 3 + [0.8] * 3) * 6
    config = _config(template, max_movimentos=3)
    trilha = rosto.planejar(_amostras(centros), DIM, config)
    assert len(trilha) <= 4
    tempos = [t for t, _ in trilha]
    assert tempos == sorted(tempos)


# --- expressão e filtergraph --------------------------------------------------

def test_expressao_soma_rampas():
    expr = rosto.expressao_ancora([(0.0, 0.2), (3.0, 0.8)], 0.5)
    assert expr.startswith("0.2000+")
    assert "clip((t-3.000)/0.500,0,1)" in expr
    assert "(+0.6000)" in expr


def test_filtro_usa_a_trilha_quando_existe(template):
    config = copy.deepcopy(template)
    config["reframe"]["trilha"] = [(0.0, 0.2), (2.0, 0.7)]
    grafo = render.filtro_reframe(config)
    assert "clip((t-2.000)" in grafo
    assert "x='(iw-ow)*(0.2000+" in grafo


def test_filtro_sem_trilha_continua_igual(template):
    assert "(iw-ow)*0.5" in render.filtro_reframe(template)


# --- aplicar ------------------------------------------------------------------

def test_desligado_devolve_o_mesmo_config_sem_chamar_detector(template):
    def explode(*a, **k):
        raise AssertionError("não devia amostrar")
    assert rosto.aplicar(template, "v.mp4", 0, 10, amostrar_fn=explode) is template


def test_modo_desfoque_ignora_rosto(template):
    config = _config(template)
    config["reframe"]["modo"] = "desfoque"
    assert rosto.aplicar(config, "v.mp4", 0, 10,
                         amostrar_fn=lambda *a: 1 / 0) is config


def test_aplica_trilha_sem_mutar_o_original(template):
    config = _config(template)
    amostrar = lambda *a: (_amostras([0.3] * 5 + [0.7] * 5), DIM)
    novo = rosto.aplicar(config, "v.mp4", 0, 10, amostrar_fn=amostrar)
    assert len(novo["reframe"]["trilha"]) == 2
    assert "trilha" not in config["reframe"]


def test_detector_quebrado_degrada_com_aviso(template, caplog):
    config = _config(template)

    def quebra(*a):
        raise rosto.ErroRosto("sem opencv")
    assert rosto.aplicar(config, "v.mp4", 0, 10, amostrar_fn=quebra) is config
    assert "desligado neste clip" in caplog.text


def test_sem_rosto_mantem_ancora_fixa(template):
    config = _config(template)
    amostrar = lambda *a: (_amostras([None] * 8), DIM)
    assert rosto.aplicar(config, "v.mp4", 0, 10, amostrar_fn=amostrar) is config


# --- detector real, vídeo sintético ------------------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg ausente")
def test_amostrar_real_em_video_sem_rosto(tmp_path):
    pytest.importorskip("cv2")
    video = tmp_path / "s.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=10:duration=3",
         "-pix_fmt", "yuv420p", str(video)],
        capture_output=True, check=True)
    amostras, dim = rosto.amostrar(str(video), 0.0, 3.0, amostras_por_s=2)
    assert dim == (640, 360)
    assert len(amostras) >= 4
    assert all(c is None for _, c in amostras)
