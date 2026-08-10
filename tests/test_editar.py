"""Orquestração da etapa 3: fila de render, retomada e isolamento de falha."""
import pytest

import settings
from db import repositorio
from editing import editar


@pytest.fixture
def clip_pronto(conn, video, tmp_path, transcricao):
    """Um vídeo baixado, transcrito e com um trecho selecionado."""
    fonte = tmp_path / "vid1.mp4"
    fonte.write_text("video", encoding="utf-8")
    transcricao_path = tmp_path / "vid1.json"

    def _fn(video_id="vid1", com_transcricao=True, com_video=True):
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=video_id), views=1, ganho=1, score=100.0,
            status=repositorio.STATUS_ANALISADO,
        )
        repositorio.registrar_midia(
            conn, fila_id,
            video_path=str(fonte) if com_video else "",
            audio_path=str(tmp_path / "vid1.wav"),
            transcricao_path=str(transcricao_path) if com_transcricao else "",
            duracao_real_s=600.0,
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 100.0, "fim_s": 145.0, "score_claude": 9.0,
            "motivo": "fecha na virada", "hook_text": "ele nunca contou isso",
            "picos_energia": 3, "score_final": 9.5,
            # Relativos ao início do trecho, como a etapa 2 grava.
            "picos_instantes": [12.0, 25.0, 38.0],
        }])
        return repositorio.clips_do_video(conn, fila_id)[0]["id"]

    _fn.transcricao = lambda: transcricao((100.0, 145.0, "a virada acontece aqui"))
    return _fn


@pytest.fixture
def render_falso():
    """Duplo de render.renderizar que registra os argumentos."""
    registro = []

    def renderizar(config, video_path, inicio, fim, ass_texto, nome,
                   destino_dir=None, sons=None):
        registro.append({
            "video_path": video_path, "inicio": inicio, "fim": fim,
            "ass": ass_texto, "nome": nome, "sons": sons or [],
        })
        return f"/render/{nome}.mp4"

    return registro, renderizar


# --- caminho feliz ------------------------------------------------------------

def test_renderiza_e_registra_o_artefato(conn, template, clip_pronto, render_falso):
    registro, renderizar = render_falso
    clip_id = clip_pronto()

    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )

    assert contagem == {"ok": 1, "falha": 0}
    linha = repositorio.render(conn, clip_id)
    assert linha["caminho"] == "/render/vid1_%d.mp4" % clip_id
    assert linha["duracao_s"] == pytest.approx(45.0)


def test_grava_a_versao_do_template(conn, template, clip_pronto, render_falso):
    # É o que permite saber, na etapa 7, se a diferença de performance entre
    # dois clips veio do trecho ou do visual.
    _, renderizar = render_falso
    clip_id = clip_pronto()

    editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert repositorio.render(conn, clip_id)["template_versao"] == template["versao"]


def test_o_ass_recebe_o_hook_e_a_legenda(conn, template, clip_pronto, render_falso):
    registro, renderizar = render_falso
    clip_pronto()

    editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    ass = registro[0]["ass"]
    assert "ELE NUNCA CONTOU ISSO" in ass          # hook
    assert "VIRADA" in ass                          # legenda
    assert registro[0]["inicio"] == pytest.approx(100.0)


def test_legenda_sai_sincronizada_com_o_clip(conn, template, clip_pronto,
                                             render_falso):
    # O trecho começa em 100 s no vídeo-fonte; no clip a primeira palavra tem
    # que cair perto do zero, não em 1:40.
    registro, renderizar = render_falso
    clip_pronto()

    editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    dialogos = [l for l in registro[0]["ass"].splitlines()
                if l.startswith("Dialogue:")]
    assert any(",0:00:00.00," in l for l in dialogos)
    assert not any("0:01:4" in l for l in dialogos)


# --- fila ---------------------------------------------------------------------

def test_clip_ja_renderizado_sai_da_fila(conn, template, clip_pronto, render_falso):
    _, renderizar = render_falso
    clip_id = clip_pronto()
    repositorio.registrar_render(conn, clip_id, "/render/ja.mp4")

    assert repositorio.clips_para_renderizar(conn) == []
    assert editar.renderizar_fila(conn, config=template, renderizar=renderizar) == {
        "ok": 0, "falha": 0
    }


def test_clip_descartado_nunca_entra_na_fila(conn, video):
    fila_id = repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO,
    )
    repositorio.registrar_clips(conn, fila_id, [{
        "inicio_s": 10.0, "fim_s": 55.0, "score_claude": 2.0, "score_final": 2.0,
        "status": repositorio.CLIP_DESCARTADO, "motivo_descarte": "score baixo",
    }])
    assert repositorio.clips_para_renderizar(conn) == []


def test_fila_ordena_pelo_score_final(conn, video):
    fila_id = repositorio.registrar_observacao(
        conn, video(), views=1, ganho=1, score=1.0,
        status=repositorio.STATUS_ANALISADO,
    )
    repositorio.registrar_clips(conn, fila_id, [
        {"inicio_s": 10.0, "fim_s": 55.0, "score_claude": 5.0, "score_final": 5.0},
        {"inicio_s": 200.0, "fim_s": 245.0, "score_claude": 9.0, "score_final": 9.0},
    ])
    fila = repositorio.clips_para_renderizar(conn)
    assert [c["score_final"] for c in fila] == [9.0, 5.0]


