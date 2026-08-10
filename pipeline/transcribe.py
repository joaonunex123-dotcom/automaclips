"""Transcrição do áudio com faster-whisper, com timestamp por PALAVRA.

Por que palavra e não só frase: a etapa 3 pede legenda com destaque
word-by-word, e o único jeito de sincronizar isso é ter o instante de cada
palavra. Pedir os timestamps agora custa pouco em cima de uma transcrição que
já vai rodar; extraí-los depois exigiria transcrever tudo de novo.

O resultado vai para um .json em disco, e é esse arquivo que as etapas
seguintes leem. A transcrição é o artefato mais caro do pipeline (minutos de
CPU por vídeo) — mantê-la fora do banco deixa reprocessar o highlight_detect
com um prompt novo custar zero de transcrição.
"""
import json
import logging
import os

import settings

log = logging.getLogger(__name__)

# Modelo carregado uma vez por processo: carregar o Whisper leva segundos e
# aloca centenas de MB, e o pipeline transcreve vários vídeos por execução.
# A chave garante que mudar o modelo/device no ambiente recarrega em vez de
# devolver silenciosamente o antigo.
_modelo_cache = {}


class ErroTranscricao(Exception):
    """Falha ao transcrever."""


def _carregar_modelo_padrao(nome, device, compute_type):
    """Import local: mantém o módulo importável sem faster-whisper instalado."""
    from faster_whisper import WhisperModel

    log.info("Carregando Whisper %s (%s/%s)...", nome, device, compute_type)
    return WhisperModel(nome, device=device, compute_type=compute_type)


def obter_modelo(nome=None, device=None, compute_type=None, carregar=None):
    nome = nome or settings.WHISPER_MODELO
    device = device or settings.WHISPER_DEVICE
    compute_type = compute_type or settings.WHISPER_COMPUTE_TYPE
    chave = (nome, device, compute_type)
    if chave not in _modelo_cache:
        carregar = carregar or _carregar_modelo_padrao
        _modelo_cache[chave] = carregar(nome, device, compute_type)
    return _modelo_cache[chave]


def transcrever(audio_path, modelo=None, idioma=None):
    """Transcreve e devolve o dict que vai para o .json.

    `modelo` injetável para os testes; None carrega o de settings.
    """
    if not os.path.exists(audio_path):
        raise ErroTranscricao(f"áudio não encontrado: {audio_path}")
    modelo = modelo if modelo is not None else obter_modelo()

    idioma = idioma if idioma is not None else settings.WHISPER_IDIOMA
    segmentos_brutos, info = modelo.transcribe(
        audio_path,
        language=idioma or None,   # '' -> None -> detecção automática
        word_timestamps=True,
        # VAD corta silêncio antes de transcrever: em vídeo longo é a diferença
        # entre transcrever o conteúdo e transcrever as pausas. Também evita a
        # alucinação clássica do Whisper em trechos mudos.
        vad_filter=True,
    )

    segmentos = []
    for seg in segmentos_brutos:   # gerador: só aqui a transcrição roda
        segmentos.append(
            {
                "inicio": float(seg.start),
                "fim": float(seg.end),
                "texto": (seg.text or "").strip(),
                "palavras": [
                    {
                        "inicio": float(p.start),
                        "fim": float(p.end),
                        "palavra": (p.word or "").strip(),
                    }
                    for p in (getattr(seg, "words", None) or [])
                ],
            }
        )

    transcricao = {
        "idioma": getattr(info, "language", "") or "",
        "duracao_s": float(getattr(info, "duration", 0) or 0),
        "segmentos": segmentos,
    }
    log.info(
        "Transcrito %s: %d segmentos, idioma %s.",
        os.path.basename(audio_path), len(segmentos), transcricao["idioma"],
    )
    return transcricao


def salvar(transcricao, destino_path):
    """Grava o .json em UTF-8 sem BOM, com acento legível."""
    os.makedirs(os.path.dirname(destino_path) or ".", exist_ok=True)
    with open(destino_path, "w", encoding="utf-8") as f:
        json.dump(transcricao, f, ensure_ascii=False, indent=1)
    return destino_path


def carregar(caminho):
    with open(caminho, encoding="utf-8") as f:
        return json.load(f)


def caminho_para(video_id, destino_dir=None):
    destino_dir = destino_dir or settings.TRANSCRICOES_DIR
    return os.path.join(destino_dir, f"{video_id}.json")


def texto_com_timestamps(transcricao, casas=1):
    """A transcrição no formato que vai no prompt do Claude.

        [12.4] e aí ele vira pra mim e fala
        [15.1] que nunca tinha visto aquilo

    Timestamp por SEGMENTO, não por palavra: o Claude precisa saber onde cada
    fala começa para devolver start/end utilizáveis, e marcar cada palavra
    multiplicaria o tamanho do prompt por três sem melhorar a escolha do
    trecho. Os timestamps de palavra continuam no .json para a etapa 3.
    """
    linhas = []
    for seg in transcricao.get("segmentos", []):
        texto = (seg.get("texto") or "").strip()
        if not texto:
            continue
        linhas.append(f"[{seg['inicio']:.{casas}f}] {texto}")
    return "\n".join(linhas)


def transcrever_para_arquivo(audio_path, video_id, destino_dir=None, modelo=None,
                             idioma=None):
    """Transcreve e persiste. Reaproveita o .json se ele já existe.

    A reutilização é o que torna barato reprocessar o highlight_detect com um
    prompt novo — que é exatamente o que a etapa 7 vai fazer em cima do
    histórico.
    """
    destino = caminho_para(video_id, destino_dir)
    if os.path.exists(destino) and os.path.getsize(destino) > 0:
        log.info("Transcrição já existe: %s", destino)
        return destino, carregar(destino)
    transcricao = transcrever(audio_path, modelo=modelo, idioma=idioma)
    salvar(transcricao, destino)
    return destino, transcricao
