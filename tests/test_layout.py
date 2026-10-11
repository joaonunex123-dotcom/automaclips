"""Layouts A/B: variante estável, preenchimento, grafo split e comparação.

Nada aqui baixa mídia. Os vídeos são sintéticos (ffmpeg lavfi) e os testes que
precisam do binário pulam sem ele; o resto usa duplos.
"""
import copy
import logging
import os
import random
import shutil
import subprocess
from types import SimpleNamespace

import pytest

import settings
from analytics import variantes
from db import repositorio
from editing import editar, layout, render
from editing import template as template_mod

FFMPEG = shutil.which("ffmpeg")
precisa_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg ausente")


@pytest.fixture(autouse=True)
def _limpa_avisos():
    layout._AVISADAS.clear()


@pytest.fixture
def fillers(tmp_path):
    """fillers(categoria, *nomes) -> raiz com arquivos vazios (só nomes)."""
    raiz = tmp_path / "filler"

    def _fn(categoria="parkour", *nomes):
        pasta = raiz / categoria
        pasta.mkdir(parents=True, exist_ok=True)
        for nome in nomes or ("a.mp4",):
            (pasta / nome).write_bytes(b"x")
        return str(raiz)

    _fn.raiz = str(raiz)
    return _fn


@pytest.fixture
def ab(template, fillers):
    """Template com teste A/B full vs split:parkour e filler existente."""
    raiz = fillers("parkour", "a.mp4", "b.mov")
    config = copy.deepcopy(template)
    config["split"]["diretorio"] = raiz
    config["variantes"]["pesos"] = {"full": 1, "split:parkour": 1}
    return config


# --- template -----------------------------------------------------------------

def test_template_real_nasce_em_full_sem_teste_ab(template):
    assert template["layout"] == "full"
    assert template["variantes"]["pesos"] == {}
    assert layout.pesos(template) == {"full": 1.0}


def test_layout_invalido_falha_na_carga(template):
    config = copy.deepcopy(template)
    config["layout"] = "triplo"
    with pytest.raises(template_mod.TemplateInvalido, match="layout"):
        template_mod.validar(config)


def test_proporcao_fora_da_faixa_falha(template):
    config = copy.deepcopy(template)
    config["split"]["proporcao_clip"] = 0.95
    with pytest.raises(template_mod.TemplateInvalido, match="proporcao_clip"):
        template_mod.validar(config)


def test_variante_split_sem_categoria_falha(template):
    config = copy.deepcopy(template)
    config["variantes"]["pesos"] = {"split": 1}
    with pytest.raises(template_mod.TemplateInvalido, match="categoria"):
        template_mod.validar(config)


def test_peso_negativo_ou_todos_zero_falha(template):
    config = copy.deepcopy(template)
    config["variantes"]["pesos"] = {"full": -1}
    with pytest.raises(template_mod.TemplateInvalido):
        template_mod.validar(config)
    config["variantes"]["pesos"] = {"full": 0, "split:x": 0}
    with pytest.raises(template_mod.TemplateInvalido, match="zero"):
        template_mod.validar(config)


# --- atribuição ---------------------------------------------------------------

def test_escolha_e_estavel_por_clip():
    pesos = {"full": 1.0, "split:parkour": 1.0}
    primeira = [layout.escolher(i, pesos, "s") for i in range(50)]
    assert primeira == [layout.escolher(i, pesos, "s") for i in range(50)]


def test_escolha_nao_depende_da_ordem_do_json():
    a = {"full": 1.0, "split:parkour": 1.0}
    b = {"split:parkour": 1.0, "full": 1.0}
    assert all(layout.escolher(i, a, "s") == layout.escolher(i, b, "s")
               for i in range(100))


def test_escolha_respeita_os_pesos():
    pesos = {"full": 3.0, "split:parkour": 1.0}
    n = 4000
    full = sum(layout.escolher(i, pesos, "x") == "full" for i in range(n))
    assert 0.70 < full / n < 0.80


def test_semente_muda_a_atribuicao():
    pesos = {"full": 1.0, "split:parkour": 1.0}
    assert [layout.escolher(i, pesos, "a") for i in range(40)] != \
           [layout.escolher(i, pesos, "b") for i in range(40)]


