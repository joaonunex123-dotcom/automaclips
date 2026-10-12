"""Modo multi: 2+ pessoas que não cabem no crop 9:16 aparecem com quadro inteiro."""
import copy
import shutil
import subprocess

import pytest

from db import repositorio
from editing import editar, grafico, legendas, render, rosto
from editing import template as template_mod

PROP = 1080 / 1920


def _rosto(cx, largura=0.06, topo=0.2):
    """Caixa 0-1 de um rosto de `largura` centrado em cx (altura 1.8x, 16:9)."""
    return (cx - largura / 2, topo, cx + largura / 2, topo + largura * 1.8)


def _amostras(*listas, passo=0.5):
    return [(round(i * passo, 3), list(c)) for i, c in enumerate(listas)]


DIM = (1920, 1080)


# --- fusão de detectores ---------------------------------------------------------

def test_cascata_precisa_aceita_sozinha_e_a_ruidosa_so_com_apoio():
    a = _rosto(0.3)
    b = _rosto(0.7)
    # alt2 (aceita sozinha) acha a; a cascata ruidosa acha b sem apoio
    assert rosto.fundir_deteccoes([[a], [b]], [True, False]) == [a]
    # com apoio de outra cascata, b passa
    assert rosto.fundir_deteccoes([[a], [b], [b]], [True, False, False]) == [a, b]


def test_caixas_sobrepostas_viram_uma():
    a = _rosto(0.30)
    quase = _rosto(0.305)
    assert len(rosto.fundir_deteccoes([[a, quase]], [True])) == 1


# --- caixa envolvente: cabe / não cabe -------------------------------------------

def test_um_rosto_sempre_cabe_e_centra_nele():
    c = rosto.classificar(_amostras([_rosto(0.8)]), DIM, PROP)[0]
    assert c["modo"] == "single" and c["n"] == 1
    assert c["centro"] == pytest.approx(0.8, abs=1e-6)


def test_um_rosto_enorme_nunca_vira_multi():
    # close-up: 0.28 de largura + margem passaria da janela (0.316)
    c = rosto.classificar(_amostras([_rosto(0.5, largura=0.28)]), DIM, PROP)[0]
    assert c["modo"] == "single" and c["n"] == 1


def test_duas_pessoas_proximas_cabem_e_centram_na_caixa_nao_no_vao():
    # janela 9:16 de 1920x1080 = 607.5 px = 0.316 da largura
    c = rosto.classificar(_amostras([_rosto(0.40), _rosto(0.50)]), DIM, PROP)[0]
    assert c["modo"] == "single" and c["n"] == 2
    assert c["centro"] == pytest.approx(0.45, abs=0.01)


def test_duas_pessoas_afastadas_nao_cabem():
    c = rosto.classificar(_amostras([_rosto(0.25), _rosto(0.75)]), DIM, PROP)[0]
    assert c["modo"] == "multi" and c["n"] == 2


def test_tres_ou_mais_rostos_usam_a_caixa_de_todos():
    c = rosto.classificar(
        _amostras([_rosto(0.2), _rosto(0.45), _rosto(0.8)]), DIM, PROP)[0]
    assert c["modo"] == "multi" and c["n"] == 3
    assert c["caixa"][0] < 0.2 and c["caixa"][2] > 0.8


def test_margem_conta_na_caixa():
    # 2 rostos que SEM margem cabem (0.30) mas COM margem passam de 0.316
    c = rosto.classificar(_amostras([_rosto(0.40), _rosto(0.64)]), DIM, PROP)[0]
    assert c["modo"] == "multi"
    sem = rosto.classificar(_amostras([_rosto(0.40), _rosto(0.64)]), DIM, PROP,
                            margem=0.0)[0]
    assert sem["modo"] == "single"


def test_fonte_ja_vertical_nunca_vira_multi():
    c = rosto.classificar(_amostras([_rosto(0.2), _rosto(0.8)]), (1080, 1920),
                          PROP)[0]
    assert c["modo"] == "single"


def test_sem_rosto_nao_vota():
    c = rosto.classificar(_amostras([]), DIM, PROP)[0]
    assert c["modo"] is None and c["centro"] is None


# --- histerese -------------------------------------------------------------------

