"""Download (yt-dlp) e extração de áudio (ffmpeg) — sem rede e sem binários."""
import os
import shutil
from types import SimpleNamespace

import pytest

from pipeline import download


class YdlFalso:
    """Duplo do YoutubeDL: mesmo protocolo de context manager."""

    def __init__(self, opcoes, info=None, erro=None):
        self.opcoes = opcoes
        self._info = info or {}
        self._erro = erro
        self.urls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def extract_info(self, url, download=True):
        self.urls.append((url, download))
        if self._erro:
            raise self._erro
        return self._info

    def prepare_filename(self, info):
        # O yt-dlp devolve o template já resolvido; aqui basta o diretório de
        # saída mais o id.
        return os.path.join(
            os.path.dirname(self.opcoes["outtmpl"]), f"{info.get('id', 'x')}.mp4"
        )


@pytest.fixture
def ffmpeg_falso(tmp_path):
    """Um caminho de ffmpeg que existe, para não depender do PATH da máquina."""
    caminho = tmp_path / "ffmpeg.exe"
    caminho.write_text("", encoding="utf-8")
    return str(caminho)


# --- ffmpeg -------------------------------------------------------------------

def test_ffmpeg_ausente_diz_o_que_instalar(monkeypatch):
    # Checado ANTES do download: um ffmpeg faltando só apareceria na junção
    # das faixas, depois de centenas de MB já baixados.
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(download.ErroDownload, match="winget"):
        download._garantir_ffmpeg()