def test_variante_sem_categoria_usa_a_do_template(template):
    config = copy.deepcopy(template)
    config["split"]["categoria"] = "parkour"
    config["layout"] = "split"
    assert layout.pesos(config) == {"split:parkour": 1.0}


# --- preenchimento ausente ----------------------------------------------------

def test_split_sem_arquivo_e_ignorado_e_logado(template, fillers, caplog):
    config = copy.deepcopy(template)
    config["split"]["diretorio"] = fillers.raiz       # nem existe
    config["variantes"]["pesos"] = {"full": 1, "split:parkour": 5}
    with caplog.at_level(logging.WARNING, logger="editing.layout"):
        disponiveis = layout.variantes_disponiveis(config)
    assert disponiveis == {"full": 1.0}
    assert "split:parkour" in caplog.text and "ignorada" in caplog.text


def test_so_split_configurado_e_pasta_vazia_cai_em_full(template, fillers):
    config = copy.deepcopy(template)
    config["split"]["diretorio"] = fillers.raiz
    config["layout"] = "split"
    config["split"]["categoria"] = "parkour"
    assert layout.variantes_disponiveis(config) == {"full": 1.0}


def test_pasta_com_arquivo_que_nao_e_video_nao_conta(template, fillers):
    raiz = fillers("parkour", "leia-me.txt")
    config = copy.deepcopy(template)
    config["split"]["diretorio"] = raiz
    assert layout.listar_fillers(config, "parkour") == []


def test_variantes_disponiveis_mantem_split_com_arquivo(ab):
    assert layout.variantes_disponiveis(ab) == {"full": 1.0, "split:parkour": 1.0}


# --- plano do preenchimento ---------------------------------------------------

def _executar_duracao(duracao):
    h, resto = divmod(duracao, 3600)
    m, s = divmod(resto, 60)

    def _fn(argumentos, **_):
        return SimpleNamespace(
            returncode=1,
            stderr=f"  Duration: {int(h):02d}:{int(m):02d}:{s:05.2f}, start: 0\n",
        )
    return _fn


def test_duracao_do_arquivo_le_o_stderr():
    assert layout.duracao_do_arquivo("x.mp4", executar=_executar_duracao(125.5)) \
        == pytest.approx(125.5)


def test_duracao_ilegivel_levanta():
    falso = lambda *a, **k: SimpleNamespace(returncode=1, stderr="invalid data")
    with pytest.raises(layout.ErroLayout):
        layout.duracao_do_arquivo("x.mp4", executar=falso)


def test_filler_longo_corta_trecho_aleatorio_dentro_do_arquivo(ab):
    for clip_id in range(20):
        plano = layout.planejar_filler(
            ab, "split:parkour", 40.0, clip_id=clip_id,
            executar=_executar_duracao(300.0),
        )
        assert plano["loop"] is False
        assert 0.0 <= plano["inicio_s"] <= 300.0 - 40.0
        assert os.path.basename(plano["caminho"]) in ("a.mp4", "b.mov")


def test_filler_curto_entra_em_loop_do_zero(ab):
    plano = layout.planejar_filler(
        ab, "split:parkour", 40.0, clip_id=1, executar=_executar_duracao(12.0),
    )
    assert plano == {"caminho": plano["caminho"], "inicio_s": 0.0, "loop": True}


def test_plano_e_reproduzivel_por_clip(ab):
    um = layout.planejar_filler(ab, "split:parkour", 40.0, clip_id=7,
                                executar=_executar_duracao(300.0))
    dois = layout.planejar_filler(ab, "split:parkour", 40.0, clip_id=7,
                                  executar=_executar_duracao(300.0))
    assert um == dois


def test_plano_varia_entre_clips(ab):
    planos = {
        layout.planejar_filler(ab, "split:parkour", 40.0, clip_id=i,
                               executar=_executar_duracao(300.0))["inicio_s"]
        for i in range(10)
    }
    assert len(planos) > 1


def test_full_nao_planeja_filler(ab):
    assert layout.filler_do_layout(ab, "full", 40.0) is None


# --- grafo --------------------------------------------------------------------

def _plano():
    return {"caminho": "/f/p.mp4", "inicio_s": 12.5, "loop": False}


