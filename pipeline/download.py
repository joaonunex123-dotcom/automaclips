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


# Dois caminhos de download, medidos em 10/10/2026 (vídeo 42, 1h50):
#
# * HD (1080p). Exige yt-dlp >= 2026.08 (Python >= 3.10) com o plugin
#   bgutil-ytdlp-pot-provider, o servidor de PO token no ar (ops/pot_provider.sh)
#   e um runtime JS (node). Sem PO token o cliente web só devolve imagens, e o
#   android_vr corta o googlevideo com 403 em ~10 MB.
# * 360p (fallback). Cliente android, formato 18 (360p COM áudio): o único que
#   baixa inteiro sem PO token. Usado quando o servidor está fora do ar ou o
#   HD falha — o clip sai pior, mas sai.
#
# O android_vr fica só nas legendas (YTDLP_EXTRACTOR_ARGS, compartilhado com
# legendas_youtube).
YTDLP_EXTRACTOR_ARGS = {"youtube": {"player_client": ["android_vr"]}}
_ARGS_360P = {"youtube": {"player_client": ["android"]}}
_FORMATO_360P = "18"


class ErroDownload(Exception):
    """Falha ao baixar ou converter — problema de operação, não de programação."""


class FFmpegAusente(ErroDownload):
    """O ffmpeg não está no PATH: defeito do ambiente, não do vídeo."""


def _garantir_ffmpeg(caminho_ffmpeg=None):
    """Resolve o binário do ffmpeg ou falha com instrução, não com FileNotFound.

    Checado ANTES do download: um ffmpeg ausente só apareceria na hora de
    juntar as faixas, depois de já ter baixado centenas de megabytes.
    """
    caminho = caminho_ffmpeg or shutil.which("ffmpeg")
    if not caminho:
        raise FFmpegAusente(
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


def pot_no_ar(url=None, timeout=2.0):
    """O servidor de PO token responde? Barato (um GET) e evita gastar um
    extract_info inteiro só para descobrir que o HD não vai sair."""
    import urllib.request

    url = (url or settings.POT_URL).rstrip("/") + "/ping"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _opcoes_hd():
    extra = {"youtubepot-bgutilhttp": {"base_url": [settings.POT_URL]}}
    opcoes = {"extractor_args": extra}
    if settings.YTDLP_JS_RUNTIME:
        opcoes["js_runtimes"] = {"node": {"path": settings.YTDLP_JS_RUNTIME}}
    else:
        opcoes["js_runtimes"] = {"node": {}}
    return opcoes


def baixar_video(video_id, destino_dir=None, formato=None, criar_ydl=None,
                 caminho_ffmpeg=None, pot_ativo=None):
    """Baixa o vídeo e devolve (caminho, duração_em_segundos).

    Tenta 1080p (settings.YTDLP_FORMATO) quando o servidor de PO token está no
    ar e cai para 360p (formato 18) se ele estiver fora ou o HD falhar. A altura
    realmente obtida fica em `ultimo_download["altura"]` e no log.

    Idempotente por arquivo: o yt-dlp pula o download se o destino já existe,
    então retomar um vídeo que falhou na transcrição não rebaixa o vídeo. Um
    360p já em disco NÃO é trocado por HD sozinho: apague o .mp4 para refazer.
    """
    destino_dir = destino_dir or settings.DOWNLOADS_DIR
    os.makedirs(destino_dir, exist_ok=True)
    ffmpeg = _garantir_ffmpeg(caminho_ffmpeg)
    criar_ydl = criar_ydl or _criar_ydl_padrao

    base = {
        "outtmpl": os.path.join(destino_dir, "%(id)s.%(ext)s"),
        # mp4 para a etapa 3 não precisar adivinhar o container na hora de
        # cortar.
        "merge_output_format": "mp4",
        "ffmpeg_location": os.path.dirname(ffmpeg) or None,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    url = f"https://www.youtube.com/watch?v={video_id}"

    tentativas = []
    hd_possivel = pot_no_ar() if pot_ativo is None else pot_ativo
    if hd_possivel:
        tentativas.append(("1080p", {
            **base, "format": formato or settings.YTDLP_FORMATO, **_opcoes_hd(),
        }))
    else:
        log.warning("Servidor de PO token fora do ar (%s): baixando em 360p. "
                    "Suba com ops/pot_provider.sh subir.", settings.POT_URL)
    tentativas.append(("360p", {
        **base, "format": _FORMATO_360P, "extractor_args": _ARGS_360P,
    }))

    ultimo_erro = None
    for rotulo, opcoes in tentativas:
        try:
            ydl = criar_ydl(opcoes)
            with ydl:
                info = ydl.extract_info(url, download=True)
            caminho = _caminho_baixado(info, ydl)
        except ErroDownload as e:
            ultimo_erro = e
        except Exception as e:
            ultimo_erro = ErroDownload(f"yt-dlp falhou em {video_id}: {e}")
        else:
            duracao = float(info.get("duration") or 0)
            altura = info.get("height") or max(
                [d.get("height") or 0 for d in info.get("requested_downloads") or []]
                or [0]
            )
            largura = info.get("width") or 0
            ultimo_download.update(altura=altura, largura=largura,
                                   tentativa=rotulo)
            log.info("Baixado %s (%.0fs, %sx%s) em %s", video_id, duracao,
                     largura or "?", altura or "?", caminho)
            return caminho, duracao
        if rotulo != tentativas[-1][0]:
            log.warning("Download %s falhou (%s); caindo para 360p.", rotulo,
                        ultimo_erro)
    raise ultimo_erro


# Resultado do último download (altura e qual tentativa valeu), para a CLI e os
# testes mostrarem a resolução obtida sem mudar o retorno de baixar_video.
ultimo_download = {}


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
        criar_ydl=criar_ydl, caminho_ffmpeg=caminho_ffmpeg,
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
