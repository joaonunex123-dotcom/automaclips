"""Planejamento dos efeitos sonoros. Nenhum arquivo de áudio é aberto."""
import copy
import os

import pytest

from editing import sfx


@pytest.fixture
def config(template):
    """Template com SFX ligado e os três eventos do padrão."""
    cfg = copy.deepcopy(template)
    cfg["sfx"]["ativo"] = True
    return cfg


@pytest.fixture
def biblioteca():
    return {"whoosh": "/sfx/whoosh.wav", "ding": "/sfx/ding.wav",
            "pop": "/sfx/pop.wav"}


def _planejar(config, biblioteca, duracao=45.0, picos=None, palavras=None):
    return sfx.planejar(config, duracao, picos=picos, palavras=palavras,
                        biblioteca=biblioteca)


def _palavras(*pares):
    return [{"inicio": t, "fim": t + 0.4, "palavra": p} for t, p in pares]


# --- biblioteca ---------------------------------------------------------------

def test_desligado_nao_carrega_nada(template):
    assert sfx.carregar_biblioteca(template) == {}


def test_carrega_os_arquivos_declarados(config, tmp_path):
    for nome in ("whoosh.wav", "ding.wav", "pop.wav"):
        (tmp_path / nome).write_text("audio", encoding="utf-8")
    config["sfx"]["diretorio"] = str(tmp_path)

    biblioteca = sfx.carregar_biblioteca(config)
    assert set(biblioteca) == {"whoosh", "ding", "pop"}
    assert biblioteca["ding"] == os.path.join(str(tmp_path), "ding.wav")


def test_arquivo_faltando_falha_apontando_qual(config, tmp_path):
    # Pular o efeito em silêncio produziria um defeito que só aparece
    # assistindo, muito depois de a fila inteira ter rodado.
    (tmp_path / "whoosh.wav").write_text("audio", encoding="utf-8")
    config["sfx"]["diretorio"] = str(tmp_path)

    with pytest.raises(sfx.ErroSFX, match="ding.wav"):
        sfx.carregar_biblioteca(config)


def test_evento_desligado_nao_exige_arquivo(config, tmp_path):
    (tmp_path / "whoosh.wav").write_text("audio", encoding="utf-8")
    config["sfx"]["diretorio"] = str(tmp_path)
    config["sfx"]["eventos"]["ding"]["ativo"] = False
    config["sfx"]["eventos"]["pop"]["ativo"] = False

    assert set(sfx.carregar_biblioteca(config)) == {"whoosh"}


def test_diretorio_relativo_parte_da_raiz_do_repo(config, tmp_path):
    config["sfx"]["diretorio"] = "assets/sfx"
    with pytest.raises(sfx.ErroSFX, match="assets"):
        sfx.carregar_biblioteca(config, base_dir=str(tmp_path))


# --- gatilho: transição -------------------------------------------------------

def test_abertura_e_fim_do_hook(config):
    config["hook"]["ativo"] = True
    config["hook"]["duracao_s"] = 1.0
    assert sfx.instantes_de_transicao(config, 45.0) == [0.0, 1.0]


def test_hook_desligado_deixa_so_a_abertura(config):
    config["hook"]["ativo"] = False
    assert sfx.instantes_de_transicao(config, 45.0) == [0.0]


def test_hook_maior_que_o_clip_nao_tem_virada(config):
    # Um hook tão longo quanto o clip não tem transição para marcar.
    config["hook"]["duracao_s"] = 60.0
    assert sfx.instantes_de_transicao(config, 45.0) == [0.0]


def test_abertura_desligada(config):
    config["sfx"]["na_abertura"] = False
    config["sfx"]["no_fim_do_hook"] = False
    assert sfx.instantes_de_transicao(config, 45.0) == []


# --- gatilho: palavra-chave ---------------------------------------------------

def test_palavra_da_lista_dispara(config):
    config["sfx"]["palavras_chave"] = ["caramba"]
    palavras = _palavras((3.0, "olha"), (4.0, "caramba"), (5.0, "isso"))
    assert sfx.instantes_de_palavra_chave(config, palavras) == [4.0]


def test_acento_e_caixa_nao_impedem_o_casamento(config):
    # Sem normalizar, "inacreditável" no template não casaria com
    # "Inacreditavel" transcrito, e a lista viraria loteria de grafia.
    config["sfx"]["palavras_chave"] = ["inacreditável"]
    palavras = _palavras((2.0, "Inacreditavel!"))
    config["sfx"]["exclamacao_conta"] = False
    assert sfx.instantes_de_palavra_chave(config, palavras) == [2.0]


def test_pontuacao_colada_na_palavra_nao_atrapalha(config):
    config["sfx"]["palavras_chave"] = ["nossa"]
    config["sfx"]["exclamacao_conta"] = False
    assert sfx.instantes_de_palavra_chave(config, _palavras((1.0, "nossa,"))) == [1.0]


def test_expressao_de_duas_palavras(config):
    config["sfx"]["palavras_chave"] = ["meu deus"]
    config["sfx"]["exclamacao_conta"] = False
    palavras = _palavras((1.0, "meu"), (1.5, "deus"), (2.0, "do"))
    assert sfx.instantes_de_palavra_chave(config, palavras) == [1.0]


