"""Montagem do comando do ffmpeg. Nenhum teste executa o binário."""
import os

import pytest

from editing import render


# --- nome de arquivo ----------------------------------------------------------

def test_nome_base_neutraliza_caracteres_de_filtergraph():
    # O nome entra CRU no filtergraph; ':' e '\' têm significado lá dentro.
    assert render.nome_base("a:b\\c", 7) == "a_b_c_7"
    assert render.nome_base("vid1", 42) == "vid1_42"


# --- reframe ------------------------------------------------------------------

def test_corte_limita_pelos_dois_lados(template):
    grafo = render.filtro_reframe(template)
    # Limitar só pela altura distorceria um vídeo que já viesse mais alto que
    # 9:16 — um Short republicado, por exemplo.
    assert "min(iw,ih*" in grafo
    assert "min(ih,iw/" in grafo
    assert grafo.endswith("[vr];")


def test_corte_usa_a_ancora_do_template(template):
    config = dict(template)
    config["reframe"] = {**template["reframe"], "ancora_horizontal": 0.0}
    assert "(iw-ow)*0.0" in render.filtro_reframe(config)


def test_desfoque_monta_fundo_e_frente(template):
    config = dict(template)
    config["reframe"] = {**template["reframe"], "modo": "desfoque"}
    grafo = render.filtro_reframe(config)
    assert "split=2[bg][fg]" in grafo
    assert "gblur=sigma=" in grafo
    assert "overlay=(W-w)/2:(H-h)/2" in grafo
    assert grafo.endswith("[vr];")


def test_reframe_escreve_a_resolucao_de_saida(template):
    grafo = render.filtro_reframe(template)
    assert f"scale={template['saida']['largura']}:{template['saida']['altura']}" in grafo


# --- zoom ---------------------------------------------------------------------

def test_zoom_desligado_nao_entra_no_grafo(template):
    assert render.filtro_zoom(template, 45.0) == ""


def test_zoom_ligado_usa_zoompan(template):
    config = dict(template)
    config["zoom"] = {"ativo": True, "fator_final": 1.08}
    grafo = render.filtro_zoom(config, 45.0)
    assert grafo.startswith("[vr]zoompan=")
    assert "1.08" in grafo
    assert grafo.endswith("[vz];")


def test_zoom_com_duracao_zero_nao_divide_por_zero(template):
    config = dict(template)
    config["zoom"] = {"ativo": True, "fator_final": 1.08}
    assert render.filtro_zoom(config, 0.0) == ""


# --- grafo completo -----------------------------------------------------------

def test_grafo_termina_no_rotulo_mapeado(template):
    grafo = render.filtergraph(template, "clip.ass", 45.0)
    assert grafo.endswith("[vout]")
    assert "ass=clip.ass" in grafo


def test_grafo_encadeia_o_zoom_entre_reframe_e_legenda(template):
    config = dict(template)
    config["zoom"] = {"ativo": True, "fator_final": 1.05}
    grafo = render.filtergraph(config, "clip.ass", 45.0)
    assert "[vz]ass=clip.ass[vout]" in grafo


def test_sem_zoom_a_legenda_vem_direto_do_reframe(template):
    assert "[vr]ass=clip.ass[vout]" in render.filtergraph(template, "clip.ass", 45.0)


# --- comando ------------------------------------------------------------------

def test_comando_corta_antes_de_decodificar(template):
    cmd = render.comando(template, "/v/a.mp4", 100.0, 45.0, "c.ass", "/r/c.mp4")
    # -ss ANTES de -i: busca rápida. Com recodificação o corte sai exato.
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd[cmd.index("-ss") + 1] == "100.000"
    assert cmd[cmd.index("-t") + 1] == "45.000"


def test_comando_mapeia_o_video_filtrado_e_o_audio_opcional(template):
    cmd = render.comando(template, "/v/a.mp4", 0.0, 45.0, "c.ass", "/r/c.mp4")
    assert "[vout]" in cmd
    # O '?' torna o áudio opcional: vídeo mudo não pode derrubar o render.
    assert "0:a?" in cmd


