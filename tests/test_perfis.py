"""Perfis: um canal de destino por processo, e nenhum vazando no outro.

Nenhum subprocesso é criado aqui: `executar` é injetável, e o que os testes
afirmam é o comando e o AMBIENTE que sairiam — que é onde mora o risco de
publicar o clip de um canal na conta do outro.
"""
import importlib
import logging
import os

import pytest

import settings
from orchestrator import perfis


@pytest.fixture
def raiz(tmp_path):
    """Uma raiz de projeto com .env de perfis dentro."""
    def _fn(*nomes):
        for nome in nomes:
            (tmp_path / nome).write_text("", encoding="utf-8")
        return str(tmp_path)
    return _fn


class ExecutarFalso:
    """Duplo de subprocess.run: registra e devolve o código combinado."""

    def __init__(self, codigos=None, erro=None):
        self._codigos = dict(codigos or {})
        self._erro = erro
        self.chamadas = []

    def __call__(self, comando, env=None, cwd=None):
        perfil = (env or {}).get("CLIPS_PERFIL", "")
        self.chamadas.append({"comando": comando, "perfil": perfil,
                              "env": env, "cwd": cwd})
        if self._erro:
            raise self._erro
        return type("Resultado", (), {"returncode": self._codigos.get(perfil, 0)})


# --- nome de perfil -----------------------------------------------------------

@pytest.mark.parametrize("bom", ["esportes", "podcast-2", "canal_b", "x1"])
def test_nome_valido_passa(bom):
    assert settings.nome_de_perfil(bom) == bom


def test_nome_e_normalizado_para_minusculas():
    assert settings.nome_de_perfil("  Esportes  ") == "esportes"


@pytest.mark.parametrize("ruim", ["../..", "a/b", "-comeca-com-traco", "com espaço",
                                  "acentuação", "..", "a;rm -rf"])
def test_nome_torto_e_recusado_e_nao_sanitizado(ruim):
    # O nome vira PASTA e nome de arquivo: '../..' apontaria o banco e o
    # render para fora do projeto. Sanitizar em silêncio esconderia o engano.
    with pytest.raises(ValueError):
        settings.nome_de_perfil(ruim)


def test_sem_perfil_e_string_vazia():
    assert settings.nome_de_perfil(None) == ""
    assert settings.nome_de_perfil("") == ""


# --- raiz de dados ------------------------------------------------------------

def test_sem_perfil_os_dados_ficam_na_raiz(tmp_path):
    assert settings.raiz_de_dados("", base_dir=str(tmp_path)) == str(tmp_path)


def test_com_perfil_os_dados_ficam_em_perfis_nome(tmp_path):
    esperado = os.path.join(str(tmp_path), "perfis", "esportes")
    assert settings.raiz_de_dados("esportes", base_dir=str(tmp_path)) == esperado


def test_caminhos_do_perfil_nao_encostam_nos_do_outro(monkeypatch):
    # O teste que justifica o desenho: dois canais não podem compartilhar
    # banco, render nem token do YouTube.
    def caminhos(perfil):
        monkeypatch.setenv("CLIPS_PERFIL", perfil)
        recarregado = importlib.reload(settings)
        return (recarregado.DB_PATH, recarregado.RENDER_DIR,
                recarregado.CANAIS_PATH, recarregado.YOUTUBE_OAUTH_TOKEN,
                recarregado.DOWNLOADS_DIR, recarregado.TRANSCRICOES_DIR)

    try:
        um = caminhos("esportes")
        outro = caminhos("podcast")
        assert not set(um) & set(outro)
        assert os.path.join("perfis", "esportes", "clips.db") in um[0]
        assert os.path.join("perfis", "esportes", "render") in um[1]
        assert os.path.join("perfis", "esportes", "youtube_token.json") in um[3]
        # A transcrição segue o downloads do perfil, e não a raiz.
        assert um[5].startswith(um[4])
    finally:
        monkeypatch.delenv("CLIPS_PERFIL", raising=False)
        importlib.reload(settings)


def test_sem_perfil_tudo_fica_como_sempre_foi(monkeypatch):
    monkeypatch.delenv("CLIPS_PERFIL", raising=False)
    recarregado = importlib.reload(settings)
    assert recarregado.PERFIL == ""
    assert "perfis" not in recarregado.DB_PATH
    assert recarregado.DB_PATH.endswith("clips.db")
    # O canais.json continua onde sempre esteve, e não migra para a raiz.
    assert recarregado.CANAIS_PATH.endswith(os.path.join("sourcing", "canais.json"))


def test_o_client_secret_e_do_projeto_e_nao_do_canal(monkeypatch):
    # O segredo do app é do projeto do Google Cloud e serve a todos os canais;
    # o TOKEN é que é a autorização daquele canal.
    try:
        monkeypatch.setenv("CLIPS_PERFIL", "esportes")
        recarregado = importlib.reload(settings)
        assert "perfis" not in recarregado.YOUTUBE_CLIENT_SECRETS
        assert "perfis" in recarregado.YOUTUBE_OAUTH_TOKEN
    finally:
        monkeypatch.delenv("CLIPS_PERFIL", raising=False)
        importlib.reload(settings)


# --- descoberta ---------------------------------------------------------------

def test_lista_os_env_de_perfil(raiz):
    base = raiz(".env.esportes", ".env.podcast")
    assert perfis.listar(base) == ["esportes", "podcast"]


