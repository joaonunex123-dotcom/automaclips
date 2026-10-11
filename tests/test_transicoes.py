"""Biblioteca de transições: cadência (1 a cada 4 s, variando) e filtros."""
import copy

import pytest

from editing import efeitos, render, template as template_mod


@pytest.fixture
def todas(template):
    config = copy.deepcopy(template)
    for nome in efeitos.TRANSICOES:
        config["efeitos"][nome]["ativo"] = True
    return config


def _so(template, *nomes):
    config = copy.deepcopy(template)
    for nome in nomes:
        config["efeitos"][nome]["ativo"] = True
    return config


# --- padrão -------------------------------------------------------------------

def test_tudo_desligado_por_padrao(template):
    for nome in efeitos.TRANSICOES:
        assert template["efeitos"][nome]["ativo"] is False
    assert template["efeitos"]["cadencia"]["ativo"] is False
    assert efeitos.planejar_transicoes(template, 30.0, picos=[5, 15]) == []


def test_desligado_o_grafo_e_identico_ao_de_antes(template):
    com_plano = render.filtergraph(
        template, "x.ass", 30.0, efeitos={"picos": [3.0], "transicoes": []})
    sem = render.filtergraph(template, "x.ass", 30.0)
    assert com_plano == sem


def test_so_flash_ligado_mantem_o_comportamento_antigo(template):
    config = _so(template, "flash")
    assert not efeitos.cadencia_governa(config)
    # abertura + virada do hook, 1 s de distância: o legado não aplica a regra
    assert efeitos.instantes_flash(config, 30.0) == [0.0, 1.0]


# --- cadência -----------------------------------------------------------------

def test_no_maximo_um_efeito_a_cada_quatro_segundos(todas):
    picos = [i * 1.1 for i in range(1, 26)]       # picos muito densos
    ev = efeitos.planejar_transicoes(todas, 30.0, picos=picos)
    assert len(ev) >= 4
    instantes = [e["chave_s"] for e in ev]
    for a, b in zip(instantes, instantes[1:]):
        assert b - a >= 4.0 - 1e-6


def test_nunca_repete_o_tipo_em_seguida(todas):
    ev = efeitos.planejar_transicoes(todas, 60.0,
                                     picos=[5 + 4.5 * i for i in range(11)])
    tipos = [e["tipo"] for e in ev]
    assert len(tipos) >= 6
    assert all(a != b for a, b in zip(tipos, tipos[1:]))


def test_varia_entre_todos_os_tipos_ligados(todas):
    ev = efeitos.planejar_transicoes(todas, 90.0,
                                     picos=[3 + 4.2 * i for i in range(20)])
    assert {e["tipo"] for e in ev} >= {"shutter", "glitch", "zoom", "iris"}


def test_um_unico_tipo_ligado_repete_porque_nao_ha_alternativa(template):
    config = _so(template, "glitch")
    ev = efeitos.planejar_transicoes(config, 30.0, picos=[5, 10, 15, 20])
    assert len(ev) >= 4 and {e["tipo"] for e in ev} == {"glitch"}


def test_respeita_o_maximo_por_clip(todas):
    todas["efeitos"]["cadencia"]["maximo_por_clip"] = 2
    ev = efeitos.planejar_transicoes(todas, 60.0,
                                     picos=[5 + 5 * i for i in range(10)])
    assert len(ev) == 2


def test_virada_do_hook_ganha_de_um_pico_proximo(todas):
    todas["hook"]["ativo"] = True
    todas["hook"]["duracao_s"] = 2.0
    todas["efeitos"]["flash"]["na_abertura"] = False
    ev = efeitos.planejar_transicoes(todas, 30.0, picos=[3.0])
    assert [e["gatilho"] for e in ev] == ["virada"]


def test_entrada_de_grafico_dispara_transicao_estrutural(todas):
    todas["efeitos"]["flash"]["na_abertura"] = False
    todas["hook"]["ativo"] = False
    ev = efeitos.planejar_transicoes(
        todas, 30.0, segmentos_grafico=[{"inicio_s": 10.0, "fim_s": 20.0}])
    assert ev[0]["gatilho"] == "grafico_entrada"
    assert ev[0]["tipo"] in ("shutter", "iris", "whip_pan")
    assert ev[0]["chave_s"] == pytest.approx(10.0)


def test_plano_e_deterministico(todas):
    a = efeitos.planejar_transicoes(todas, 30.0, picos=[5, 12, 20, 26])
    b = efeitos.planejar_transicoes(todas, 30.0, picos=[5, 12, 20, 26])
    assert a == b


