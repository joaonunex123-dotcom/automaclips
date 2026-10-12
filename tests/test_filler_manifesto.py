"""Validador do manifesto de filler e rotação de trechos."""
import json
import logging
import random

import pytest

from editing import filler_manifesto as fm


def _entrada(arquivo, **extra):
    base = {"arquivo": arquivo, "categoria": arquivo.split("/")[0],
            "url": "https://www.youtube.com/watch?v=abc", "licenca": "cc",
            "status": "teste"}
    base.update(extra)
    return base


@pytest.fixture
def raiz(tmp_path):
    def _fn(entradas, arquivos):
        for rel in arquivos:
            alvo = tmp_path / rel
            alvo.parent.mkdir(parents=True, exist_ok=True)
            alvo.write_bytes(b"x")
        if entradas is not None:
            (tmp_path / "MANIFESTO.json").write_text(
                json.dumps({"versao": 1, "arquivos": entradas}))
        return str(tmp_path)
    return _fn


def _caminhos(raiz, *rel):
    return [f"{raiz}/{r}" for r in rel]


# --- validador ----------------------------------------------------------------

def test_sem_manifesto_nada_e_filtrado(raiz):
    r = raiz(None, ["slime/a.mp4"])
    assert fm.carregar(r) is None
    assert fm.filtrar(_caminhos(r, "slime/a.mp4"), r) == _caminhos(r, "slime/a.mp4")


def test_arquivo_sem_entrada_e_recusado(raiz, caplog):
    r = raiz([_entrada("slime/a.mp4")], ["slime/a.mp4", "slime/b.mp4"])
    with caplog.at_level(logging.WARNING):
        aceitos = fm.filtrar(_caminhos(r, "slime/a.mp4", "slime/b.mp4"), r)
    assert aceitos == _caminhos(r, "slime/a.mp4")
    assert "sem entrada no manifesto" in caplog.text


@pytest.mark.parametrize("campo,valor", [("url", ""), ("url", None),
                                         ("categoria", ""), ("categoria", "  ")])
def test_entrada_sem_url_ou_categoria_e_recusada(raiz, campo, valor):
    r = raiz([_entrada("slime/a.mp4", **{campo: valor})], ["slime/a.mp4"])
    ok, motivo, _ = fm.validar_arquivo(f"{r}/slime/a.mp4", r, fm.carregar(r))
    assert not ok and motivo


def test_categoria_diferente_da_pasta_e_recusada(raiz):
    r = raiz([_entrada("slime/a.mp4", categoria="subway")], ["slime/a.mp4"])
    ok, motivo, _ = fm.validar_arquivo(f"{r}/slime/a.mp4", r, fm.carregar(r))
    assert not ok and "não bate com a pasta" in motivo


def test_licenca_padrao_avisa_risco(raiz, caplog):
    r = raiz([_entrada("slime/a.mp4", licenca="padrao")], ["slime/a.mp4"])
    ok, motivo, aviso = fm.validar_arquivo(f"{r}/slime/a.mp4", r, fm.carregar(r))
    assert ok and not motivo and "risco" in aviso
    with caplog.at_level(logging.WARNING):
        assert fm.filtrar(_caminhos(r, "slime/a.mp4"), r)
    assert "risco" in caplog.text


def test_licenca_cc_passa_sem_aviso(raiz):
    r = raiz([_entrada("slime/a.mp4")], ["slime/a.mp4"])
    assert fm.validar_arquivo(f"{r}/slime/a.mp4", r, fm.carregar(r)) == (True, "", "")


def test_flag_desligada_recusa_so_a_licenca_padrao(raiz):
    r = raiz([_entrada("slime/cc.mp4"),
              _entrada("slime/p.mp4", licenca="padrao")],
             ["slime/cc.mp4", "slime/p.mp4"])
    aceitos = fm.filtrar(_caminhos(r, "slime/cc.mp4", "slime/p.mp4"), r,
                         permitir_padrao=False)
    assert aceitos == _caminhos(r, "slime/cc.mp4")


def test_manifesto_corrompido_levanta_em_vez_de_liberar_tudo(raiz):
    r = raiz(None, ["slime/a.mp4"])
    (open(f"{r}/MANIFESTO.json", "w")).write("{nao e json")
    with pytest.raises(fm.ErroManifesto):
        fm.filtrar(_caminhos(r, "slime/a.mp4"), r)


# --- rotação ------------------------------------------------------------------

def test_sorteio_e_reproduzivel_com_a_mesma_semente():
    cand = [("a.mp4", 600.0), ("b.mp4", 900.0)]
    um = fm.sortear_trecho(random.Random("s:1"), cand, 30.0)
    dois = fm.sortear_trecho(random.Random("s:1"), cand, 30.0)
    assert um == dois


def test_trecho_longo_fica_dentro_do_arquivo():
    for i in range(50):
        p = fm.sortear_trecho(random.Random(i), [("a.mp4", 100.0)], 30.0)
        assert 0.0 <= p["inicio_s"] and p["fim_s"] <= 100.0 and not p["loop"]


def test_arquivo_curto_entra_em_loop():
    p = fm.sortear_trecho(random.Random(1), [("a.mp4", 10.0)], 30.0)
    assert p["loop"] and p["inicio_s"] == 0.0


def test_nao_repete_trecho_recente_do_mesmo_arquivo():
    recentes = [{"caminho": "a.mp4", "inicio_s": 0.0, "fim_s": 60.0}]
    for i in range(100):
        # a.mp4 tem 120 s: o único trecho livre começa depois de 60 s.
        p = fm.sortear_trecho(random.Random(i), [("a.mp4", 120.0)], 30.0, recentes)
        assert not p["repetido"] and p["inicio_s"] >= 60.0


def test_outro_arquivo_nunca_colide():
    recentes = [{"caminho": "a.mp4", "inicio_s": 0.0, "fim_s": 600.0}]
    for i in range(30):
        p = fm.sortear_trecho(random.Random(i),
                              [("a.mp4", 600.0), ("b.mp4", 600.0)], 30.0, recentes)
        assert p["caminho"] == "b.mp4" or not p["repetido"]


def test_sem_alternativa_repete_e_marca():
    recentes = [{"caminho": "a.mp4", "inicio_s": 0.0, "fim_s": 20.0}]
    p = fm.sortear_trecho(random.Random(1), [("a.mp4", 20.0)], 30.0, recentes)
    assert p["repetido"] and p["caminho"] == "a.mp4"


def test_historico_e_por_canal_e_limitado(tmp_path):
    r = str(tmp_path)
    plano = {"caminho": "a.mp4", "inicio_s": 1.0, "fim_s": 31.0, "loop": False}
    for clip in range(fm.HISTORICO_MAX + 5):
        fm.registrar(r, "canalA", clip, plano)
    fm.registrar(r, "canalB", 1, plano)
    assert len(fm.ler_historico(r, "canalA")) == fm.HISTORICO_MAX
    assert [i["clip_id"] for i in fm.ler_historico(r, "canalB")] == [1]
    assert fm.ler_historico(r, "outro") == []


def test_registrar_mesmo_clip_substitui_em_vez_de_duplicar(tmp_path):
    r = str(tmp_path)
    fm.registrar(r, "c", 7, {"caminho": "a", "inicio_s": 0, "fim_s": 1, "loop": True})
    fm.registrar(r, "c", 7, {"caminho": "b", "inicio_s": 0, "fim_s": 1, "loop": True})
    assert [i["caminho"] for i in fm.ler_historico(r, "c")] == ["b"]
