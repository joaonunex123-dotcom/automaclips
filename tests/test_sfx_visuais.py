"""SFX sintetizados dos efeitos visuais: geração, sincronia e mixagem."""
import copy
import os
import shutil
import subprocess

import numpy as np
import pytest

from editing import efeitos, gerar_sfx, render, template as template_mod

FPS = 30


@pytest.fixture
def config(template, tmp_path):
    c = copy.deepcopy(template)
    for nome in efeitos.TRANSICOES:
        c["efeitos"][nome]["ativo"] = True
    c["efeitos"]["sfx_visuais"]["ativo"] = True
    c["sfx"]["diretorio"] = str(tmp_path / "sfx")
    return c


def _ev(tipo, chave, dur=0.3):
    return {"t": chave - dur / 2, "dur": dur, "tipo": tipo,
            "gatilho": "enfase", "chave_s": chave}


# --- geração ------------------------------------------------------------------

@pytest.mark.parametrize("nome", list(gerar_sfx.SONS))
def test_sons_sao_normalizados_e_sem_clip(nome):
    y = gerar_sfx.normalizar(gerar_sfx.SONS[nome]())
    teto = 10 ** (-1.0 / 20) + 1e-6
    assert np.max(np.abs(y)) <= teto
    lufs = gerar_sfx.lufs_sem_gating(y)
    # -24 LUFS, ou mais baixo quando o pico (-1 dBFS) manda num som esparso
    assert lufs <= -24.0 + 0.1
    assert np.isfinite(y).all()


def test_geracao_e_deterministica():
    a = gerar_sfx.shutter()
    b = gerar_sfx.shutter()
    assert np.array_equal(a, b)


def test_ancoras_fazem_sentido_para_cada_som():
    anc = {n: gerar_sfx.ancora_do_arquivo(n) for n in gerar_sfx.SONS_VISUAIS}
    assert anc["shutter"] < 0.03 and anc["impacto"] < 0.03 and anc["pop"] < 0.03
    assert anc["riser"] > 0.6                  # chega no fim
    assert 0.05 < anc["whoosh"] < 0.3          # passa no meio


def test_gerar_um_nao_regera_arquivo_existente(tmp_path):
    caminho = gerar_sfx.gerar_um("pop", str(tmp_path))
    marca = os.path.getmtime(caminho)
    assert gerar_sfx.gerar_um("pop", str(tmp_path)) == caminho
    assert os.path.getmtime(caminho) == marca


# --- plano --------------------------------------------------------------------

def test_desligado_nao_planeja_nada(template):
    assert efeitos.plano_sfx_visuais(template, [_ev("shutter", 5.0)], 30.0) == []


def test_cada_efeito_dispara_o_som_mapeado(config):
    eventos = [_ev("shutter", 5.0), _ev("whip_pan", 10.0), _ev("iris", 15.0),
               _ev("glitch", 20.0), _ev("flash", 25.0)]
    sons = efeitos.plano_sfx_visuais(config, eventos, 30.0)
    assert [s["som"] for s in sons] == ["shutter", "whoosh", "riser", "pop",
                                        "impacto"]
    assert all(os.path.exists(s["caminho"]) for s in sons)


def test_som_bate_no_mesmo_quadro_do_efeito(config):
    eventos = [_ev("shutter", 5.017), _ev("iris", 12.4), _ev("whip_pan", 20.2)]
    for s in efeitos.plano_sfx_visuais(config, eventos, 30.0):
        ancora = efeitos.ancora_do_som(s["som"])
        bate = s["instante_s"] + ancora - s["corte_ini_s"]
        assert abs(bate - s["chave_s"]) <= 1.0 / FPS


def test_som_que_cairia_antes_do_clip_e_cortado_nao_atrasado(config):
    s = efeitos.plano_sfx_visuais(config, [_ev("iris", 0.3)], 30.0)[0]
    assert s["instante_s"] == 0.0 and s["corte_ini_s"] > 0.3
    ancora = efeitos.ancora_do_som("riser")
    assert 0.3 == pytest.approx(0.0 + ancora - s["corte_ini_s"], abs=1 / FPS)


def test_categoria_desligada_silencia_so_aquele_som(config):
    config["efeitos"]["sfx_visuais"]["categorias"]["shutter"] = False
    sons = efeitos.plano_sfx_visuais(
        config, [_ev("shutter", 5.0), _ev("glitch", 10.0)], 30.0)
    assert [s["som"] for s in sons] == ["pop"]


def test_som_que_nao_cabe_no_fim_do_clip_e_ignorado(config):
    assert efeitos.plano_sfx_visuais(config, [_ev("glitch", 29.99)], 30.0) == []


# --- mixagem ------------------------------------------------------------------

def _som(corte=0.0, instante=5.0):
    return {"tipo": "sfx_visual", "caminho": "/s/x.wav", "instante_s": instante,
            "corte_ini_s": corte, "volume": 0.8}