def test_alturas_somam_a_saida(ab):
    clip, fill = layout.alturas(ab)
    assert clip + fill == 1920
    assert clip == 1152          # 60% de 1920


def test_grafo_split_empilha_clip_e_preenchimento(ab):
    grafo = render.filtergraph(ab, "c.ass", 45.0, filler=_plano())
    assert "crop=w=" in grafo and "scale=1080:1152" in grafo     # clip em cima
    assert "[1:v]" in grafo and "scale=1080:768" in grafo         # preenchimento
    assert "vstack=inputs=2" in grafo
    assert grafo.index("vstack") < grafo.index("ass=c.ass")       # legenda por cima
    assert grafo.endswith("[vout]")


def test_grafo_full_nao_muda(ab):
    sem = render.filtergraph(ab, "c.ass", 45.0)
    assert "vstack" not in sem and "[1:v]" not in sem


def test_separador_e_parametrizado(ab):
    ab["split"]["separador"] = {"ativo": True, "espessura": 10, "cor": "#FF0000"}
    grafo = render.filtergraph(ab, "c.ass", 45.0, filler=_plano())
    assert "drawbox=x=0:y=1147:w=iw:h=10" in grafo
    assert "0xFF0000@1.000" in grafo
    ab["split"]["separador"]["ativo"] = False
    assert "drawbox" not in render.filtergraph(ab, "c.ass", 45.0, filler=_plano())


def test_filler_e_a_ultima_entrada_e_nao_desloca_os_efeitos(ab):
    sons = [{"caminho": "/s/whoosh.wav", "instante_s": 1.0, "volume": 0.5}]
    argumentos = render.comando(ab, "/v/src.mp4", 10.0, 45.0, "c.ass", "/o.mp4",
                                sons=sons, filler=_plano())
    entradas = [argumentos[i + 1] for i, a in enumerate(argumentos) if a == "-i"]
    assert entradas == ["/v/src.mp4", "/s/whoosh.wav", "/f/p.mp4"]
    grafo = argumentos[argumentos.index("-filter_complex") + 1]
    assert "[1:a]adelay=" in grafo          # efeito continua na entrada 1
    assert "[2:v]" in grafo                 # preenchimento na entrada 2


def test_audio_nunca_vem_do_preenchimento(ab):
    argumentos = render.comando(ab, "/v/src.mp4", 10.0, 45.0, "c.ass", "/o.mp4",
                                filler=_plano())
    mapas = [argumentos[i + 1] for i, a in enumerate(argumentos) if a == "-map"]
    assert mapas == ["[vout]", "0:a?"]


def test_loop_e_trecho_viram_opcoes_de_entrada(ab):
    plano = {"caminho": "/f/curto.mp4", "inicio_s": 0.0, "loop": True}
    argumentos = render.comando(ab, "/v/src.mp4", 10.0, 45.0, "c.ass", "/o.mp4",
                                filler=plano)
    i = argumentos.index("/f/curto.mp4")
    assert argumentos[i - 7:i + 1] == [
        "-stream_loop", "-1", "-ss", "0.000", "-t", "45.000", "-i", "/f/curto.mp4",
    ]


def test_trecho_sem_loop_nao_tem_stream_loop(ab):
    argumentos = render.comando(ab, "/v/src.mp4", 10.0, 45.0, "c.ass", "/o.mp4",
                                filler=_plano())
    i = argumentos.index("/f/p.mp4")
    assert "-stream_loop" not in argumentos
    assert argumentos[i - 5:i + 1] == ["-ss", "12.500", "-t", "45.000", "-i", "/f/p.mp4"]


def test_legenda_do_split_usa_posicao_propria(ab):
    efetivo = layout.config_do_layout(ab, "split:parkour")
    assert efetivo["legenda"]["margem_vertical"] == ab["split"]["legenda"]["margem_vertical"]
    assert ab["legenda"]["margem_vertical"] == 420          # original intacto
    assert layout.config_do_layout(ab, "full") is ab


# --- orquestração -------------------------------------------------------------