def test_exclamacao_dispara_quando_configurado(config):
    config["sfx"]["palavras_chave"] = []
    config["sfx"]["exclamacao_conta"] = True
    assert sfx.instantes_de_palavra_chave(config, _palavras((7.0, "inacreditável!"))) == [7.0]


def test_exclamacao_desligada(config):
    config["sfx"]["palavras_chave"] = []
    config["sfx"]["exclamacao_conta"] = False
    assert sfx.instantes_de_palavra_chave(config, _palavras((7.0, "olha!"))) == []


def test_uma_palavra_dispara_uma_vez_so(config):
    # Sem o break, uma palavra que casa como isolada E como início de
    # expressão marcaria o mesmo instante duas vezes.
    config["sfx"]["palavras_chave"] = ["meu", "meu deus"]
    config["sfx"]["exclamacao_conta"] = False
    palavras = _palavras((1.0, "meu"), (1.5, "deus"))
    assert sfx.instantes_de_palavra_chave(config, palavras) == [1.0]


# --- plano --------------------------------------------------------------------

def test_desligado_nao_planeja_nada(template, biblioteca):
    assert _planejar(template, biblioteca, picos=[5.0, 10.0]) == []


def test_biblioteca_vazia_nao_planeja_nada(config):
    assert _planejar(config, {}, picos=[5.0, 10.0]) == []


def test_cada_gatilho_usa_o_som_declarado(config, biblioteca):
    config["sfx"]["espacamento_minimo_s"] = 0.0
    config["sfx"]["palavras_chave"] = ["caramba"]
    config["sfx"]["exclamacao_conta"] = False

    plano = _planejar(
        config, biblioteca, picos=[20.0],
        palavras=_palavras((30.0, "caramba")),
    )
    por_instante = {p["instante_s"]: p for p in plano}
    assert por_instante[0.0]["caminho"] == "/sfx/whoosh.wav"
    assert por_instante[20.0]["caminho"] == "/sfx/ding.wav"
    assert por_instante[30.0]["caminho"] == "/sfx/pop.wav"


def test_plano_sai_ordenado_no_tempo(config, biblioteca):
    config["sfx"]["espacamento_minimo_s"] = 0.0
    plano = _planejar(config, biblioteca, picos=[30.0, 10.0, 20.0])
    assert [p["instante_s"] for p in plano] == sorted(
        p["instante_s"] for p in plano
    )


def test_espacamento_minimo_evita_metralhadora(config, biblioteca):
    # Uma gargalhada produz vários picos seguidos; sem espaçamento o clip sai
    # com seis efeitos em cima de três segundos de áudio.
    config["sfx"]["espacamento_minimo_s"] = 1.5
    config["sfx"]["na_abertura"] = False
    config["sfx"]["no_fim_do_hook"] = False

    plano = _planejar(config, biblioteca, picos=[10.0, 10.3, 10.6, 11.0, 20.0])
    assert [p["instante_s"] for p in plano] == [10.0, 20.0]


def test_transicao_ganha_do_pico_na_disputa(config, biblioteca):
    # A transição é estrutural: sem ela o clip começa seco. Pico e palavra são
    # realce, e perder um não custa nada.
    config["sfx"]["espacamento_minimo_s"] = 2.0
    config["sfx"]["no_fim_do_hook"] = False

    plano = _planejar(config, biblioteca, picos=[0.5])
    assert len(plano) == 1
    assert plano[0]["gatilho"] == "transicao"


def test_teto_por_clip(config, biblioteca):
    config["sfx"]["espacamento_minimo_s"] = 0.0
    config["sfx"]["maximo_por_clip"] = 3
    plano = _planejar(config, biblioteca, picos=[float(t) for t in range(5, 40)])
    assert len(plano) == 3


def test_teto_zero_nao_limita(config, biblioteca):
    config["sfx"]["espacamento_minimo_s"] = 0.0
    config["sfx"]["maximo_por_clip"] = 0
    plano = _planejar(config, biblioteca, picos=[5.0, 10.0, 15.0, 20.0])
    assert len(plano) >= 4


def test_efeito_fora_do_clip_e_descartado(config, biblioteca):
    # O pico vem relativo ao clip, mas um erro de deslocamento colocaria o som
    # depois do fim do vídeo — onde ele simplesmente não tocaria.
    config["sfx"]["espacamento_minimo_s"] = 0.0
    plano = _planejar(config, biblioteca, duracao=45.0, picos=[10.0, 90.0, -3.0])
    assert [p["instante_s"] for p in plano if p["gatilho"] == "pico"] == [10.0]


def test_volume_do_evento_ganha_do_geral(config, biblioteca):
    config["sfx"]["volume"] = 0.6
    config["sfx"]["eventos"]["ding"]["volume"] = 0.2
    config["sfx"]["na_abertura"] = False
    config["sfx"]["no_fim_do_hook"] = False

    plano = _planejar(config, biblioteca, picos=[10.0])
    assert plano[0]["volume"] == pytest.approx(0.2)


def test_duracao_zero_nao_planeja(config, biblioteca):
    assert _planejar(config, biblioteca, duracao=0.0, picos=[1.0]) == []
