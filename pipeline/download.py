"""Download do vídeo-fonte (yt-dlp) e extração do áudio de trabalho (ffmpeg).

Baixa o VÍDEO, não só o áudio, mesmo que a etapa 2 precise apenas do som: a
etapa 3 recorta a imagem do mesmo arquivo, e vídeo de canal alheio some — fica
privado, é removido, cai por copyright. Baixar duas vezes é apostar que ele
ainda estará lá amanhã.

O áudio sai num .wav mono de 16 kHz separado porque é o que Whisper e librosa
consomem: pedir a cada um que decodifique o mp4 por conta própria duplicaria a
decodificação do vídeo inteiro, duas vezes, por vídeo.

Dependências externas (nenhuma é pip): **ffmpeg** no PATH — o yt-dlp precisa
dele para juntar as faixas de vídeo e áudio separadas do YouTube, e a extração
do .wav é uma chamada direta a ele.
"""
import logging
import os
import shutil
import subprocess

import settings

log = logging.getLogger(__name__)


# O cliente padrão do yt-dlp (tv/web) falha hoje com "The page needs to be
# reloaded" para qualquer vídeo. Dois clientes servem, cada um com um pedaço:
#
# * android_vr lista 1080p e abre as legendas, mas devolve 403 nas faixas de
#   ÁUDIO;
# * android só oferece o formato 18 (360p COM áudio), e esse o googlevideo
#   entrega.
#
# Pedir os dois na mesma extração não funciona: o yt-dlp descarta os formatos
# do android (exigem PO token quando há outro cliente) e cai no AV1, que dá
# 403. Por isso são duas passadas e um mux: vídeo do android_vr, áudio do 18.
# YTDLP_EXTRACTOR_ARGS (android_vr) é compartilhado com legendas_youtube.
YTDLP_EXTRACTOR_ARGS = {"youtube": {"player_client": ["android_vr"]}}
_ARGS_AUDIO = {"youtube": {"player_client": ["android"]}}
_FORMATO_VIDEO = "bestvideo[height<=1080][vcodec^=avc1]/bestvideo[height<=1080]"
_FORMATO_AUDIO = "18"

# O googlevideo recusa (403) a requisição de faixa inteira de um vídeo longo
# para esses clientes; em pedaços ele entrega.
YTDLP_CHUNK_BYTES = 5 * 1024 * 1024


class ErroDownload(Exception):
    """Falha ao baixar ou converter — problema de operação, não de programação."""


def _garantir_ffmpeg(caminho_ffmpeg=None):
    """Resolve o binário do ffmpeg ou falha com instrução, não com FileNotFound.

    Checado ANTES do download: um ffmpeg ausente só apareceria na hora de
    juntar as faixas, depois de já ter baixado centenas de megabytes.
    """
    caminho = caminho_ffmpeg or shutil.which("ffmpeg")
    if not caminho:
        raise ErroDownload(
            "ffmpeg não encontrado no PATH. É obrigatório: o yt-dlp o usa para "
            "juntar vídeo e áudio, e a extração do .wav depende dele. "
            "Instale (winget install Gyan.FFmpeg) e reabra o terminal."
        )
    return caminho


def _caminho_baixado(info, ydl):
    """Extrai o caminho final do arquivo da resposta do yt-dlp.

    `requested_downloads[0].filepath` é o caminho DEPOIS da junção das faixas;
    prepare_filename devolve o nome do template, que numa junção aponta para um
    arquivo intermediário que já não existe. O fallback existe só para versões
    do yt-dlp que não preenchem requested_downloads.
    """
    baixados = info.get("requested_downloads") or []
    if baixados and baixados[0].get("filepath"):
        return baixados[0]["filepath"]

    provavel = ydl.prepare_filename(info)
    if os.path.exists(provavel):
        return provavel
    raiz, _ = os.path.splitext(provavel)
    for ext in (".mp4", ".mkv", ".webm"):
        if os.path.exists(raiz + ext):
            return raiz + ext
    raise ErroDownload(f"yt-dlp terminou mas o arquivo não foi encontrado: {provavel}")