@pytest.fixture
def clip_pronto(conn, video, tmp_path, transcricao):
    fonte = tmp_path / "vid.mp4"
    fonte.write_text("video", encoding="utf-8")
    contador = {"n": 0}

    def _fn():
        contador["n"] += 1
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=f"vid{contador['n']}"), views=1, ganho=1,
            score=100.0, status=repositorio.STATUS_ANALISADO,
        )
        repositorio.registrar_midia(
            conn, fila_id, video_path=str(fonte), audio_path="",
            transcricao_path="", duracao_real_s=600.0,
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 100.0, "fim_s": 145.0, "score_claude": 9.0,
            "motivo": "m", "hook_text": "h", "picos_energia": 0,
            "score_final": 9.0,
        }])
        return repositorio.clips_do_video(conn, fila_id)[0]["id"]

    return _fn


@pytest.fixture
def renders_falsos():
    registro = []

    def renderizar(config, video_path, inicio, fim, ass_texto, nome,
                   destino_dir=None, sons=None, filler=None):
        registro.append({"nome": nome, "filler": filler, "config": config})
        return f"/render/{nome}.mp4"

    return registro, renderizar


def _plano_falso(config, variante, duracao, **_):
    return {"caminho": "/f/x.mp4", "inicio_s": 1.0, "loop": False}


def _linha(conn, clip_id):
    return next(c for c in repositorio.clips_para_renderizar(conn)
                if c["id"] == clip_id)


def test_variante_e_gravada_em_renders(conn, ab, clip_pronto, renders_falsos):
    registro, renderizar = renders_falsos
    clip = clip_pronto()
    editar.renderizar_clip(
        conn, _linha(conn, clip), ab, biblioteca={}, renderizar=renderizar,
        carregar_transcricao=lambda _: {"segmentos": []},
        planejar_filler=_plano_falso,
    )
    gravada = repositorio.render(conn, clip)["variante"]
    assert gravada == layout.escolher(clip, layout.pesos(ab), "ab1")
    # Só o split recebe preenchimento.
    assert (registro[0]["filler"] is not None) == gravada.startswith("split")


def test_tela_cheia_nao_recebe_argumento_filler(conn, template, clip_pronto):
    # Compatível com o renderizador de antes do A/B: sem `filler` na assinatura.
    chamadas = []

    def antigo(config, video_path, inicio, fim, ass_texto, nome,
               destino_dir=None, sons=None):
        chamadas.append(nome)
        return "/r.mp4"

    clip = clip_pronto()
    editar.renderizar_clip(
        conn, _linha(conn, clip), template, biblioteca={}, renderizar=antigo,
        carregar_transcricao=lambda _: {"segmentos": []},
    )
    assert chamadas and repositorio.render(conn, clip)["variante"] == "full"


def test_mesmo_clip_mantem_a_variante_mesmo_mudando_os_pesos(
        conn, ab, clip_pronto, renders_falsos):
    registro, renderizar = renders_falsos
    clip = clip_pronto()
    kwargs = dict(biblioteca={}, renderizar=renderizar,
                  carregar_transcricao=lambda _: {"segmentos": []},
                  planejar_filler=_plano_falso)
    editar.renderizar_clip(conn, _linha(conn, clip), ab, **kwargs)
    primeira = repositorio.render(conn, clip)["variante"]

    # Inverte os pesos e a semente e refaz o render (apaga a linha, como o
    # README manda): o clip não pode trocar de layout.
    conn.execute("DELETE FROM resultados WHERE clip_id = ?", (clip,))
    novo = copy.deepcopy(ab)
    novo["variantes"] = {"pesos": {"full": 0.0001, "split:parkour": 1.0}
                         if primeira == "full" else
                         {"full": 1.0, "split:parkour": 0.0001},
                         "semente": "outra"}
    repositorio.registrar_render(conn, clip, "/x.mp4", variante=primeira)
    assert layout.variante_do_clip(conn, clip, novo) == primeira


def test_variante_publicada_sobrevive_a_render_apagado(conn, ab, clip_pronto):
    clip = clip_pronto()
    conn.execute(
        "INSERT INTO publicacoes (clip_id, plataforma, agendado_para)"
        " VALUES (?, 'youtube', '2026-08-10 12:00:00')", (clip,))
    pub = conn.execute("SELECT id FROM publicacoes").fetchone()[0]
    conn.execute(
        "INSERT INTO resultados (publicacao_id, clip_id, plataforma, variante)"
        " VALUES (?, ?, 'youtube', 'split:parkour')", (pub, clip))
    assert repositorio.render(conn, clip) is None
    assert layout.variante_do_clip(conn, clip, ab) == "split:parkour"


