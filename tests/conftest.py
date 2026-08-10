"""Fixtures compartilhadas.

Nenhum teste toca rede, disco do projeto ou o clips.db real: o banco vai para
um tmp_path por teste, e a API do YouTube entra por duplo. É o que permite
rodar a suíte inteira sem chave de API e sem quota.
"""
from datetime import datetime, timedelta, timezone

import pytest

import settings
from db import repositorio

# Instante fixo para todo cálculo de idade. Com datetime.now() os testes de
# score ficariam sensíveis ao relógio da máquina e à duração da própria suíte.
AGORA = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """Conexão a um clips.db descartável, com o schema já aplicado."""
    monkeypatch.setattr(settings, "DB_PATH", str(tmp_path / "clips.db"))
    conexao = repositorio.conectar()
    yield conexao
    conexao.close()


@pytest.fixture
def publicado_ha():
    """publicado_ha(horas) -> string ISO UTC, como a API do YouTube devolve."""
    def _fn(horas):
        return (AGORA - timedelta(hours=horas)).isoformat().replace("+00:00", "Z")
    return _fn


@pytest.fixture
def video(publicado_ha):
    """video(**overrides) -> dict no formato que sourcing/youtube.py produz."""
    def _fn(**kwargs):
        base = {
            "plataforma": "youtube",
            "video_id": "vid1",
            "canal_id": "UC_teste",
            "canal_nome": "Canal de Teste",
            "titulo": "Um título",
            "url": "https://www.youtube.com/watch?v=vid1",
            "publicado_em": publicado_ha(6),
            "duracao_s": 1200,
            "views": 0,
        }
        base.update(kwargs)
        return base
    return _fn


class ClienteFalso:
    """Duplo da API do YouTube: mesma forma encadeada, respostas de dicionário.

    Conta as chamadas por endpoint para que os testes possam afirmar sobre
    quota — o desenho de youtube.py existe justamente para não gastá-la.
    """

    def __init__(self, canais=None, playlists=None, videos=None):
        self._canais = canais or {}
        self._playlists = playlists or {}
        self._videos = videos or {}
        self.chamadas = {"channels": 0, "playlistItems": 0, "videos": 0}

    class _Requisicao:
        def __init__(self, resultado):
            self._resultado = resultado

        def execute(self):
            return self._resultado

    class _Recurso:
        def __init__(self, fn):
            self._fn = fn

        def list(self, **kwargs):
            return ClienteFalso._Requisicao(self._fn(**kwargs))

    def channels(self):
        def _fn(id="", **_):
            self.chamadas["channels"] += 1
            itens = []
            for canal_id in id.split(","):
                dados = self._canais.get(canal_id)
                if dados is None:
                    continue
                itens.append(
                    {
                        "id": canal_id,
                        "snippet": {"title": dados["nome"]},
                        "contentDetails": {
                            "relatedPlaylists": {"uploads": dados["uploads"]}
                        },
                    }
                )
            return {"items": itens}
        return self._Recurso(_fn)

    def playlistItems(self):
        def _fn(playlistId="", maxResults=50, **_):
            self.chamadas["playlistItems"] += 1
            ids = self._playlists.get(playlistId, [])[:maxResults]
            return {"items": [{"contentDetails": {"videoId": v}} for v in ids]}
        return self._Recurso(_fn)

    def videos(self):
        def _fn(id="", **_):
            self.chamadas["videos"] += 1
            itens = []
            for video_id in id.split(","):
                dados = self._videos.get(video_id)
                if dados is None:
                    continue
                itens.append({"id": video_id, **dados})
            return {"items": itens}
        return self._Recurso(_fn)


@pytest.fixture
def cliente_falso():
    return ClienteFalso
