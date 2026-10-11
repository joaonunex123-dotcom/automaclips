"""Efeitos de edição: validação das flags e expressões de filtro do ffmpeg.

Nenhum teste executa o binário; o que se trava aqui é o texto do filtergraph.
"""
import copy

import pytest

from editing import efeitos as ef
from editing import render
from editing import template as template_mod
from editing.template import TemplateInvalido


def _com(template, **secoes):
    """Cópia do template com as chaves de `efeitos` sobrepostas."""
    config = copy.deepcopy(template)
    for nome, valores in secoes.items():
        if isinstance(valores, dict):
            config["efeitos"][nome].update(valores)
        else:
            config["efeitos"][nome] = valores
    return config


SONS = [
    {"instante_s": 0.0, "volume": 0.7, "caminho": "/x/whoosh.wav"},
    {"instante_s": 2.5, "volume": 0.5, "caminho": "/x/pop.wav"},
]
FILLER = {"caminho": "/x/f.mp4", "inicio_s": 3.0, "loop": False}


# --- padrão e degradação -------------------------------------------------------

def test_template_entrega_tudo_desligado(template):
    for nome in ("zoom", "flash", "vinheta", "barra", "voz", "ducking", "pad"):
        assert template["efeitos"][nome]["ativo"] is False
    assert not ef.algum_ativo(template)


def test_desligado_o_grafo_nao_muda(template):
    """Sem flag ligada, nenhum caractere de efeito entra no filtergraph."""
    grafo = render.filtergraph(template, "a.ass", 20.0, sons=SONS, filler=FILLER,
                               efeitos={"picos": [3.0, 9.0]})
    for marca in ("zoompan=z='1+", "eq=brightness", "vignette", "overlay=x=",
                  "loudnorm", "sidechaincompress", "[vp]", "[vfx]", "[vbar]"):
        assert marca not in grafo
    assert grafo == render.filtergraph(template, "a.ass", 20.0, sons=SONS,
                                       filler=FILLER)


def test_desligado_mapeia_a_trilha_original(template):
    args = render.comando(template, "/v.mp4", 0, 20.0, "a.ass", "/o.mp4")
    assert "0:a?" in args and "[aout]" not in args


# --- validação das flags -------------------------------------------------------

def test_ativo_precisa_ser_booleano(tmp_path):
    import json
    caminho = tmp_path / "t.json"
    caminho.write_text(json.dumps({"efeitos": {"zoom": {"ativo": "sim"}}}))
    with pytest.raises(TemplateInvalido, match="efeitos.zoom.ativo"):
        template_mod.carregar(str(caminho))


@pytest.mark.parametrize("secao,chave,valor,trecho", [
    ("zoom", "modo", "aleatorio", "efeitos.zoom.modo"),
    ("zoom", "fator", 0.9, "efeitos.zoom.fator"),
    ("zoom", "fator", 3.0, "efeitos.zoom.fator"),
    ("zoom", "duracao_s", 0, "efeitos.zoom.duracao_s"),
    ("zoom", "intervalo_s", "4", "efeitos.zoom.intervalo_s"),
    ("flash", "intensidade", 1.5, "efeitos.flash.intensidade"),
    ("flash", "duracao_s", -1, "efeitos.flash.duracao_s"),
    ("vinheta", "angulo", 4, "efeitos.vinheta.angulo"),
    ("barra", "altura", 0, "efeitos.barra.altura"),
    ("barra", "posicao", "meio", "efeitos.barra.posicao"),
    ("barra", "cor", "vermelho", "cor"),
    ("voz", "lufs", 0, "efeitos.voz.lufs"),
    ("ducking", "razao", 0.5, "efeitos.ducking.razao"),
    ("ducking", "limiar", 0, "efeitos.ducking.limiar"),
    ("pad", "volume", -1, "efeitos.pad.volume"),
])
def test_valores_invalidos_sao_recusados(template, secao, chave, valor, trecho):
    config = _com(template, **{secao: {chave: valor}})
    with pytest.raises(TemplateInvalido, match=trecho):
        template_mod.validar(config)


