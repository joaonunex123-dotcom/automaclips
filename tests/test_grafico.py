"""Modo gráfico: detecção heurística, geometria e filtergraph."""
import copy

import numpy as np
import pytest

from db import repositorio
from editing import editar, grafico, render, template as template_mod


def _grafico_sintetico():
    """Fundo branco, grade e barras: o que um slide de dados parece."""
    import cv2
    img = np.full((216, 384, 3), 250, np.uint8)
    for y in range(30, 190, 32):
        cv2.line(img, (20, y), (360, y), (200, 200, 200), 1)
    cv2.line(img, (20, 20), (20, 190), (40, 40, 40), 2)
    cv2.line(img, (20, 190), (360, 190), (40, 40, 40), 2)
    for i, (h, cor) in enumerate([(90, (200, 120, 40)), (140, (60, 160, 230)),
                                  (60, (80, 180, 90))]):
        cv2.rectangle(img, (50 + i * 100, 190 - h), (110 + i * 100, 190), cor, -1)
    return img


def _foto_ruidosa(seed=1):
    rng = np.random.default_rng(seed)
    base = rng.integers(40, 200, (216, 384, 3)).astype("uint8")
    import cv2
    return cv2.GaussianBlur(base, (0, 0), 1.5)


# --- características ----------------------------------------------------------

def test_grafico_sintetico_e_reconhecido():
    c = grafico.caracteristicas(_grafico_sintetico(), detectar_rosto=lambda f: [])
    assert c["plano"] >= grafico.LIMIAR_PLANO
    assert c["eixos"] >= grafico.LIMIAR_EIXOS
    assert grafico.eh_grafico(c)


def test_foto_ruidosa_nao_e_grafico():
    c = grafico.caracteristicas(_foto_ruidosa(), detectar_rosto=lambda f: [])
    assert not grafico.eh_grafico(c)


def test_rosto_veta_mesmo_com_fundo_de_slide():
    c = grafico.caracteristicas(
        _grafico_sintetico(), detectar_rosto=lambda f: [(100, 40, 60, 80)])
    assert c["rosto"] > 0 and not grafico.eh_grafico(c)


def test_caixa_do_conteudo_fica_dentro_do_quadro():
    c = grafico.caracteristicas(_grafico_sintetico(), detectar_rosto=lambda f: [])
    x0, y0, x1, y1 = c["caixa"]
    assert 0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0


# --- segmentos ----------------------------------------------------------------

def _instantes(n, passo=0.5):
    return [passo * (i + 0.5) for i in range(n)]


def test_segmento_abrange_os_quadros_marcados():
    flags = [False] * 4 + [True] * 6 + [False] * 4
    segs = grafico.montar_segmentos(_instantes(14), flags, [None] * 14, 0.5, 7.0)
    assert len(segs) == 1
    assert segs[0]["inicio_s"] == pytest.approx(2.0)
    assert segs[0]["fim_s"] == pytest.approx(5.0)


def test_lacuna_de_um_quadro_nao_parte_o_segmento():
    flags = [True, True, True, False, True, True, True]
    segs = grafico.montar_segmentos(_instantes(7), flags, [None] * 7, 0.5, 3.5)
    assert len(segs) == 1


def test_lacuna_grande_gera_dois_segmentos():
    flags = [True] * 4 + [False] * 3 + [True] * 4
    segs = grafico.montar_segmentos(_instantes(11), flags, [None] * 11, 0.5, 5.5,
                                    min_duracao_s=1.0)
    assert len(segs) == 2


def test_segmento_curto_demais_e_descartado():
    flags = [False, True, True, False, False]
    assert grafico.montar_segmentos(_instantes(5), flags, [None] * 5, 0.5, 2.5,
                                    min_duracao_s=1.5) == []


def test_respeita_o_maximo_por_clip():
    flags = ([True] * 4 + [False] * 3) * 4
    segs = grafico.montar_segmentos(_instantes(28), flags, [None] * 28, 0.5, 14,
                                    min_duracao_s=1.0, maximo=2)
    assert len(segs) == 2