def test_o_env_comum_e_o_exemplo_nao_sao_perfis(raiz):
    base = raiz(".env", ".env.example", ".env.local", ".env.esportes")
    assert perfis.listar(base) == ["esportes"]


def test_arquivo_com_nome_torto_e_ignorado_com_aviso(raiz, caplog):
    # Um `.env.Backup~` na raiz é engano de quem copiou arquivo, não um canal.
    base = raiz(".env.esportes", ".env.Backup~")
    assert perfis.listar(base) == ["esportes"]
    assert "Ignorando" in caplog.text


def test_raiz_sem_perfil_nenhum(raiz):
    assert perfis.listar(raiz(".env")) == []


# --- ambiente e comando -------------------------------------------------------

def test_o_perfil_vai_no_ambiente_do_subprocesso():
    env = perfis.ambiente("esportes", base={"PATH": "/bin"})
    assert env["CLIPS_PERFIL"] == "esportes"
    assert env["PATH"] == "/bin"


def test_perfil_padrao_limpa_a_variavel_herdada():
    # Um CLIPS_PERFIL exportado no terminal não pode vazar para dentro da
    # execução que deveria ser a da instalação única.
    env = perfis.ambiente(perfis.PADRAO, base={"CLIPS_PERFIL": "esportes"})
    assert "CLIPS_PERFIL" not in env


def test_comando_de_ciclo_e_de_verificacao():
    assert "--uma-vez" in perfis.montar_comando("x", python="py")
    assert "--verificar" in perfis.montar_comando("x", verificar=True, python="py")
    # --verificar não roda ciclo junto: são coisas diferentes.
    assert "--uma-vez" not in perfis.montar_comando("x", verificar=True, python="py")


# --- execução -----------------------------------------------------------------

def test_cada_perfil_roda_com_o_proprio_ambiente():
    executar = ExecutarFalso()
    perfis.rodar_todos(["esportes", "podcast"], executar=executar)
    assert [c["perfil"] for c in executar.chamadas] == ["esportes", "podcast"]


def test_perfil_que_falha_nao_interrompe_os_outros():
    # Um canal com token vencido não pode impedir os outros de publicar.
    executar = ExecutarFalso(codigos={"esportes": 2})
    codigos = perfis.rodar_todos(["esportes", "podcast"], executar=executar)
    assert codigos == {"esportes": 2, "podcast": 0}
    assert len(executar.chamadas) == 2


def test_subprocesso_que_nem_comeca_vira_codigo_1(caplog):
    executar = ExecutarFalso(erro=OSError("python sumiu"))
    assert perfis.rodar("esportes", executar=executar) == 1
    assert "não chegou a rodar" in caplog.text


# --- CLI ----------------------------------------------------------------------

def test_listar_imprime_os_perfis(monkeypatch, raiz, capsys):
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env.esportes", ".env.podcast"))
    assert perfis.main(["--listar"]) == 0
    assert capsys.readouterr().out.split() == ["esportes", "podcast"]


def test_sem_perfil_configurado_roda_a_instalacao_unica(monkeypatch, raiz,
                                                        caplog):
    caplog.set_level(logging.INFO)
    chamados = []
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env"))
    monkeypatch.setattr(perfis, "rodar",
                        lambda perfil, **kw: chamados.append(perfil) or 0)

    assert perfis.main(["--uma-vez"]) == 0
    assert chamados == [perfis.PADRAO]
    assert "instalação única" in caplog.text


def test_um_perfil_so_pela_linha_de_comando(monkeypatch, raiz):
    chamados = []
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env.esportes", ".env.podcast"))
    monkeypatch.setattr(perfis, "rodar",
                        lambda perfil, **kw: chamados.append(perfil) or 0)

    perfis.main(["--uma-vez", "--perfil", "podcast"])
    assert chamados == ["podcast"]


def test_perfil_torto_na_linha_de_comando_para_antes_de_rodar(monkeypatch,
                                                              raiz, caplog):
    monkeypatch.setattr(perfis, "rodar",
                        lambda *a, **k: pytest.fail("não podia rodar"))
    assert perfis.main(["--uma-vez", "--perfil", "../.."]) == 2
    assert "inválido" in caplog.text


def test_verificar_devolve_1_quando_algum_canal_bloqueia(monkeypatch, raiz):
    # É o código que diz se dá para ligar a publicação; num ciclo normal, ele
    # não importa (falha de etapa é esperada).
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env.esportes", ".env.podcast"))
    monkeypatch.setattr(perfis, "rodar",
                        lambda perfil, **kw: 1 if perfil == "podcast" else 0)
    assert perfis.main(["--verificar"]) == 1


def test_ciclo_com_falha_ainda_sai_zero(monkeypatch, raiz):
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env.esportes"))
    monkeypatch.setattr(perfis, "rodar", lambda perfil, **kw: 3)
    assert perfis.main(["--uma-vez"]) == 0


def test_resumo_mostra_cada_canal(monkeypatch, raiz, capsys):
    monkeypatch.setattr(perfis, "_BASE_DIR", raiz(".env.esportes", ".env.podcast"))
    monkeypatch.setattr(perfis, "rodar",
                        lambda perfil, **kw: 2 if perfil == "podcast" else 0)
    perfis.main(["--uma-vez"])

    saida = capsys.readouterr().out
    assert "esportes" in saida and "ok" in saida
    assert "código 2" in saida