def test_teto_do_sfx_nao_pode_ser_negativo(template):
    config = _com(template, teto_sfx_abaixo_da_voz_db=-3)
    with pytest.raises(TemplateInvalido, match="teto_sfx_abaixo_da_voz_db"):
        template_mod.validar(config)


def test_template_sem_a_secao_usa_os_defaults(tmp_path):
    import json
    caminho = tmp_path / "t.json"
    caminho.write_text(json.dumps({"versao": "9"}))
    config = template_mod.carregar(str(caminho))
    assert not ef.algum_ativo(config)


# --- instantes -----------------------------------------------------------------

def test_zoom_por_picos_respeita_espaco_e_cabe_no_clip(template):
    config = _com(template, zoom={"ativo": True, "modo": "picos",
                                  "duracao_s": 0.6, "espacamento_minimo_s": 2.0})
    # 5.0 e 5.5 colidem; 19.8 estoura o clip (bump de 0.6 s não cabe).
    inst = ef.instantes_zoom(config, 20.0, picos=[5.0, 5.5, 9.0, 19.8])
    assert len(inst) == 2
    assert inst[0] == pytest.approx(5.0 - 0.6 * 0.25)
    assert inst[1] == pytest.approx(9.0 - 0.15)


def test_zoom_por_picos_sem_pico_nao_faz_nada(template):
    config = _com(template, zoom={"ativo": True, "modo": "picos"})
    assert ef.instantes_zoom(config, 20.0, picos=[]) == []
    assert ef.filtro_punch(config, 20.0, "[vr]", None, picos=None) == ""


def test_zoom_por_intervalo_distribui_e_respeita_o_teto(template):
    config = _com(template, zoom={"ativo": True, "modo": "intervalo",
                                  "intervalo_s": 4.0, "duracao_s": 0.6,
                                  "maximo_por_clip": 3})
    assert ef.instantes_zoom(config, 30.0) == [2.0, 6.0, 10.0]


def test_flash_marca_abertura_e_fim_do_hook(template):
    config = _com(template, flash={"ativo": True})
    hook = config["hook"]["duracao_s"]
    assert ef.instantes_flash(config, 20.0) == [0.0, hook]


def test_flash_sem_hook_so_na_abertura(template):
    config = _com(template, flash={"ativo": True})
    config["hook"]["ativo"] = False
    assert ef.instantes_flash(config, 20.0) == [0.0]


# --- expressões de filtro ------------------------------------------------------

def test_punch_usa_zoompan_com_ease_e_tempo_real(template):
    config = _com(template, zoom={"ativo": True, "modo": "intervalo",
                                  "intervalo_s": 6.0, "fator": 1.1,
                                  "duracao_s": 0.5})
    trecho = ef.filtro_punch(config, 12.0, "[vr]", altura=1152)
    assert trecho.startswith("[vr]fps=30,zoompan=z='1+0.1000*(")
    assert "pow(sin(PI*clip(((on/30)-3.000)/0.500,0,1)),2)" in trecho
    assert "s=1080x1152:fps=30[vp];" in trecho
    # vírgulas dentro de aspas simples: o parser do filtergraph as ignora.
    assert trecho.count("'") % 2 == 0


def test_punch_entra_depois_do_zoom_lento_e_antes_do_split(template):
    config = _com(template, zoom={"ativo": True, "modo": "intervalo",
                                  "intervalo_s": 6.0})
    config["zoom"]["ativo"] = True
    grafo = render.filtergraph(config, "a.ass", 20.0, filler=FILLER)
    assert grafo.index("[vz]fps=30,zoompan") > grafo.index("[vr]zoompan")
    assert grafo.index("[vp]fps=30,format") > grafo.index("[vz]fps=30,zoompan")
    assert grafo.endswith("[vs]ass=a.ass[vout]")


def test_flash_vira_brilho_decaindo_por_quadro(template):
    config = _com(template, flash={"ativo": True, "intensidade": 0.4,
                                   "duracao_s": 0.2})
    trecho, saida = ef.filtro_acabamento(config, 20.0, "[vs]")
    assert saida == "[vfx]"
    assert "eq=brightness='0.400*(" in trecho
    assert "between(t,0.000,0.200)*(1-(t-0.000)/0.200)" in trecho
    assert ":eval=frame" in trecho