def test_caixa_do_segmento_e_a_uniao():
    flags = [True] * 4
    caixas = [(0.1, 0.1, 0.5, 0.5), (0.2, 0.05, 0.6, 0.4), None,
              (0.0, 0.2, 0.4, 0.9)]
    seg = grafico.montar_segmentos(_instantes(4), flags, caixas, 0.5, 2.0,
                                   min_duracao_s=1.0)[0]
    assert seg["caixa"] == (0.0, 0.05, 0.6, 0.9)


def test_detectar_degrada_para_vazio_se_a_amostragem_falha(template):
    config = copy.deepcopy(template)

    def executar(*a, **k):
        raise OSError("sem ffmpeg")
    assert grafico.detectar("x.mp4", 0, 30, config, executar=executar) == []


# --- geometria ----------------------------------------------------------------

def test_fonte_16x9_ja_ocupa_a_largura_toda(template):
    plano = grafico.plano_do_segmento(
        {"inicio_s": 1, "fim_s": 5, "caixa": (0, 0, 1, 1)}, template, 1920, 1080)
    assert plano["largura_final"] == 1080 and not plano["ampliado"]
    assert plano["recorte"] is None


def test_ampliacao_so_acontece_abaixo_do_minimo(template):
    config = copy.deepcopy(template)
    # Altura-alvo baixa (split) faz a fonte larga ficar limitada pela altura.
    seg = {"inicio_s": 0, "fim_s": 4, "caixa": (0.25, 0.1, 0.75, 0.9)}
    plano = grafico.plano_do_segmento(seg, config, 1000, 1000, altura_alvo=600)
    assert plano["largura_final"] == 600           # 55% de 1080: pequeno
    assert plano["ampliado"] is False              # recorte não ganha nada: quadrado
    seg = {"inicio_s": 0, "fim_s": 4, "caixa": (0.0, 0.3, 1.0, 0.7)}
    plano = grafico.plano_do_segmento(seg, config, 1000, 1000, altura_alvo=600)
    assert plano["ampliado"] is True
    assert plano["largura_final"] > 600
    assert plano["recorte"]["w"] <= 1000 and plano["recorte"]["h"] <= 1000


# --- filtergraph --------------------------------------------------------------

def _config(template, fundo="desfoque"):
    config = copy.deepcopy(template)
    config["grafico"]["ativo"] = True
    config["grafico"]["fundo"] = fundo
    return config


def _seg(**kw):
    base = {"inicio_s": 10.0, "fim_s": 20.0, "recorte": None,
            "largura_final": 1080, "altura_final": 608}
    return {**base, **kw}


def test_sem_segmento_o_grafo_nao_muda(template):
    assert render.filtergraph(template, "x.ass", 30.0) == render.filtergraph(
        _config(template), "x.ass", 30.0, efeitos={"picos": [1.0]})


def test_com_segmento_a_fonte_e_dividida_e_recomposta(template):
    grafo = render.filtergraph(_config(template), "x.ass", 30.0,
                               efeitos={"grafico": [_seg()]})
    assert grafo.startswith("[0:v]split=2[gorig][gsrc0];")
    assert "[gorig]crop=" in grafo and "[vrbase];" in grafo
    assert "[vrbase][vg0]overlay=0:0:eof_action=pass" in grafo
    assert "[vr];" in grafo and grafo.endswith("[vout]")


def test_transicao_e_um_fade_de_alfa_nas_duas_pontas(template):
    trecho = grafico.filtro_segmento(_config(template), 0, _seg())
    assert "fade=t=in:st=10.000:d=0.250:alpha=1" in trecho
    assert "fade=t=out:st=19.750:d=0.250:alpha=1" in trecho
    assert "format=yuva420p" in trecho


def test_fundo_desfoque_borra_em_baixa_resolucao_e_escurece(template):
    trecho = grafico.filtro_segmento(_config(template, "desfoque"), 0, _seg())
    assert "gblur=sigma=" in trecho and "eq=brightness=" in trecho
    assert "scale=108:192" in trecho         # reduz antes de borrar