def test_cadeia_aplica_teto_ducking_e_limiter(config):
    for nome in ("voz", "ducking", "limiter"):
        config["efeitos"][nome]["ativo"] = True
    grafo = efeitos.cadeia_audio(config, [_som()], 30.0,
                                 medir=lambda c: -24.0)
    assert "loudnorm=I=-16" in grafo
    assert "sidechaincompress" in grafo
    assert grafo.endswith("alimiter=limit=0.8414:attack=5:release=60:"
                          "level=disabled[aout]")
    # -24 LUFS (arquivo) já está a 8 dB sob -16: o volume do template (0.8)
    # é menor que o teto (1.0), então vale o do template.
    assert "volume=0.8000" in grafo


def test_teto_limita_som_mais_alto_que_a_voz(config):
    config["efeitos"]["voz"]["ativo"] = True
    grafo = efeitos.cadeia_audio(config, [_som()], 30.0,
                                 medir=lambda c: -12.0)    # 12 dB mais alto
    # ganho máx = 10^((-16 - 8 - -12)/20) = 0.2512
    assert "volume=0.2512" in grafo


def test_sem_limiter_o_final_e_o_de_antes(config):
    config["efeitos"]["voz"]["ativo"] = True
    grafo = efeitos.cadeia_audio(config, [_som()], 30.0, medir=lambda c: -24.0)
    assert "alimiter" not in grafo and grafo.endswith("normalize=0[aout]")


def test_corte_inicial_vira_atrim_antes_do_atraso(config):
    config["efeitos"]["voz"]["ativo"] = True
    grafo = efeitos.cadeia_audio(config, [_som(corte=0.25, instante=0.0)],
                                 30.0, medir=lambda c: -24.0)
    assert "atrim=start=0.2500,asetpts=PTS-STARTPTS,adelay=0:all=1" in grafo


def test_limiter_tambem_fecha_a_cadeia_sem_efeitos(config):
    config["efeitos"]["limiter"]["ativo"] = True
    grafo = efeitos.cadeia_audio(config, [], 30.0)
    assert "[voz0]anull[mst]" in grafo and grafo.endswith("[aout]")


# --- template -----------------------------------------------------------------

def test_template_vem_com_sfx_visuais_desligado(template):
    assert template["efeitos"]["sfx_visuais"]["ativo"] is False
    assert template["efeitos"]["limiter"]["ativo"] is False


@pytest.mark.parametrize("campo, valor", [
    (("categorias", "gemido"), True),
    (("categorias", "pop"), "sim"),
    (("mapa", "shutter"), "gemido"),
    (("mapa", "nada"), "pop"),
    (("volume",), -1),
])
def test_template_rejeita_sfx_visuais_invalido(template, campo, valor):
    c = copy.deepcopy(template)
    alvo = c["efeitos"]["sfx_visuais"]
    for k in campo[:-1]:
        alvo = alvo[k]
    alvo[campo[-1]] = valor
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(c)


# --- sincronia no áudio real (precisa do ffmpeg) -------------------------------

@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="sem ffmpeg")
def test_transiente_no_audio_renderizado_cai_no_quadro_do_efeito(config, tmp_path):
    """Fonte muda de 6 s + shutter em 3,0 s: o clique tem de estar em ~3,0 s."""
    ffmpeg = shutil.which("ffmpeg")
    fonte = tmp_path / "mudo.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
         "color=c=gray:s=320x180:r=30:d=6", "-f", "lavfi", "-i",
         "anullsrc=r=48000:cl=stereo", "-t", "6", "-shortest", "-c:v",
         "libx264", "-c:a", "aac", str(fonte)], check=True)
    config["saida"]["preset"] = "ultrafast"
    config["hook"]["ativo"] = False
    config["efeitos"]["voz"]["ativo"] = False
    config["efeitos"]["flash"]["na_abertura"] = False
    evento = _ev("shutter", 3.0)
    sons = efeitos.plano_sfx_visuais(config, [evento], 6.0)
    ass = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
           "\n[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, "
           "SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, "
           "Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
           "Style: D,Arial,40,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,"
           "0,0,100,100,0,0,1,1,0,2,10,10,10,1\n\n[Events]\nFormat: Layer, "
           "Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    saida = render.renderizar(
        config, str(fonte), 0.0, 6.0, ass, "sync", str(tmp_path),
        sons=sons, efeitos={"transicoes": [evento]})
    wav = tmp_path / "audio.wav"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", saida, "-vn",
                    "-ac", "1", "-ar", "48000", str(wav)], check=True)
    import soundfile as sf
    x, taxa = sf.read(str(wav))
    ativo = np.flatnonzero(np.abs(x) > 0.02)
    assert len(ativo) > 0
    primeiro = ativo[0] / taxa
    # o ataque do clique é a âncora (~0): o primeiro som audível é o do efeito
    assert abs(primeiro - 3.0) <= 1.5 / FPS