def test_limite_por_execucao(conn, template, clip_pronto, render_falso, monkeypatch):
    _, renderizar = render_falso
    clip_pronto(video_id="a")
    clip_pronto(video_id="b")
    monkeypatch.setattr(settings, "EDITING_MAX_CLIPS", 1)

    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert contagem["ok"] == 1


# --- fontes ausentes ----------------------------------------------------------

def test_sem_video_baixado_vira_falha(conn, template, clip_pronto, render_falso):
    _, renderizar = render_falso
    clip_pronto(com_video=False)

    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert contagem == {"ok": 0, "falha": 1}


def test_sem_transcricao_renderiza_sem_legenda(conn, template, clip_pronto,
                                               render_falso, caplog):
    # Melhor um clip sem legenda do que nenhum clip.
    registro, renderizar = render_falso
    clip_pronto(com_transcricao=False)

    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar
    )
    assert contagem == {"ok": 1, "falha": 0}
    ass = registro[0]["ass"]
    assert "ELE NUNCA CONTOU ISSO" in ass            # o hook continua
    assert "sem transcrição" in caplog.text


def test_falha_num_clip_nao_derruba_os_outros(conn, template, clip_pronto):
    clip_pronto(video_id="quebrado")
    clip_pronto(video_id="bom")
    tentativas = []

    def renderizar(config, video_path, inicio, fim, ass_texto, nome,
                   destino_dir=None, sons=None):
        tentativas.append(nome)
        if nome.startswith("quebrado"):
            raise RuntimeError("codec indisponível")
        return f"/render/{nome}.mp4"

    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert contagem == {"ok": 1, "falha": 1}
    assert len(tentativas) == 2


# --- efeitos sonoros ----------------------------------------------------------

def test_sfx_desligado_nao_manda_som(conn, template, clip_pronto, render_falso):
    registro, renderizar = render_falso
    clip_pronto()

    editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert registro[0]["sons"] == []


def test_sfx_usa_os_picos_gravados_na_etapa_2(conn, template, clip_pronto,
                                              render_falso):
    import copy

    config = copy.deepcopy(template)
    config["sfx"].update(ativo=True, espacamento_minimo_s=0.0,
                         na_abertura=False, no_fim_do_hook=False)
    registro, renderizar = render_falso
    clip_pronto()

    editar.renderizar_fila(
        conn, config=config, renderizar=renderizar,
        biblioteca={"ding": "/sfx/ding.wav"},
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    instantes = [s["instante_s"] for s in registro[0]["sons"]]
    assert instantes == [12.0, 25.0, 38.0]


def test_sfx_le_as_palavras_da_mesma_extracao_da_legenda(conn, template,
                                                         clip_pronto, render_falso):
    # As palavras são extraídas uma vez e servem à legenda e ao SFX; recalcular
    # para o segundo uso convidaria os dois a divergirem.
    import copy

    config = copy.deepcopy(template)
    config["sfx"].update(
        ativo=True, espacamento_minimo_s=0.0, na_abertura=False,
        no_fim_do_hook=False, exclamacao_conta=False, palavras_chave=["virada"],
    )
    config["sfx"]["eventos"]["ding"]["ativo"] = False
    registro, renderizar = render_falso
    clip_pronto()

    editar.renderizar_fila(
        conn, config=config, renderizar=renderizar,
        biblioteca={"pop": "/sfx/pop.wav"},
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    assert [s["caminho"] for s in registro[0]["sons"]] == ["/sfx/pop.wav"]


def test_biblioteca_e_conferida_uma_vez_antes_do_primeiro_render(
    conn, template, clip_pronto, render_falso
):
    # Descobrir clip a clip produziria a fila inteira em 'falha' com a mesma
    # mensagem repetida.
    import copy

    config = copy.deepcopy(template)
    config["sfx"]["ativo"] = True
    config["sfx"]["diretorio"] = "/nao/existe"
    _, renderizar = render_falso
    clip_pronto()

    from editing import sfx as sfx_mod
    with pytest.raises(sfx_mod.ErroSFX):
        editar.renderizar_fila(conn, config=config, renderizar=renderizar)


# --- resumo -------------------------------------------------------------------

def test_resumo_avisa_do_modo_sombra(conn, template, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_PUBLISH", False)
    texto = editar._resumo(conn, {"ok": 0, "falha": 0}, template)
    assert "AUTO_PUBLISH=false" in texto
    assert "nada é publicado" in texto


def test_resumo_mostra_o_template_usado(conn, template, clip_pronto, render_falso):
    _, renderizar = render_falso
    clip_pronto()
    contagem = editar.renderizar_fila(
        conn, config=template, renderizar=renderizar,
        carregar_transcricao=lambda c: clip_pronto.transcricao(),
    )
    texto = editar._resumo(conn, contagem, template)
    assert "renderizados  1" in texto
    assert f"template versão {template['versao']}" in texto