def test_fundo_solido_usa_pad_com_a_cor(template):
    trecho = grafico.filtro_segmento(_config(template, "solido"), 0, _seg())
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=0x101418" in trecho
    assert "gblur" not in trecho


def test_ampliacao_vira_crop_antes_do_fit(template):
    seg = _seg(recorte={"x": 10, "y": 20, "w": 800, "h": 400})
    trecho = grafico.filtro_segmento(_config(template), 0, seg)
    assert "crop=800:400:10:20" in trecho


def test_dois_segmentos_dividem_em_tres_copias(template):
    grafo = render.filtergraph(
        _config(template), "x.ass", 30.0,
        efeitos={"grafico": [_seg(), _seg(inicio_s=22.0, fim_s=28.0)]})
    assert grafo.startswith("[0:v]split=3[gorig][gsrc0][gsrc1];")
    assert "[vrbase][vg0]overlay=0:0:eof_action=pass:format=auto[vgc0];" in grafo
    assert "[vgc0][vg1]overlay=" in grafo


def test_split_usa_a_altura_do_clip(template):
    config = _config(template)
    grafo = render.filtergraph(
        config, "x.ass", 30.0,
        filler={"caminho": "f.mp4", "inicio_s": 0, "loop": False},
        efeitos={"grafico": [_seg()]})
    # 0.6 * 1920 = 1152: o fit do gráfico respeita a faixa do clip
    assert "scale=1080:1152:force_original_aspect_ratio=decrease" in grafo


# --- template -----------------------------------------------------------------

def test_template_traz_grafico_desligado(template):
    assert template["grafico"]["ativo"] is False
    assert template["grafico"]["fundo"] == "desfoque"


@pytest.mark.parametrize("chave, valor", [
    ("fundo", "gradiente"), ("ativo", "sim"), ("transicao_s", 5.0),
    ("largura_minima", 0.0), ("maximo_por_clip", 0),
])
def test_template_rejeita_grafico_invalido(template, chave, valor):
    config = copy.deepcopy(template)
    config["grafico"][chave] = valor
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(config)


# --- integração com editar ----------------------------------------------------

@pytest.fixture
def clip(conn, video, tmp_path):
    fonte = tmp_path / "v.mp4"
    fonte.write_text("v", encoding="utf-8")
    fila_id = repositorio.registrar_observacao(
        conn, video(video_id="vid1"), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO)
    repositorio.registrar_midia(conn, fila_id, video_path=str(fonte),
                                audio_path=str(tmp_path / "a.wav"),
                                duracao_real_s=600.0)
    repositorio.registrar_clips(conn, fila_id, [{
        "inicio_s": 100.0, "fim_s": 130.0, "score_claude": 9.0, "motivo": "m",
        "hook_text": "h", "picos_energia": 0, "score_final": 9.0,
        "picos_instantes": []}])
    return repositorio.clips_para_renderizar(conn)[0]


def test_desligado_nao_detecta_nada(conn, template, clip):
    chamadas = []
    editar.renderizar_clip(
        conn, clip, template, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: "/r/x.mp4",
        detectar_grafico=lambda *a: chamadas.append(a) or [])
    assert chamadas == []


def test_ligado_passa_os_segmentos_ao_render(conn, template, clip, monkeypatch):
    config = _config(template)
    monkeypatch.setattr(grafico, "dimensoes_do_arquivo",
                        lambda *a, **k: (1920, 1080))
    recebido = {}
    editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: recebido.update(k) or "/r/x.mp4",
        detectar_grafico=lambda v, i, f, c: [
            {"inicio_s": 5.0, "fim_s": 12.0, "caixa": None}])
    segs = recebido["efeitos"]["grafico"]
    assert segs[0]["inicio_s"] == 5.0 and segs[0]["largura_final"] == 1080


def test_falha_na_deteccao_deixa_o_clip_como_antes(conn, template, clip):
    config = _config(template)

    def quebra(*a):
        raise RuntimeError("cv2 sumiu")

    recebido = {}
    editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=lambda *a, **k: recebido.update(k) or "/r/x.mp4",
        detectar_grafico=quebra)
    assert "efeitos" not in recebido
