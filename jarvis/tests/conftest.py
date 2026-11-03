"""Fixtures compartilhadas.

Nenhum teste lê o .env da máquina nem toca na rede: a config vem de um dict, o
banco vai para tmp_path, e o provedor é o mock (com atraso zero).
"""
import dataclasses

import pytest
from fastapi.testclient import TestClient

from backend.config import carregar_config
from backend.main import criar_app

from tests.constantes import TOKEN


@pytest.fixture
def cfg(tmp_path):
    return carregar_config({
        "JARVIS_TOKEN": TOKEN,
        "JARVIS_LLM": "mock",
        "JARVIS_MOCK_ATRASO_S": "0",
        "JARVIS_DB_PATH": str(tmp_path / "jarvis.db"),
        "JARVIS_THERMAL_DIR": str(tmp_path / "sem_sensor"),
        "JARVIS_TZ": "America/Sao_Paulo",
    })


@pytest.fixture
def app(cfg):
    return criar_app(cfg)


@pytest.fixture
def client(app):
    # `with` dispara o lifespan: o APScheduler sobe e desce a cada teste.
    with TestClient(app) as cliente:
        yield cliente


@pytest.fixture
def cfg_com(cfg):
    """cfg_com(**campos) -> Config com campos trocados (ex.: thermal_dir)."""
    def _fn(**campos):
        return dataclasses.replace(cfg, **campos)
    return _fn