def test_histerese_ignora_oscilacao_curta():
    modos = ["single", "multi", "single", "multi", "single", "multi", "multi",
             "single", "single"]
    assert rosto.histerese(modos, 3, 4) == ["single"] * len(modos)


def test_histerese_entra_com_n_e_retroage_ao_inicio_da_sequencia():
    modos = ["single", "single", "multi", "multi", "multi", "multi"]
    assert rosto.histerese(modos, 3, 4) == [
        "single", "single", "multi", "multi", "multi", "multi"]


def test_histerese_sai_so_com_n_maior():
    modos = ["multi"] * 3 + ["single"] * 3 + ["multi"] * 2
    # 3 single seguidos não bastam (saida=4): continua multi
    assert rosto.histerese(modos, 3, 4) == ["multi"] * 8
    modos = ["multi"] * 3 + ["single"] * 4
    assert rosto.histerese(modos, 3, 4) == ["multi"] * 3 + ["single"] * 4


def test_histerese_amostra_sem_rosto_e_neutra():
    modos = ["multi", None, "multi", "multi", None, None, "single", "single",
             None, "single", "single"]
    est = rosto.histerese(modos, 3, 4)
    assert est[:4] == ["multi"] * 4 and est[-4:] == ["single"] * 4


def test_trilha_vazia_de_modos():
    assert rosto.histerese([], 3, 4) == []
    assert rosto.segmentos([], [], 0.5, 10.0) == []


# --- segmentos -------------------------------------------------------------------

def _classes(modos, passo=0.5):
    cs = []
    for i, m in enumerate(modos):
        cx = 0.5
        caixa = (0.2, 0.2, 0.8, 0.5) if m == "multi" else (0.45, 0.2, 0.55, 0.5)
        cs.append({"t": round(i * passo, 3), "modo": m, "centro": cx,
                   "n": 2 if m == "multi" else 1, "caixa": caixa})
    return cs


def test_segmentos_cobrem_o_clip_inteiro_e_contam_trocas():
    modos = ["single"] * 6 + ["multi"] * 8 + ["single"] * 6
    segs = rosto.segmentos(_classes(modos), modos, 0.5, 10.0)
    assert [s["modo"] for s in segs] == ["single", "multi", "single"]
    assert segs[0]["inicio_s"] == 0.0 and segs[-1]["fim_s"] == 10.0
    assert segs[1]["inicio_s"] == pytest.approx(2.75) \
        and segs[1]["fim_s"] == pytest.approx(6.75)
    assert segs[1]["caixa"] == (0.2, 0.2, 0.8, 0.5) and segs[1]["n_rostos"] == 2
    assert segs[0]["ancora_x"] == pytest.approx(0.5)
    assert rosto.trocas_de_modo(segs) == 2


def test_multi_curto_demais_volta_a_single():
    modos = ["single"] * 6 + ["multi"] * 1 + ["single"] * 6
    segs = rosto.segmentos(_classes(modos), modos, 0.5, 6.5, minimo_s=1.0)
    assert [s["modo"] for s in segs] == ["single"]


# --- detectar_multi (detector injetado) ------------------------------------------

@pytest.fixture
def cfg(template):
    c = copy.deepcopy(template)
    c["multi"]["ativo"] = True
    c["reframe"]["rosto"]["ativo"] = True
    return c


def _fn(amostras, dim=DIM):
    return lambda *a, **k: (amostras, dim)


def test_um_rosto_nao_muda_nada(cfg):
    am = _amostras(*[[_rosto(0.3)]] * 20)
    r = rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=_fn(am))
    assert [s["modo"] for s in r["segmentos"]] == ["single"]
    assert r["segmentos"][0]["n_rostos"] == 1


def test_trilha_vazia_devolve_none(cfg):
    am = _amostras(*[[]] * 20)
    assert rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=_fn(am)) is None
    assert rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=_fn([])) is None


def test_dois_apresentadores_distantes_viram_multi_sem_piscar(cfg):
    dois = [_rosto(0.25), _rosto(0.75)]
    # uma pessoa some por 1 amostra no meio (detecção falhou): não pisca
    seq = [[_rosto(0.25)]] * 6 + [dois] * 3 + [[_rosto(0.25)]] + [dois] * 6 \
        + [[_rosto(0.25)]] * 5
    r = rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=_fn(_amostras(*seq)))
    modos = [s["modo"] for s in r["segmentos"]]
    assert modos == ["single", "multi", "single"]
    assert rosto.trocas_de_modo(r["segmentos"]) == 2