def test_vinheta_e_barra(template):
    config = _com(template, vinheta={"ativo": True, "angulo": 0.5},
                  barra={"ativo": True, "altura": 6, "cor": "#FF0000"})
    trecho, saida = ef.filtro_acabamento(config, 25.0, "[vs]")
    assert "vignette=angle=0.500" in trecho
    assert "color=c=0xFF0000:s=1080x6:r=30:d=25.000[barra];" in trecho
    # overlay (reavaliado por quadro); drawbox avalia w uma vez só.
    assert "overlay=x='-w+w*t/25.000':y=H-h:shortest=1[vbar];" in trecho
    assert "drawbox" not in trecho
    assert saida == "[vbar]"


def test_acabamento_desligado_devolve_a_entrada(template):
    assert ef.filtro_acabamento(template, 20.0, "[vs]") == ("", "[vs]")


def test_legenda_vem_depois_dos_efeitos_de_acabamento(template):
    config = _com(template, flash={"ativo": True}, barra={"ativo": True})
    grafo = render.filtergraph(config, "a.ass", 20.0)
    assert grafo.index("eq=brightness") < grafo.index("ass=a.ass")
    assert grafo.endswith("[vbar]ass=a.ass[vout]")


# --- áudio ---------------------------------------------------------------------

def test_voz_normalizada_em_16_lufs(template):
    config = _com(template, voz={"ativo": True})
    grafo = ef.cadeia_audio(config, [], 20.0)
    assert "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000[voz0]" in grafo
    assert grafo.endswith("[voz0]anull[aout]")


def test_ducking_comprime_os_efeitos_com_a_voz_como_sidechain(template):
    config = _com(template, voz={"ativo": True}, ducking={"ativo": True})
    grafo = ef.cadeia_audio(config, SONS, 20.0, medir=lambda c: None)
    assert "[voz0]asplit=2[voz][sc]" in grafo
    assert "[mix][sc]sidechaincompress=threshold=0.03:ratio=8:attack=5:release=250" in grafo
    assert "apad=whole_dur=20.000[mix]" in grafo
    assert grafo.endswith(
        "[voz][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[aout]")


def test_sem_ducking_mistura_direto_e_sem_normalizar(template):
    config = _com(template, voz={"ativo": True})
    grafo = ef.cadeia_audio(config, SONS, 20.0, medir=lambda c: None)
    assert "sidechaincompress" not in grafo
    assert "[voz0][bus0][bus1]amix=inputs=3:duration=first" in grafo
    assert "normalize=0[aout]" in grafo


def test_teto_limita_o_efeito_a_8_db_da_voz():
    # voz -16, teto 8 dB => -24 LUFS. Arquivo a -18 LUFS (6 dB acima): ganho
    # máximo = -6 dB = 0.501, mesmo com volume 0.9 pedido pelo template.
    assert ef.volume_com_teto(0.9, -18.0, -16.0, 8.0) == pytest.approx(0.5012, abs=1e-3)
    # Arquivo já em -24: o volume do template (0.5) é o que vale.
    assert ef.volume_com_teto(0.5, -24.0, -16.0, 8.0) == 0.5
    # Sem medida: nunca amplifica.
    assert ef.volume_com_teto(1.7, None, -16.0, 8.0) == 1.0


def test_efeito_pontual_recebe_volume_com_teto_no_grafo(template):
    config = _com(template, voz={"ativo": True})
    grafo = ef.cadeia_audio(config, SONS[:1], 20.0, medir=lambda c: -18.0)
    assert "volume=0.5012" in grafo       # 0.7 pedido, teto de -6 dB