def test_comando_usa_sempre_filter_complex(template):
    # Um caminho só, com saída rotulada, elimina a classe de bug em que mudar
    # o modo do template muda a forma do comando.
    for modo in ("corte", "desfoque"):
        config = dict(template)
        config["reframe"] = {**template["reframe"], "modo": modo}
        cmd = render.comando(config, "/v/a.mp4", 0, 45, "c.ass", "/r/c.mp4")
        assert "-filter_complex" in cmd and "-vf" not in cmd


def test_comando_leva_os_parametros_de_codec_do_template(template):
    cmd = render.comando(template, "/v/a.mp4", 0, 45, "c.ass", "/r/c.mp4")
    assert cmd[cmd.index("-c:v") + 1] == template["saida"]["codec_video"]
    assert cmd[cmd.index("-crf") + 1] == str(template["saida"]["crf"])
    assert cmd[cmd.index("-r") + 1] == str(template["saida"]["fps"])
    assert cmd[cmd.index("-b:a") + 1] == template["saida"]["bitrate_audio"]


def test_comando_move_o_indice_para_o_comeco(template):
    # Sem faststart, player e plataforma baixam o arquivo inteiro antes do
    # primeiro quadro.
    cmd = render.comando(template, "/v/a.mp4", 0, 45, "c.ass", "/r/c.mp4")
    assert cmd[cmd.index("-movflags") + 1] == "+faststart"


def test_saida_e_o_ultimo_argumento(template):
    cmd = render.comando(template, "/v/a.mp4", 0, 45, "c.ass", "/r/saida.mp4")
    assert cmd[-1] == "/r/saida.mp4"


# --- execução -----------------------------------------------------------------

def test_renderizar_escreve_o_ass_e_roda_no_diretorio_dele(
    tmp_path, template, executar_ok, ffmpeg_fake
):
    # cwd no diretório do .ass: o filtro `ass` interpretaria '\' e ':' de um
    # caminho absoluto do Windows como sintaxe de filtergraph.
    video = tmp_path / "fonte.mp4"
    video.write_text("video", encoding="utf-8")
    destino = tmp_path / "render"
    registro = []

    caminho = render.renderizar(
        template, str(video), 100.0, 145.0, "conteudo do ass", "vid1_7",
        destino_dir=str(destino), executar=executar_ok(registro),
        caminho_ffmpeg=ffmpeg_fake,
    )
    assert caminho.endswith("vid1_7.mp4")
    assert (destino / "vid1_7.ass").read_text(encoding="utf-8") == "conteudo do ass"
    assert registro[0]["cwd"] == str(destino)
    # E o filtergraph referencia o .ass pelo nome, sem caminho.
    assert "ass=vid1_7.ass" in registro[0]["comando"][
        registro[0]["comando"].index("-filter_complex") + 1
    ]


def test_renderizar_calcula_a_duracao_do_trecho(
    tmp_path, template, executar_ok, ffmpeg_fake
):
    video = tmp_path / "fonte.mp4"
    video.write_text("video", encoding="utf-8")
    registro = []

    render.renderizar(
        template, str(video), 100.0, 145.0, "ass", "c",
        destino_dir=str(tmp_path / "r"), executar=executar_ok(registro),
        caminho_ffmpeg=ffmpeg_fake,
    )
    cmd = registro[0]["comando"]
    assert cmd[cmd.index("-t") + 1] == "45.000"


def test_video_fonte_ausente_falha_antes_do_ffmpeg(tmp_path, template, ffmpeg_fake):
    def explode(*a, **k):
        raise AssertionError("não deveria chamar o ffmpeg")

    with pytest.raises(render.ErroRender, match="não encontrado"):
        render.renderizar(
            template, str(tmp_path / "sumiu.mp4"), 0, 45, "ass", "c",
            destino_dir=str(tmp_path), executar=explode, caminho_ffmpeg=ffmpeg_fake,
        )


# --- efeitos sonoros ----------------------------------------------------------