def test_janela_do_evento_cabe_no_clip(todas):
    ev = efeitos.planejar_transicoes(todas, 10.0, picos=[0.05, 9.98])
    for e in ev:
        assert e["t"] >= 0.0 and e["t"] + e["dur"] <= 10.0 + 1e-6


# --- filtros ------------------------------------------------------------------

def _evento(tipo, t=5.0, dur=0.3):
    return {"t": t, "dur": dur, "tipo": tipo, "gatilho": "enfase",
            "chave_s": t + dur / 2}


def test_shutter_tem_seis_laminas_com_janela(template):
    f = efeitos.filtros_transicoes(_so(template, "shutter"),
                                   [_evento("shutter")], 1080, 1920)
    assert len(f) == 6 and all(x.startswith("drawbox=") for x in f)
    assert all("enable='between(t,5.000,5.300)'" in x for x in f)


def test_glitch_usa_chroma_e_nao_rgb(template):
    # rgbashift forçaria RGB no grafo inteiro (46 s por clip, medido).
    f = efeitos.filtros_transicoes(_so(template, "glitch"),
                                   [_evento("glitch", dur=0.2)], 1080, 1920)
    texto = ",".join(f)
    assert "chromashift" in texto and "rgbashift" not in texto
    assert "eq=saturation=" in texto


def test_whip_pan_sobe_e_desce_o_desfoque(template):
    f = efeitos.filtros_transicoes(_so(template, "whip_pan"),
                                   [_evento("whip_pan")], 1080, 1920)
    tamanhos = [int(x.split("sizeX=")[1].split(":")[0]) for x in f]
    assert tamanhos[1] > tamanhos[0] == tamanhos[2]


def test_iris_e_fonte_de_baixa_resolucao_com_alfa(template):
    config = _so(template, "iris")
    fontes = efeitos.fontes_iris(config, [_evento("iris", dur=0.4)],
                                 1080, 1920, 30)
    rotulo, grafo, _ = fontes[0]
    assert rotulo == "[iris0]"
    assert "nullsrc=s=108x192" in grafo and "format=yuva420p" in grafo
    assert "scale=1080:1920" in grafo and "setpts=PTS+5.000/TB" in grafo
    # geq usa T maiúsculo
    assert "sin(PI*T/0.400)" in grafo


def test_acabamento_encadeia_a_iris_por_overlay(template):
    config = _so(template, "iris")
    trecho, rotulo = efeitos.filtro_acabamento(
        config, 30.0, "[vs]", eventos=[_evento("iris", dur=0.4)])
    assert "[vs][iris0]overlay=0:0:eof_action=pass" in trecho
    assert rotulo == "[vir0]"


def test_intensidade_zero_nao_fecha_nada(template):
    config = _so(template, "shutter")
    config["efeitos"]["shutter"]["intensidade"] = 0.0
    f = efeitos.filtros_transicoes(config, [_evento("shutter")], 1080, 1920)
    assert all("*0.000*" in x for x in f)


def test_zoom_do_plano_vira_zoompan_nos_instantes_do_plano(todas):
    ev = [_evento("zoom", t=7.0, dur=0.6)]
    grafo = render.filtergraph(todas, "x.ass", 30.0,
                               efeitos={"picos": [], "transicoes": ev})
    assert "zoompan" in grafo and "-7.000" in grafo


def test_flash_do_plano_usa_so_os_instantes_do_plano(todas):
    ev = [{"t": 12.0, "dur": 0.18, "tipo": "flash", "gatilho": "enfase",
           "chave_s": 12.0}]
    trecho, _ = efeitos.filtro_acabamento(todas, 30.0, "[vs]", eventos=ev)
    assert "between(t,12.000,12.180)" in trecho
    assert "between(t,0.000," not in trecho


# --- validação ----------------------------------------------------------------

@pytest.mark.parametrize("nome", ["shutter", "iris", "whip_pan", "glitch"])
@pytest.mark.parametrize("chave, valor", [
    ("intensidade", 1.5), ("duracao_s", 0.05), ("duracao_s", 1.0),
    ("ativo", "sim"),
])
def test_template_rejeita_transicao_invalida(template, nome, chave, valor):
    config = copy.deepcopy(template)
    config["efeitos"][nome][chave] = valor
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(config)


def test_cadencia_precisa_de_espacamento_positivo(template):
    config = copy.deepcopy(template)
    config["efeitos"]["cadencia"]["espacamento_minimo_s"] = 0.0
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(config)