def test_variante_gravada_sem_filler_agora_cai_em_full(conn, template, clip_pronto):
    clip = clip_pronto()
    repositorio.registrar_render(conn, clip, "/x.mp4", variante="split:parkour")
    # `template` real: pesos vazios, nenhum filler.
    assert layout.variante_do_clip(conn, clip, template) == "full"


# --- banco --------------------------------------------------------------------

def test_coluna_variante_e_acrescentada_a_banco_antigo(tmp_path, monkeypatch):
    import sqlite3

    caminho = tmp_path / "antigo.db"
    antigo = sqlite3.connect(caminho)
    antigo.execute("CREATE TABLE renders (clip_id INTEGER PRIMARY KEY,"
                   " caminho TEXT NOT NULL)")
    antigo.execute("CREATE TABLE resultados (id INTEGER PRIMARY KEY,"
                   " clip_id INTEGER)")
    antigo.execute("INSERT INTO renders VALUES (1, '/a.mp4')")
    antigo.commit()
    antigo.close()

    conn = sqlite3.connect(caminho)
    conn.row_factory = sqlite3.Row
    acrescentadas = repositorio.aplicar_colunas_novas(conn)
    assert "renders.variante" in acrescentadas
    assert "resultados.variante" in acrescentadas
    # O histórico existente é, de fato, tela cheia.
    assert conn.execute("SELECT variante FROM renders").fetchone()[0] == "full"


def test_resultado_grava_a_variante_do_render(conn, ab, clip_pronto):
    clip = clip_pronto()
    repositorio.registrar_render(conn, clip, "/x.mp4", variante="split:parkour")
    pub = repositorio.agendar_publicacao(conn, clip, "youtube", "2026-08-10 12:00:00")
    conn.execute(
        "UPDATE publicacoes SET status = ?, publicado_em = ?, id_externo = 'e'"
        " WHERE id = ?", (repositorio.PUB_PUBLICADO, "2026-08-01 12:00:00", pub))
    linha = repositorio.publicacoes_para_medir(conn, agora="2026-08-10 12:00:00")[0]
    assert linha["variante"] == "split:parkour"
    repositorio.registrar_resultado(conn, linha, {"views": 10}, horas_publicado=48)
    assert conn.execute("SELECT variante FROM resultados").fetchone()[0] \
        == "split:parkour"


# --- analytics ----------------------------------------------------------------

@pytest.fixture
def medir_variante(conn, video):
    contador = {"n": 0}

    def _fn(variante, views, plataforma="youtube", horas=48.0):
        contador["n"] += 1
        n = contador["n"]
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=f"a{n}"), views=1, ganho=1, score=1.0,
            status=repositorio.STATUS_ANALISADO,
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 1.0, "fim_s": 40.0, "score_claude": 8.0,
            "score_final": 8.0,
        }])
        clip = repositorio.clips_do_video(conn, fila_id)[0]["id"]
        repositorio.registrar_render(conn, clip, f"/{n}.mp4", variante=variante)
        pub = repositorio.agendar_publicacao(conn, clip, plataforma,
                                             "2026-08-10 12:00:00")
        conn.execute(
            "UPDATE publicacoes SET status = ?, publicado_em = ?, id_externo = ?"
            " WHERE id = ?",
            (repositorio.PUB_PUBLICADO, "2026-08-08 12:00:00", f"e{n}", pub))
        linha = repositorio.publicacoes_para_medir(
            conn, agora="2026-08-10 12:00:00")
        linha = next(l for l in linha if l["id"] == pub)
        repositorio.registrar_resultado(conn, linha, {"views": views},
                                        horas_publicado=horas)

    return _fn


def test_comparacao_avisa_dado_insuficiente_abaixo_de_15(conn, medir_variante):
    for i in range(14):
        medir_variante("full", 1000 + i)
    for i in range(20):
        medir_variante("split:parkour", 2000 + i)

    comparacao = {v["variante"]: v for v in variantes.comparar(conn)}
    assert comparacao["full"]["n"] == 14 and not comparacao["full"]["suficiente"]
    assert comparacao["split:parkour"]["n"] == 20
    assert comparacao["split:parkour"]["suficiente"]

    texto = "\n".join(variantes.formatar(variantes.comparar(conn)))
    assert "dado insuficiente" in texto
    assert "n=14" in texto and "n=20" in texto
    assert "sem veredito" in texto