def test_falha_do_detector_degrada(cfg):
    def quebra(*a, **k):
        raise RuntimeError("cv2.error")

    assert rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=quebra) is None

    def sem_cv(*a, **k):
        raise rosto.ErroRosto("sem opencv")

    assert rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=sem_cv) is None


def test_trilha_do_single_segue_o_centro_e_ignora_o_multi(cfg):
    dois = [_rosto(0.25), _rosto(0.75)]
    seq = [[_rosto(0.2)]] * 6 + [dois] * 8 + [[_rosto(0.8)]] * 6
    r = rosto.detectar_multi("v.mp4", 0, 10, cfg, amostrar_fn=_fn(_amostras(*seq)))
    t0 = r["trilha"][0][1]
    assert t0 < 0.3 and r["trilha"][-1][1] > 0.7      # nunca ficou no vão (0.5)
    assert all(a < 0.4 or a > 0.6 for _, a in r["trilha"])


# --- template --------------------------------------------------------------------

def test_template_traz_multi_desligado(template):
    m = template["multi"]
    assert m["ativo"] is False and m["fundo"] == "desfoque"
    assert (m["min_rosto"], m["histerese_entrada"], m["histerese_saida"]) \
        == (0.06, 3, 4)


@pytest.mark.parametrize("chave,valor", [
    ("ativo", "sim"), ("fundo", "xadrez"), ("min_rosto", 0.9),
    ("min_rosto", "alto"), ("histerese_entrada", 0), ("histerese_saida", 2.5),
    ("histerese_entrada", True), ("amostras_por_s", 0),
    ("maximo_por_clip", 99),
])
def test_template_rejeita_multi_invalido(template, chave, valor):
    c = copy.deepcopy(template)
    c["multi"][chave] = valor
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(c)


# --- filtergraph: reaproveita o do gráfico ---------------------------------------

def _seg(**kw):
    base = {"inicio_s": 4.0, "fim_s": 9.0, "recorte": None, "largura_final": 1080,
            "altura_final": 608, "fundo": "desfoque", "origem": "multi"}
    return {**base, **kw}


def test_fundo_do_segmento_vence_o_do_grafico(template):
    c = copy.deepcopy(template)
    c["grafico"]["fundo"] = "desfoque"
    solido = grafico.filtro_segmento(c, 0, _seg(fundo="solido"))
    assert "pad=1080:1920" in solido and "gblur" not in solido
    c["grafico"]["fundo"] = "solido"
    desf = grafico.filtro_segmento(c, 0, _seg(fundo="desfoque"))
    assert "gblur" in desf


def test_transicao_de_025s_igual_a_do_grafico(template):
    t = grafico.filtro_segmento(template, 0, _seg())
    assert "fade=t=in:st=4.000:d=0.250:alpha=1" in t


# --- integração com editar -------------------------------------------------------

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


def _analise(modos_por_segmento):
    segs = []
    for ini, fim, modo in modos_por_segmento:
        segs.append({"inicio_s": ini, "fim_s": fim, "modo": modo,
                     "ancora_x": None if modo == "multi" else 0.3,
                     "n_rostos": 2 if modo == "multi" else 1,
                     "caixa": (0.2, 0.2, 0.8, 0.5) if modo == "multi" else None})
    return {"segmentos": segs, "trilha": [(0.0, 0.3)], "dimensoes": (1920, 1080),
            "amostras": []}


def _renderizar_clip(conn, clip, config, tmp_path, **inj):
    recebido = {}

    def captura(config_r, *a, **k):
        recebido["config"] = config_r
        recebido.update(k)
        return "/r/x.mp4"

    editar.renderizar_clip(
        conn, clip, config, carregar_transcricao=lambda c: {"segmentos": []},
        renderizar=captura, destino_dir=str(tmp_path / "out"), **inj)
    return recebido


@pytest.fixture(autouse=True)
def fonte_16x9(monkeypatch):
    monkeypatch.setattr(grafico, "dimensoes_do_arquivo",
                        lambda *a, **k: (1920, 1080))