SONS = [
    {"instante_s": 0.0, "caminho": "/sfx/whoosh.wav", "volume": 0.7},
    {"instante_s": 12.5, "caminho": "/sfx/ding.wav", "volume": 0.5},
]


def test_sem_sons_a_cadeia_de_audio_e_vazia():
    assert render.cadeia_audio([]) == ""
    assert render.cadeia_audio(None) == ""


def test_cada_som_vira_uma_entrada_atrasada():
    cadeia = render.cadeia_audio(SONS)
    assert "[1:a]adelay=0:all=1,volume=0.7[sfx0]" in cadeia
    assert "[2:a]adelay=12500:all=1,volume=0.5[sfx1]" in cadeia


def test_amix_nao_normaliza():
    # Sem normalize=0 o amix divide o volume pelo número de entradas, e a fala
    # afunda um pouco a cada efeito acrescentado.
    cadeia = render.cadeia_audio(SONS)
    assert "normalize=0" in cadeia
    assert "amix=inputs=3" in cadeia          # original + 2 efeitos
    assert cadeia.endswith("[aout]")


def test_atraso_vale_para_todos_os_canais():
    # Sem all=1, um efeito estéreo sai com um canal adiantado.
    assert ":all=1" in render.cadeia_audio(SONS)


def test_grafo_ganha_a_cadeia_de_audio(template):
    grafo = render.filtergraph(template, "c.ass", 45.0, sons=SONS)
    assert "[vout]" in grafo and "[aout]" in grafo


def test_sons_viram_entradas_depois_do_video(template):
    cmd = render.comando(template, "/v/a.mp4", 100.0, 45.0, "c.ass", "/r/c.mp4",
                         sons=SONS)
    entradas = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
    assert entradas == ["/v/a.mp4", "/sfx/whoosh.wav", "/sfx/ding.wav"]


def test_corte_nao_afeta_as_entradas_de_efeito(template):
    # -ss e -t valem só para a entrada seguinte; se valessem para todas, os
    # efeitos seriam cortados junto e sumiriam.
    cmd = render.comando(template, "/v/a.mp4", 100.0, 45.0, "c.ass", "/r/c.mp4",
                         sons=SONS)
    assert cmd.index("-ss") < cmd.index("-i")
    assert cmd.index("-t") < cmd.index("-i")
    # Não há um segundo -ss antes das outras entradas.
    assert cmd.count("-ss") == 1


def test_com_efeito_o_audio_vem_do_amix(template):
    cmd = render.comando(template, "/v/a.mp4", 0, 45, "c.ass", "/r/c.mp4",
                         sons=SONS)
    assert "[aout]" in cmd
    assert "0:a?" not in cmd


def test_sem_efeito_o_audio_continua_opcional(template):
    cmd = render.comando(template, "/v/a.mp4", 0, 45, "c.ass", "/r/c.mp4")
    assert "0:a?" in cmd
    assert "[aout]" not in cmd


def test_renderizar_repassa_os_sons(tmp_path, template, executar_ok, ffmpeg_fake):
    video = tmp_path / "fonte.mp4"
    video.write_text("video", encoding="utf-8")
    registro = []

    render.renderizar(
        template, str(video), 0, 45, "ass", "c", destino_dir=str(tmp_path / "r"),
        executar=executar_ok(registro), caminho_ffmpeg=ffmpeg_fake, sons=SONS,
    )
    assert "/sfx/ding.wav" in registro[0]["comando"]


def test_falha_do_ffmpeg_reporta_a_ultima_linha(
    tmp_path, template, executar_ok, ffmpeg_fake
):
    video = tmp_path / "fonte.mp4"
    video.write_text("video", encoding="utf-8")

    with pytest.raises(render.ErroRender, match="No such filter"):
        render.renderizar(
            template, str(video), 0, 45, "ass", "c",
            destino_dir=str(tmp_path / "r"),
            executar=executar_ok(returncode=1,
                                 stderr="ruído\nErro: No such filter: 'gblur'"),
            caminho_ffmpeg=ffmpeg_fake,
        )