def test_com_15_ou_mais_em_todas_aponta_a_melhor(conn, medir_variante):
    for i in range(15):
        medir_variante("full", 1000)
        medir_variante("split:parkour", 3000)
    comparacao = variantes.comparar(conn)
    assert comparacao[0]["variante"] == "split:parkour"
    assert all(v["suficiente"] for v in comparacao)
    texto = "\n".join(variantes.formatar(comparacao))
    assert "dado insuficiente" not in texto
    assert "à frente: split:parkour" in texto


def test_comparacao_e_normalizada_por_plataforma(conn, medir_variante):
    # 'full' só no YouTube (escala alta), 'split' só no Instagram (escala
    # baixa), cada um IGUAL à mediana da sua plataforma: empate, não vitória
    # do YouTube.
    for _ in range(15):
        medir_variante("full", 100000, plataforma="youtube")
        medir_variante("split:parkour", 100, plataforma="instagram")
    comparacao = {v["variante"]: v for v in variantes.comparar(conn)}
    assert comparacao["full"]["mediana"] == pytest.approx(1.0)
    assert comparacao["split:parkour"]["mediana"] == pytest.approx(1.0)


def test_sem_medicao_nao_quebra(conn):
    assert variantes.comparar(conn) == []
    assert "nenhum post medido" in "\n".join(variantes.formatar([]))


# --- render real com vídeos sintéticos ----------------------------------------

def _gerar(caminho, filtro, duracao, audio=False, ffmpeg=FFMPEG):
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", filtro]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duracao}"]
    cmd += ["-t", str(duracao), "-pix_fmt", "yuv420p", str(caminho)]
    subprocess.run(cmd, check=True)


def _info(caminho):
    saida = subprocess.run([FFMPEG, "-hide_banner", "-i", str(caminho)],
                           capture_output=True, text=True).stderr
    return saida


@precisa_ffmpeg
@pytest.mark.parametrize("filler_s", [20.0, 2.0], ids=["trecho", "loop"])
def test_render_split_sintetico(tmp_path, ab, filler_s):
    fonte = tmp_path / "fonte.mp4"
    _gerar(fonte, "testsrc2=size=1280x720:rate=30", 6, audio=True)
    pasta = tmp_path / "filler" / "parkour"
    shutil.rmtree(pasta)            # a fixture `ab` deixa arquivos-fantasma
    pasta.mkdir(parents=True)
    _gerar(pasta / "f.mp4", "mandelbrot=size=640x360:rate=24", filler_s)

    config = copy.deepcopy(ab)
    config["split"]["diretorio"] = str(tmp_path / "filler")
    config["saida"]["preset"] = "ultrafast"
    config["legenda"]["ativa"] = False
    config["hook"]["ativo"] = False

    plano = layout.planejar_filler(config, "split:parkour", 3.0, clip_id=3,
                                   ffmpeg=FFMPEG)
    assert plano["loop"] is (filler_s <= 3.0)

    saida = render.renderizar(
        layout.config_do_layout(config, "split:parkour"), str(fonte), 1.0, 4.0,
        "[Script Info]\nScriptType: v4.00+\n", "split_teste",
        destino_dir=str(tmp_path / "out"), caminho_ffmpeg=FFMPEG, filler=plano,
    )
    info = _info(saida)
    assert "1080x1920" in info
    assert info.count("Audio:") == 1       # só o do clip
    assert "Video:" in info


@precisa_ffmpeg
def test_render_full_sintetico_nao_regride(tmp_path, template):
    fonte = tmp_path / "fonte.mp4"
    _gerar(fonte, "testsrc2=size=1280x720:rate=30", 4, audio=True)
    config = copy.deepcopy(template)
    config["saida"]["preset"] = "ultrafast"
    saida = render.renderizar(
        config, str(fonte), 0.5, 3.5, "[Script Info]\nScriptType: v4.00+\n",
        "full_teste", destino_dir=str(tmp_path / "out"), caminho_ffmpeg=FFMPEG,
    )
    assert "1080x1920" in _info(saida)