def test_desligado_nao_chama_o_detector(conn, template, clip, tmp_path):
    chamadas = []
    r = _renderizar_clip(conn, clip, template, tmp_path,
                         detectar_multi=lambda *a: chamadas.append(a))
    assert chamadas == [] and "efeitos" not in r


def test_multi_vira_segmento_do_grafico_com_o_fundo_configurado(
        conn, cfg, clip, tmp_path):
    cfg["multi"]["fundo"] = "solido"
    an = _analise([(0, 8, "single"), (8, 20, "multi"), (20, 30, "single")])
    r = _renderizar_clip(conn, clip, cfg, tmp_path,
                         detectar_multi=lambda *a: an)
    segs = r["efeitos"]["grafico"]
    assert len(segs) == 1
    assert (segs[0]["inicio_s"], segs[0]["fim_s"]) == (8, 20)
    assert segs[0]["fundo"] == "solido" and segs[0]["largura_final"] == 1080
    assert r["config"]["reframe"]["trilha"] == [(0.0, 0.3)]


def test_grafico_tem_prioridade_sobre_multi(conn, cfg, clip, tmp_path):
    cfg["grafico"]["ativo"] = True
    an = _analise([(0, 5, "single"), (5, 25, "multi"), (25, 30, "single")])
    r = _renderizar_clip(
        conn, clip, cfg, tmp_path,
        detectar_multi=lambda *a: an,
        detectar_grafico=lambda *a: [
            {"inicio_s": 10.0, "fim_s": 15.0, "caixa": None}])
    segs = r["efeitos"]["grafico"]
    intervalos = [(s["inicio_s"], s["fim_s"], s.get("origem")) for s in segs]
    assert intervalos == [(5, 10.0, "multi"), (10.0, 15.0, None),
                          (15.0, 25, "multi")]
    for a, b in zip(segs, segs[1:]):
        assert a["fim_s"] <= b["inicio_s"]          # nunca se sobrepõem


def test_multi_encoberto_pelo_grafico_some(conn, cfg, clip, tmp_path):
    cfg["grafico"]["ativo"] = True
    an = _analise([(0, 5, "single"), (5, 9, "multi"), (9, 30, "single")])
    r = _renderizar_clip(
        conn, clip, cfg, tmp_path, detectar_multi=lambda *a: an,
        detectar_grafico=lambda *a: [
            {"inicio_s": 4.0, "fim_s": 10.0, "caixa": None}])
    assert [s.get("origem") for s in r["efeitos"]["grafico"]] == [None]


def test_degrada_sem_rosto_ou_com_falha(conn, cfg, clip, tmp_path):
    r = _renderizar_clip(conn, clip, cfg, tmp_path, detectar_multi=lambda *a: None)
    assert "efeitos" not in r and "trilha" not in r["config"]["reframe"]

    def quebra(*a):
        raise RuntimeError("cv2")

    r = _renderizar_clip(conn, clip, cfg, tmp_path, detectar_multi=quebra)
    assert "efeitos" not in r


def test_so_single_usa_a_trilha_sem_segmento_de_quadro_inteiro(
        conn, cfg, clip, tmp_path):
    an = _analise([(0, 30, "single")])
    r = _renderizar_clip(conn, clip, cfg, tmp_path, detectar_multi=lambda *a: an)
    assert "efeitos" not in r
    assert r["config"]["reframe"]["trilha"] == [(0.0, 0.3)]


def test_depuracao_json_e_gravado(conn, cfg, clip, tmp_path):
    import json
    an = _analise([(0, 8, "single"), (8, 20, "multi"), (20, 30, "single")])
    _renderizar_clip(conn, clip, cfg, tmp_path, detectar_multi=lambda *a: an)
    arq = list((tmp_path / "out").glob("*_multi.json"))
    assert len(arq) == 1
    dados = json.loads(arq[0].read_text(encoding="utf-8"))
    assert dados["trocas_de_modo"] == 2 and len(dados["segmentos_usados"]) == 1


