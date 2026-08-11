"""Fixtures compartilhadas.

Nenhum teste toca rede, disco do projeto ou o clips.db real: o banco vai para
um tmp_path por teste, e a API do YouTube entra por duplo. É o que permite
rodar a suíte inteira sem chave de API e sem quota.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

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


# --- etapa 2: pipeline --------------------------------------------------------

class _Bloco:
    def __init__(self, tipo, texto=None):
        self.type = tipo
        self.text = texto


class _Mensagem:
    def __init__(self, content, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class _Stream:
    """Duplo do context manager de streaming do SDK."""

    def __init__(self, mensagem):
        self._mensagem = mensagem

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def get_final_message(self):
        return self._mensagem


class ClienteClaudeFalso:
    """Duplo do cliente da Anthropic, com as duas superfícies que usamos.

    Registra os kwargs de cada chamada para que os testes possam afirmar sobre
    o formato da requisição — onde o cache_control caiu, se o beta de fallback
    foi enviado — sem rede e sem chave de API.
    """

    def __init__(self, trechos=None, stop_reason="end_turn", input_tokens=1000,
                 texto=None, blocos=None, stop_details=None):
        if blocos is not None:
            content = blocos
        elif texto is not None:
            content = [_Bloco("text", texto)]
        else:
            content = [
                _Bloco("text", json.dumps({"trechos": trechos if trechos is not None else []}))
            ]
        self._mensagem = _Mensagem(content, stop_reason, stop_details)
        self._input_tokens = input_tokens
        self.chamadas = []
        self.contagens = []

        self.messages = SimpleNamespace(
            count_tokens=self._count_tokens,
            stream=lambda **kw: self._stream("padrao", kw),
        )
        self.beta = SimpleNamespace(
            messages=SimpleNamespace(stream=lambda **kw: self._stream("beta", kw))
        )

    def _count_tokens(self, **kwargs):
        self.contagens.append(kwargs)
        return SimpleNamespace(input_tokens=self._input_tokens)

    def _stream(self, superficie, kwargs):
        self.chamadas.append({"superficie": superficie, **kwargs})
        return _Stream(self._mensagem)


@pytest.fixture
def cliente_claude():
    return ClienteClaudeFalso


@pytest.fixture
def bloco():
    """Constrói blocos de conteúdo avulsos (para testar resposta com fallback)."""
    return _Bloco


@pytest.fixture
def transcricao():
    """transcricao(*(inicio, fim, texto)) -> dict no formato do .json."""
    def _fn(*falas, idioma="pt", duracao_s=600.0):
        segmentos = [
            {
                "inicio": float(i),
                "fim": float(f),
                "texto": t,
                "palavras": [
                    {"inicio": float(i), "fim": float(f), "palavra": p}
                    for p in t.split()
                ],
            }
            for i, f, t in falas
        ]
        return {"idioma": idioma, "duracao_s": duracao_s, "segmentos": segmentos}
    return _fn


class ClienteOpenAIFalso:
    """Duplo do cliente da OpenAI, só a superfície de transcrição."""

    def __init__(self, respostas=None, erro=None):
        self._respostas = list(respostas or [])
        self._erro = erro
        self.chamadas = []
        self.audio = SimpleNamespace(
            transcriptions=SimpleNamespace(create=self._create)
        )

    def _create(self, file=None, **kwargs):
        self.chamadas.append(
            {"arquivo": os.path.basename(getattr(file, "name", "")), **kwargs}
        )
        if self._erro:
            raise self._erro
        if self._respostas:
            return self._respostas.pop(0)
        return {"language": "portuguese", "duration": 0.0, "text": "",
                "segments": [], "words": []}


@pytest.fixture
def cliente_openai():
    return ClienteOpenAIFalso


@pytest.fixture
def executar_ok():
    """Duplo de subprocess.run que 'cria' o arquivo de saída e registra a chamada."""
    def _fabricar(registro=None, returncode=0, stderr="", criar_saida=True):
        def executar(comando, **kwargs):
            if registro is not None:
                registro.append({"comando": comando, **kwargs})
            if returncode == 0 and criar_saida:
                alvo = comando[-1]
                if alvo not in ("-", "NUL", "/dev/null"):
                    with open(alvo, "w", encoding="utf-8") as f:
                        f.write("saida")
            return SimpleNamespace(returncode=returncode, stderr=stderr, stdout="")
        return executar
    return _fabricar


@pytest.fixture
def ffmpeg_fake(tmp_path):
    """Caminho de ffmpeg que existe, para não depender do PATH da máquina."""
    caminho = tmp_path / "ffmpeg.exe"
    caminho.write_text("", encoding="utf-8")
    return str(caminho)


class ClienteOpenRouterFalso:
    """Duplo do cliente OpenAI-compatível usado contra o OpenRouter.

    Aceita uma lista de respostas — a primeira serve o modelo principal, a
    segunda o fallback — e registra os kwargs de cada chamada, para os testes
    afirmarem sobre o formato da requisição sem rede nem chave.

    Cada item pode ser: uma string (vira o content), um dict
    {"content":..., "model":...}, ou uma exceção (que é levantada).
    """

    def __init__(self, respostas=None, modelo_respondeu=None):
        self._respostas = list(respostas or [])
        self._modelo_respondeu = modelo_respondeu
        self.chamadas = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )

    def _create(self, **kwargs):
        self.chamadas.append(kwargs)
        item = self._respostas.pop(0) if self._respostas else '{}'
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, dict):
            conteudo = item.get("content", "")
            modelo = item.get("model")
        else:
            conteudo, modelo = item, None
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=conteudo))],
            model=modelo or self._modelo_respondeu or kwargs.get("model"),
            usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45),
        )


@pytest.fixture
def cliente_openrouter():
    return ClienteOpenRouterFalso


class RespostaFalsa:
    def __init__(self, dados=None, status_code=200, text=""):
        self._dados = dados if dados is not None else {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._dados


class HttpFalso:
    """Duplo do `requests`: registra as chamadas e devolve respostas na ordem."""

    def __init__(self, respostas=None):
        self._respostas = list(respostas or [])
        self.chamadas = []

    def request(self, metodo, url, params=None, timeout=None):
        self.chamadas.append({"metodo": metodo, "url": url,
                              "params": dict(params or {})})
        if self._respostas:
            return self._respostas.pop(0)
        return RespostaFalsa({})


@pytest.fixture
def http_falso():
    return HttpFalso


@pytest.fixture
def resposta_falsa():
    return RespostaFalsa


@pytest.fixture
def clip_publicavel(conn, video, transcricao, tmp_path):
    """Um clip selecionado, renderizado e pronto para agendar."""
    def _fn(video_id="vid1", score=9.5, com_render=True, com_transcricao=True):
        fila_id = repositorio.registrar_observacao(
            conn, video(video_id=video_id, titulo=f"Vídeo {video_id}"),
            views=1, ganho=1, score=100.0, status=repositorio.STATUS_ANALISADO,
        )
        caminho_transcricao = str(tmp_path / f"{video_id}.json")
        if com_transcricao:
            with open(caminho_transcricao, "w", encoding="utf-8") as f:
                json.dump(transcricao((100.0, 145.0, "a virada acontece aqui")), f)
        repositorio.registrar_midia(
            conn, fila_id, video_path=f"/v/{video_id}.mp4",
            transcricao_path=caminho_transcricao if com_transcricao else "",
        )
        repositorio.registrar_clips(conn, fila_id, [{
            "inicio_s": 100.0, "fim_s": 145.0, "score_claude": 9.0,
            "motivo": "fecha na virada", "hook_text": "ele nunca contou isso",
            "picos_energia": 3, "score_final": score,
        }])
        clip_id = repositorio.clips_do_video(conn, fila_id)[0]["id"]
        if com_render:
            repositorio.registrar_render(
                conn, clip_id, f"/render/{video_id}_{clip_id}.mp4",
                template_versao="1", duracao_s=45.0,
            )
        return clip_id
    return _fn


@pytest.fixture
def meta_falso():
    """Duplo de metadata.gerar."""
    def _fn(**overrides):
        base = {
            "titulo": "O que ninguém contou sobre isso",
            "descricao": "Ele revela o que aconteceu depois.",
            "caption": "não dava pra prever esse final",
            "hashtags": ["podcast", "entrevista"],
        }
        base.update(overrides)
        return lambda clip, **kwargs: dict(base)
    return _fn


@pytest.fixture
def template():
    """O template_config.json real do repositório, carregado e validado.

    De propósito o arquivo de verdade: se o template que acompanha o repo não
    passar na própria validação, a etapa 3 nasce quebrada e nenhum teste com
    config sintético perceberia.
    """
    from editing import template as template_mod

    return template_mod.carregar()


@pytest.fixture
def trecho():
    """trecho(**overrides) -> dict no formato que highlight_detect devolve."""
    def _fn(**kwargs):
        base = {
            "inicio_s": 100.0,
            "fim_s": 145.0,
            "score_claude": 8.0,
            "motivo": "conta a história e fecha na virada",
            "hook_text": "ele nunca contou isso",
        }
        base.update(kwargs)
        return base
    return _fn