def _criar_ydl_padrao(opcoes):
    """Import local: mantém o módulo importável sem yt-dlp instalado."""
    from yt_dlp import YoutubeDL

    return YoutubeDL(opcoes)


def _baixar_duas_passadas(video_id, destino_dir, ffmpeg, criar_ydl,
                          executar=None):
    """Vídeo (android_vr) + áudio (android) e mux. Devolve (caminho, duração).

    Idempotente por arquivo, como o caminho de passada única: se o .mp4 final
    já existe, não baixa de novo.
    """
    executar = executar or subprocess.run
    final = os.path.join(destino_dir, f"{video_id}.mp4")
    url = f"https://www.youtube.com/watch?v={video_id}"

    def _passada(formato, args, sufixo):
        opcoes = {
            "format": formato,
            "outtmpl": os.path.join(destino_dir, f"%(id)s.{sufixo}.%(ext)s"),
            "extractor_args": args,
            "http_chunk_size": YTDLP_CHUNK_BYTES,
            "quiet": True,
            "noprogress": True,
            "no_warnings": True,
        }
        ydl = criar_ydl(opcoes)
        with ydl:
            info = ydl.extract_info(url, download=True)
        return _caminho_baixado(info, ydl), info

    try:
        if os.path.exists(final) and os.path.getsize(final) > 0:
            # Só os metadados (nenhum byte de mídia): a duração é do YouTube.
            ydl = criar_ydl({"extractor_args": YTDLP_EXTRACTOR_ARGS,
                             "quiet": True, "no_warnings": True})
            with ydl:
                meta = ydl.extract_info(url, download=False)
            return final, float(meta.get("duration") or 0)
        video, info_v = _passada(_FORMATO_VIDEO, YTDLP_EXTRACTOR_ARGS, "video")
        audio, _ = _passada(_FORMATO_AUDIO, _ARGS_AUDIO, "audio")
    except ErroDownload:
        raise
    except Exception as e:
        raise ErroDownload(f"yt-dlp falhou em {video_id}: {e}") from e

    resultado = executar(
        [ffmpeg, "-y", "-i", video, "-i", audio, "-map", "0:v:0",
         "-map", "1:a:0", "-c", "copy", "-movflags", "+faststart", final],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        ultima = (resultado.stderr or "").strip().splitlines()
        raise ErroDownload(
            f"ffmpeg falhou ao juntar vídeo e áudio de {video_id}: "
            f"{ultima[-1] if ultima else 'sem stderr'}"
        )
    # As faixas soltas só servem para o mux; o .mp4 final é o que fica.
    for parcial in (video, audio):
        if parcial != final:
            try:
                os.remove(parcial)
            except OSError:
                pass
    return final, float(info_v.get("duration") or 0)


def baixar_video(video_id, destino_dir=None, formato=None, criar_ydl=None,
                 caminho_ffmpeg=None, executar=None):
    """Baixa o vídeo e devolve (caminho, duração_em_segundos).

    Idempotente por arquivo: o yt-dlp pula o download se o destino já existe,
    então retomar um vídeo que falhou na transcrição não rebaixa o vídeo.

    Sem `formato`, baixa em duas passadas (ver YTDLP_EXTRACTOR_ARGS) e o
    settings.YTDLP_FORMATO não entra: ele descreve uma passada única, que hoje
    não funciona contra o YouTube. Com `formato` explícito, passada única.
    """
    destino_dir = destino_dir or settings.DOWNLOADS_DIR
    os.makedirs(destino_dir, exist_ok=True)
    ffmpeg = _garantir_ffmpeg(caminho_ffmpeg)
    criar_ydl = criar_ydl or _criar_ydl_padrao

    if formato is None:
        caminho, duracao = _baixar_duas_passadas(
            video_id, destino_dir, ffmpeg, criar_ydl, executar=executar
        )
        log.info("Baixado %s (%.0fs) em %s", video_id, duracao, caminho)
        return caminho, duracao

    opcoes = {
        "format": formato,
        "outtmpl": os.path.join(destino_dir, "%(id)s.%(ext)s"),
        # mp4 para a etapa 3 não precisar adivinhar o container na hora de
        # cortar.
        "merge_output_format": "mp4",
        "extractor_args": YTDLP_EXTRACTOR_ARGS,
        "http_chunk_size": YTDLP_CHUNK_BYTES,
        "ffmpeg_location": os.path.dirname(ffmpeg) or None,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        ydl = criar_ydl(opcoes)
        with ydl:
            info = ydl.extract_info(url, download=True)
    except ErroDownload:
        raise
    except Exception as e:
        raise ErroDownload(f"yt-dlp falhou em {video_id}: {e}") from e

    caminho = _caminho_baixado(info, ydl)
    duracao = float(info.get("duration") or 0)
    log.info("Baixado %s (%.0fs) em %s", video_id, duracao, caminho)
    return caminho, duracao


def extrair_audio(video_path, destino_dir=None, sample_rate=None,
                  executar=None, caminho_ffmpeg=None):
    """Extrai um .wav mono na taxa de trabalho. Devolve o caminho.

    Reaproveita o arquivo se ele já existe: a extração é determinística, e
    refazê-la ao retomar um vídeo é decodificar o vídeo inteiro de novo por
    nada.
    """
    destino_dir = destino_dir or settings.DOWNLOADS_DIR
    os.makedirs(destino_dir, exist_ok=True)
    sample_rate = sample_rate or settings.AUDIO_SAMPLE_RATE
    ffmpeg = _garantir_ffmpeg(caminho_ffmpeg)
    executar = executar or subprocess.run

    nome = os.path.splitext(os.path.basename(video_path))[0]
    saida = os.path.join(destino_dir, f"{nome}.wav")
    if os.path.exists(saida) and os.path.getsize(saida) > 0:
        log.debug("Áudio já extraído: %s", saida)
        return saida

    comando = [
        ffmpeg, "-y",
        "-i", video_path,
        "-vn",                      # descarta o vídeo
        "-ac", "1",                 # mono
        "-ar", str(sample_rate),
        "-f", "wav",
        saida,
    ]
    resultado = executar(comando, capture_output=True, text=True)
    if resultado.returncode != 0:
        # stderr do ffmpeg é longo; a última linha é a que diz o quê.
        ultima = (resultado.stderr or "").strip().splitlines()
        raise ErroDownload(
            f"ffmpeg falhou ao extrair áudio de {video_path}: "
            f"{ultima[-1] if ultima else 'sem stderr'}"
        )
    log.info("Áudio extraído em %s", saida)
    return saida


def baixar(video_id, destino_dir=None, formato=None, criar_ydl=None,
           executar=None, sample_rate=None, caminho_ffmpeg=None):
    """Vídeo + áudio numa chamada. Devolve o dict que o repositório grava.

    Os parâmetros são explícitos (e não **kwargs) porque as duas etapas aceitam
    conjuntos diferentes: repassar kwargs em bloco mandaria `executar` para o
    yt-dlp e `formato` para o ffmpeg.
    """
    video_path, duracao = baixar_video(
        video_id, destino_dir=destino_dir, formato=formato,
        criar_ydl=criar_ydl, caminho_ffmpeg=caminho_ffmpeg, executar=executar,
    )
    audio_path = extrair_audio(
        video_path, destino_dir=destino_dir, sample_rate=sample_rate,
        executar=executar, caminho_ffmpeg=caminho_ffmpeg,
    )
    return {
        "video_path": video_path,
        "audio_path": audio_path,
        "duracao_real_s": duracao,
    }
