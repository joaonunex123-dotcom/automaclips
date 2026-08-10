"""Leitura do canais.json."""
import json

import pytest

from sourcing import canais as canais_mod


def _escrever(tmp_path, dados):
    caminho = tmp_path / "canais.json"
    caminho.write_text(json.dumps(dados), encoding="utf-8")
    return str(caminho)


def test_carrega_e_normaliza(tmp_path):
    caminho = _escrever(tmp_path, {"canais": [{"id": "UC1", "nome": "Um"}]})
    assert canais_mod.carregar(caminho) == [
        {"id": "UC1", "nome": "Um", "plataforma": "youtube"}
    ]


def test_ativo_ausente_conta_como_ativo(tmp_path):
    # Só a etapa 7 escreve ativo=false; a ausência é "nunca foi avaliado".
    caminho = _escrever(tmp_path, {"canais": [{"id": "UC1"}]})
    assert len(canais_mod.carregar(caminho)) == 1


def test_canal_desativado_fica_de_fora(tmp_path):
    caminho = _escrever(tmp_path, {"canais": [
        {"id": "UC1", "ativo": True},
        {"id": "UC2", "ativo": False},
    ]})
    assert [c["id"] for c in canais_mod.carregar(caminho)] == ["UC1"]
    # ...mas continua visível para quem quiser a lista inteira (etapa 7).
    assert len(canais_mod.carregar(caminho, somente_ativos=False)) == 2


def test_arquivo_ausente_diz_o_que_fazer(tmp_path):
    faltando = str(tmp_path / "canais.json")
    with pytest.raises(canais_mod.CanaisInvalidos, match="canais.exemplo.json"):
        canais_mod.carregar(faltando)


def test_json_invalido(tmp_path):
    caminho = tmp_path / "canais.json"
    caminho.write_text("{quebrado", encoding="utf-8")
    with pytest.raises(canais_mod.CanaisInvalidos, match="não é JSON válido"):
        canais_mod.carregar(str(caminho))


def test_sem_a_chave_canais(tmp_path):
    caminho = _escrever(tmp_path, {"outra_coisa": []})
    with pytest.raises(canais_mod.CanaisInvalidos, match="lista em 'canais'"):
        canais_mod.carregar(caminho)


def test_canal_sem_id_aponta_a_posicao(tmp_path):
    caminho = _escrever(tmp_path, {"canais": [{"id": "UC1"}, {"nome": "sem id"}]})
    with pytest.raises(canais_mod.CanaisInvalidos, match="posição 1"):
        canais_mod.carregar(caminho)


def test_exemplo_versionado_e_valido():
    # O arquivo que o README manda copiar precisa carregar sem erro.
    import os

    import settings
    exemplo = os.path.join(os.path.dirname(settings.CANAIS_PATH), "canais.exemplo.json")
    assert canais_mod.carregar(exemplo) != []
