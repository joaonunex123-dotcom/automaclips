"""Transcrição — formato do .json, timestamps de palavra e reaproveitamento."""
import json
import os
from types import SimpleNamespace

import pytest

from pipeline import transcribe


class ModeloFalso:
    """Duplo do WhisperModel. Registra os kwargs para travar as opções."""

    def __init__(self, segmentos=None, idioma="pt", duracao=600.0):
        self._segmentos = segmentos or []
        self._idioma = idioma
        self._duracao = duracao
        self.chamadas = []

    def transcribe(self, audio_path, **kwargs):
        self.chamadas.append({"audio_path": audio_path, **kwargs})
        info = SimpleNamespace(language=self._idioma, duration=self._duracao)
        return iter(self._segmentos), info


def _segmento(inicio, fim, texto, palavras=None):
    return SimpleNamespace(
        start=inicio, end=fim, text=texto,
        words=[
            SimpleNamespace(start=p[0], end=p[1], word=p[2])
            for p in (palavras or [])
        ],
    )


@pytest.fixture
def audio(tmp_path):
    caminho = tmp_path / "vid1.wav"
    caminho.write_text("audio", encoding="utf-8")
    return str(caminho)


# --- transcrição --------------------------------------------------------------

def test_monta_o_dict_com_palavras(audio):
    modelo = ModeloFalso(
        [_segmento(0.0, 2.5, " boa noite ", [(0.0, 1.0, "boa"), (1.1, 2.5, "noite")])]
    )
    resultado = transcribe.transcrever(audio, modelo=modelo)

    assert resultado["idioma"] == "pt"
    seg = resultado["segmentos"][0]
    assert seg["texto"] == "boa noite"          # sem os espaços do Whisper
    assert seg["palavras"] == [
        {"inicio": 0.0, "fim": 1.0, "palavra": "boa"},
        {"inicio": 1.1, "fim": 2.5, "palavra": "noite"},
    ]


def test_pede_timestamp_por_palavra_e_vad(audio):
    # word_timestamps é o que torna possível a legenda word-by-word da etapa 3;
    # o VAD evita a alucinação clássica do Whisper em trechos mudos.
    modelo = ModeloFalso([])
    transcribe.transcrever(audio, modelo=modelo)
    assert modelo.chamadas[0]["word_timestamps"] is True
    assert modelo.chamadas[0]["vad_filter"] is True


def test_idioma_vazio_vira_deteccao_automatica(audio):
    modelo = ModeloFalso([])
    transcribe.transcrever(audio, modelo=modelo, idioma="")
    assert modelo.chamadas[0]["language"] is None


def test_idioma_fixado_e_repassado(audio):
    modelo = ModeloFalso([])
    transcribe.transcrever(audio, modelo=modelo, idioma="pt")
    assert modelo.chamadas[0]["language"] == "pt"


def test_segmento_sem_palavras_nao_quebra(audio):
    modelo = ModeloFalso([_segmento(0.0, 2.0, "olá")])
    assert transcribe.transcrever(audio, modelo=modelo)["segmentos"][0]["palavras"] == []


def test_audio_inexistente_falha_antes_de_carregar_o_modelo(tmp_path):
    with pytest.raises(transcribe.ErroTranscricao, match="não encontrado"):
        transcribe.transcrever(str(tmp_path / "nao_existe.wav"))


# --- persistência -------------------------------------------------------------

def test_salvar_e_carregar_preservam_acento(tmp_path, transcricao):
    destino = str(tmp_path / "sub" / "vid1.json")
    transcribe.salvar(transcricao((0.0, 2.0, "coração à vontade")), destino)

    # UTF-8 sem escape: o .json é lido por humano quando algo sai errado.
    bruto = open(destino, encoding="utf-8").read()
    assert "coração" in bruto
    assert transcribe.carregar(destino)["segmentos"][0]["texto"] == "coração à vontade"


def test_transcricao_existente_e_reaproveitada(tmp_path, audio, transcricao):
    # É o que torna barato reprocessar o highlight_detect com prompt novo.
    destino_dir = str(tmp_path / "transcricoes")
    transcribe.salvar(
        transcricao((0.0, 2.0, "já estava aqui")),
        transcribe.caminho_para("vid1", destino_dir),
    )

    def explode(*a, **k):
        raise AssertionError("não deveria transcrever de novo")

    caminho, resultado = transcribe.transcrever_para_arquivo(
        audio, "vid1", destino_dir=destino_dir, modelo=explode
    )
    assert resultado["segmentos"][0]["texto"] == "já estava aqui"
    assert os.path.exists(caminho)


def test_transcreve_quando_nao_existe(tmp_path, audio):
    modelo = ModeloFalso([_segmento(0.0, 2.0, "primeira vez")])
    caminho, resultado = transcribe.transcrever_para_arquivo(
        audio, "vid1", destino_dir=str(tmp_path), modelo=modelo
    )
    assert resultado["segmentos"][0]["texto"] == "primeira vez"
    assert json.load(open(caminho, encoding="utf-8"))["segmentos"]


# --- formato para o prompt ----------------------------------------------------

def test_texto_com_timestamps_marca_o_inicio_de_cada_fala(transcricao):
    texto = transcribe.texto_com_timestamps(
        transcricao((0.0, 2.0, "boa noite"), (12.44, 15.0, "e aí ele vira"))
    )
    assert texto == "[0.0] boa noite\n[12.4] e aí ele vira"


def test_texto_ignora_segmento_vazio(transcricao):
    texto = transcribe.texto_com_timestamps(
        transcricao((0.0, 2.0, "  "), (5.0, 7.0, "conteúdo"))
    )
    assert texto == "[5.0] conteúdo"


def test_texto_nao_carrega_timestamp_de_palavra(transcricao):
    # Marcar cada palavra triplicaria o prompt sem melhorar a escolha do
    # trecho; os timestamps de palavra ficam no .json para a etapa 3.
    texto = transcribe.texto_com_timestamps(
        transcricao((0.0, 3.0, "uma frase inteira aqui"))
    )
    assert texto.count("[") == 1


def test_transcricao_sem_segmentos_vira_texto_vazio():
    assert transcribe.texto_com_timestamps({"segmentos": []}) == ""


# --- cache do modelo ----------------------------------------------------------

def test_modelo_e_carregado_uma_vez_por_configuracao():
    # Carregar o Whisper leva segundos e centenas de MB; o pipeline transcreve
    # vários vídeos por execução.
    transcribe._modelo_cache.clear()
    cargas = []

    def carregar(nome, device, compute_type):
        cargas.append((nome, device, compute_type))
        return object()

    a = transcribe.obter_modelo("small", "cpu", "int8", carregar=carregar)
    b = transcribe.obter_modelo("small", "cpu", "int8", carregar=carregar)
    assert a is b and len(cargas) == 1

    # Configuração diferente recarrega, em vez de devolver o antigo em silêncio.
    transcribe.obter_modelo("medium", "cpu", "int8", carregar=carregar)
    assert len(cargas) == 2
    transcribe._modelo_cache.clear()