def test_ffmpeg_explicito_ganha_do_path(ffmpeg_falso, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/ffmpeg")
    assert download._garantir_ffmpeg(ffmpeg_falso) == ffmpeg_falso


# --- caminho do arquivo baixado -----------------------------------------------

def test_usa_o_filepath_pos_juncao(tmp_path):
    # prepare_filename devolve o nome do template, que numa junção aponta para
    # um arquivo intermediário que já não existe.
    final = tmp_path / "abc.mp4"
    final.write_text("", encoding="utf-8")
    info = {"id": "abc", "requested_downloads": [{"filepath": str(final)}]}
    ydl = YdlFalso({"outtmpl": str(tmp_path / "%(id)s.%(ext)s")}, info)
    assert download._caminho_baixado(info, ydl) == str(final)


def test_fallback_procura_as_extensoes_usuais(tmp_path):
    (tmp_path / "abc.mkv").write_text("", encoding="utf-8")
    info = {"id": "abc"}
    ydl = YdlFalso({"outtmpl": str(tmp_path / "%(id)s.%(ext)s")}, info)
    assert download._caminho_baixado(info, ydl).endswith("abc.mkv")


def test_arquivo_inexistente_falha_explicitamente(tmp_path):
    info = {"id": "sumiu"}
    ydl = YdlFalso({"outtmpl": str(tmp_path / "%(id)s.%(ext)s")}, info)
    with pytest.raises(download.ErroDownload, match="não foi encontrado"):
        download._caminho_baixado(info, ydl)


# --- download -----------------------------------------------------------------

def test_baixa_e_devolve_caminho_e_duracao(tmp_path, ffmpeg_falso):
    final = tmp_path / "vid1.mp4"
    final.write_text("", encoding="utf-8")
    info = {
        "id": "vid1", "duration": 1234.0,
        "requested_downloads": [{"filepath": str(final)}],
    }
    criados = []

    def criar(opcoes):
        ydl = YdlFalso(opcoes, info)
        criados.append(ydl)
        return ydl

    caminho, duracao = download.baixar_video(
        "vid1", destino_dir=str(tmp_path), criar_ydl=criar,
        caminho_ffmpeg=ffmpeg_falso,
    )
    assert caminho == str(final)
    assert duracao == 1234.0
    assert criados[0].urls == [("https://www.youtube.com/watch?v=vid1", True)]
    # mp4 para a etapa 3 não ter de adivinhar o container ao cortar.
    assert criados[0].opcoes["merge_output_format"] == "mp4"


def test_falha_do_ytdlp_vira_erro_do_modulo(tmp_path, ffmpeg_falso):
    def criar(opcoes):
        return YdlFalso(opcoes, erro=RuntimeError("vídeo privado"))

    with pytest.raises(download.ErroDownload, match="vídeo privado"):
        download.baixar_video(
            "vid1", destino_dir=str(tmp_path), criar_ydl=criar,
            caminho_ffmpeg=ffmpeg_falso,
        )


# --- extração de áudio --------------------------------------------------------

def test_monta_o_comando_de_extracao(tmp_path, ffmpeg_falso):
    video = tmp_path / "vid1.mp4"
    video.write_text("", encoding="utf-8")
    comandos = []

    def executar(cmd, **kwargs):
        comandos.append(cmd)
        # o ffmpeg de verdade cria o arquivo; o duplo também
        open(cmd[-1], "w", encoding="utf-8").close()
        return SimpleNamespace(returncode=0, stderr="")

    saida = download.extrair_audio(
        str(video), destino_dir=str(tmp_path), sample_rate=16000,
        executar=executar, caminho_ffmpeg=ffmpeg_falso,
    )
    assert saida.endswith("vid1.wav")
    cmd = comandos[0]
    assert "-vn" in cmd                        # descarta o vídeo
    assert cmd[cmd.index("-ac") + 1] == "1"    # mono
    assert cmd[cmd.index("-ar") + 1] == "16000"


def test_audio_ja_extraido_e_reaproveitado(tmp_path, ffmpeg_falso):
    # Refazer a extração ao retomar um vídeo é decodificá-lo inteiro por nada.
    video = tmp_path / "vid1.mp4"
    video.write_text("", encoding="utf-8")
    (tmp_path / "vid1.wav").write_text("audio", encoding="utf-8")

    def executar(cmd, **kwargs):
        raise AssertionError("não deveria chamar o ffmpeg")

    saida = download.extrair_audio(
        str(video), destino_dir=str(tmp_path), executar=executar,
        caminho_ffmpeg=ffmpeg_falso,
    )
    assert saida.endswith("vid1.wav")


def test_wav_vazio_nao_conta_como_extraido(tmp_path, ffmpeg_falso):
    # Extração interrompida deixa um arquivo de zero byte; reaproveitá-lo
    # entregaria um áudio vazio ao Whisper.
    video = tmp_path / "vid1.mp4"
    video.write_text("", encoding="utf-8")
    (tmp_path / "vid1.wav").write_text("", encoding="utf-8")
    chamou = []

    def executar(cmd, **kwargs):
        chamou.append(cmd)
        open(cmd[-1], "w", encoding="utf-8").write("audio")
        return SimpleNamespace(returncode=0, stderr="")

    download.extrair_audio(
        str(video), destino_dir=str(tmp_path), executar=executar,
        caminho_ffmpeg=ffmpeg_falso,
    )
    assert len(chamou) == 1


def test_falha_do_ffmpeg_reporta_a_ultima_linha(tmp_path, ffmpeg_falso):
    video = tmp_path / "vid1.mp4"
    video.write_text("", encoding="utf-8")

    def executar(cmd, **kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr="linha de ruído\noutra linha\nInvalid data found when processing input",
        )

    with pytest.raises(download.ErroDownload, match="Invalid data found"):
        download.extrair_audio(
            str(video), destino_dir=str(tmp_path), executar=executar,
            caminho_ffmpeg=ffmpeg_falso,
        )


# --- composição ---------------------------------------------------------------

def test_baixar_devolve_o_dict_do_repositorio(tmp_path, ffmpeg_falso):
    final = tmp_path / "vid1.mp4"
    final.write_text("", encoding="utf-8")
    info = {
        "id": "vid1", "duration": 900.0,
        "requested_downloads": [{"filepath": str(final)}],
    }

    def executar(cmd, **kwargs):
        open(cmd[-1], "w", encoding="utf-8").write("audio")
        return SimpleNamespace(returncode=0, stderr="")

    resultado = download.baixar(
        "vid1", destino_dir=str(tmp_path),
        criar_ydl=lambda opcoes: YdlFalso(opcoes, info),
        executar=executar, caminho_ffmpeg=ffmpeg_falso,
    )
    assert set(resultado) == {"video_path", "audio_path", "duracao_real_s"}
    assert resultado["duracao_real_s"] == 900.0
    assert resultado["audio_path"].endswith(".wav")