def test_cadencia_respeita_4s_entre_efeitos_com_multi(conn, cfg, clip, tmp_path):
    from editing import efeitos
    cfg["efeitos"]["cadencia"]["ativo"] = True
    cfg["efeitos"]["shutter"]["ativo"] = True
    cfg["efeitos"]["glitch"]["ativo"] = True
    an = _analise([(0, 8, "single"), (8, 10, "multi"), (10, 12, "single"),
                   (12, 20, "multi"), (20, 30, "single")])
    r = _renderizar_clip(conn, clip, cfg, tmp_path, detectar_multi=lambda *a: an)
    ev = r["efeitos"]["transicoes"]
    assert ev
    instantes = sorted(e["chave_s"] for e in ev)
    for a, b in zip(instantes, instantes[1:]):
        assert b - a >= 4.0 - 0.3          # espaçamento_minimo_s, na âncora da chave
    assert {e["gatilho"] for e in ev} & {"grafico_entrada", "grafico_saida"}


# --- legenda ---------------------------------------------------------------------

def test_legenda_fica_abaixo_do_quadro_16x9_sem_mexer(template):
    segs = [_seg(altura_final=608)]
    out = editar._legenda_fora_dos_rostos(template, segs, None)
    assert out is template


def test_legenda_desce_quando_o_quadro_a_alcanca(template):
    c = copy.deepcopy(template)
    c["legenda"]["margem_vertical"] = 700            # topo ~ y=1120
    out = editar._legenda_fora_dos_rostos(c, [_seg(altura_final=1000)], None)
    assert out is not c
    # frame vai até y=(1920+1000)/2=1460; legenda deve começar depois dele
    topo = 1920 - out["legenda"]["margem_vertical"] - int(c["legenda"]["tamanho"] * 1.3)
    assert topo >= 1460
    assert c["legenda"]["margem_vertical"] == 700    # original intacto


# --- ffmpeg real: expressão de filtro --------------------------------------------

FFMPEG = shutil.which("ffmpeg") or "/Users/joaonunes/ffmpeg-bin/ffmpeg"


@pytest.fixture(scope="module")
def fonte_sintetica(tmp_path_factory):
    caminho = tmp_path_factory.mktemp("fonte") / "src.mp4"
    r = subprocess.run(
        [FFMPEG, "-y", "-f", "lavfi", "-i", "testsrc2=s=640x360:r=25:d=6",
         "-f", "lavfi", "-i", "sine=f=440:d=6", "-shortest",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
         str(caminho)], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("ffmpeg indisponível")
    return str(caminho)


def _render_real(config, fonte, tmp_path, segmentos, duracao=6.0):
    ass = legendas.gerar_ass(config, [], hook_text="", duracao_s=duracao)
    cfg = copy.deepcopy(config)
    cfg["saida"].update(largura=360, altura=640, preset="ultrafast")
    saida = render.renderizar(
        cfg, fonte, 0.0, duracao, ass, "multi_t", destino_dir=str(tmp_path),
        caminho_ffmpeg=FFMPEG, efeitos={"grafico": segmentos} if segmentos else None)
    return saida


@pytest.mark.parametrize("segmentos", [
    [],                                                   # trilha vazia
    [_seg(inicio_s=2.0, fim_s=4.0)],                      # 1 segmento
    [_seg(inicio_s=0.5, fim_s=1.8), _seg(inicio_s=2.5, fim_s=3.2, fundo="solido"),
     _seg(inicio_s=4.0, fim_s=5.0)],                      # vários, fundos mistos
    [_seg(inicio_s=0.0, fim_s=6.0)],                      # borda a borda
    [_seg(inicio_s=5.2, fim_s=6.0)],                      # encosta no fim
])
def test_filtro_roda_no_ffmpeg_real(template, fonte_sintetica, tmp_path, segmentos):
    cfg = copy.deepcopy(template)
    cfg["legenda"]["ativa"] = False
    for s in segmentos:      # geometria já em escala da saída de teste
        s["largura_final"], s["altura_final"] = 360, 203
    saida = _render_real(cfg, fonte_sintetica, tmp_path, segmentos)
    r = subprocess.run([FFMPEG, "-hide_banner", "-i", saida],
                       capture_output=True, text=True)
    assert "360x640" in r.stderr and "Audio" in r.stderr
    assert "Duration: 00:00:0" in r.stderr