def test_pad_entra_em_loop_com_fade_e_corte_na_duracao(template, tmp_path):
    config = _com(template, pad={"ativo": True})
    config["sfx"]["diretorio"] = str(tmp_path)
    (tmp_path / "pad_slime.wav").write_bytes(b"x")
    plano = ef.plano_pad(config, "slime")
    assert plano[0]["loop"] is True and plano[0]["tipo"] == "pad"
    grafo = ef.cadeia_audio(config, plano, 20.0)
    assert "atrim=duration=20.000" in grafo
    assert "afade=t=in:d=0.60" in grafo and "afade=t=out:st=19.400:d=0.60" in grafo


def test_pad_ausente_falha_apontando_o_arquivo(template, tmp_path):
    config = _com(template, pad={"ativo": True})
    config["sfx"]["diretorio"] = str(tmp_path)
    with pytest.raises(ef.ErroEfeitos, match="pad_parkour.wav"):
        ef.plano_pad(config, "parkour")
    with pytest.raises(ef.ErroEfeitos, match="pad_full.wav"):
        ef.verificar_pads(config, {"full"})


def test_pad_desligado_nao_exige_arquivo(template):
    assert ef.plano_pad(template, "qualquer") == []
    ef.verificar_pads(template, {"qualquer"})


def test_comando_poe_stream_loop_no_pad_e_mapeia_aout(template, tmp_path):
    config = _com(template, voz={"ativo": True})
    sons = SONS[:1] + [{"tipo": "pad", "loop": True, "instante_s": 0.0,
                        "caminho": "/x/pad_slime.wav", "volume": 1.0}]
    args = render.comando(config, "/v.mp4", 0, 20.0, "a.ass", "/o.mp4",
                          sons=sons, filler=FILLER)
    i = args.index("/x/pad_slime.wav")
    assert args[i - 3:i - 1] == ["-stream_loop", "-1"]
    assert "[aout]" in args
    # o filler continua sendo a ÚLTIMA entrada, com o índice deslocado pelo pad.
    grafo = args[args.index("-filter_complex") + 1]
    assert "[3:v]fps=30" in grafo


# --- loudnorm em duas passadas -------------------------------------------------

SAIDA_LOUDNORM = """
[Parsed_loudnorm_0 @ 0x1] 
{
	"input_i" : "-23.41",
	"input_tp" : "-6.20",
	"input_lra" : "5.10",
	"input_thresh" : "-33.80",
	"output_i" : "-16.90",
	"target_offset" : "0.90"
}
"""


def test_parse_loudnorm_le_o_json_do_stderr():
    m = ef.parse_loudnorm("ruido\n" + SAIDA_LOUDNORM)
    assert m == {"I": -23.41, "TP": -6.2, "LRA": 5.1, "thresh": -33.8,
                 "offset": 0.9}
    assert ef.parse_loudnorm("sem json") is None


def test_medir_voz_chama_so_o_audio_e_devolve_a_medida(template):
    config = _com(template, voz={"ativo": True})
    chamadas = []

    class R:
        stderr = SAIDA_LOUDNORM

    def falso(args, **kw):
        chamadas.append(args)
        return R()

    m = ef.medir_voz(config, "/v.mp4", 75.0, 30.0, executar=falso)
    assert m["I"] == -23.41
    args = chamadas[0]
    assert "-vn" in args and "print_format=json" in " ".join(args)
    assert args[args.index("-ss") + 1] == "75.000"


def test_medir_voz_desligada_nao_roda_nada(template):
    def falso(*a, **k):
        raise AssertionError("não devia rodar")
    assert ef.medir_voz(template, "/v.mp4", 0, 10, executar=falso) is None


def test_segunda_passada_aplica_ganho_linear(template):
    config = _com(template, voz={"ativo": True})
    medida = {"I": -23.4, "TP": -6.2, "LRA": 5.1, "thresh": -33.8, "offset": 0.9}
    grafo = ef.cadeia_audio(config, [], 20.0, voz_medida=medida)
    assert "measured_I=-23.4:measured_TP=-6.2:measured_LRA=5.1" in grafo
    assert "measured_thresh=-33.8:offset=0.9:linear=true" in grafo
    # sem medida continua a passada única
    assert "measured_I" not in ef.cadeia_audio(config, [], 20.0)
