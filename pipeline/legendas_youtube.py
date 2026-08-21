"""Transcrição de graça: as legendas que o próprio YouTube já tem.

Terceiro backend, e o mais barato dos três — não usa modelo nenhum, não gasta
API e não gasta CPU. O yt-dlp busca a legenda junto com o vídeo, sem chave de
API.

A escolha que não é óbvia, e por isso é configurável:

* **Legenda automática** (gerada por reconhecimento de fala) traz o timestamp
  de CADA PALAVRA, embutido no VTT como marcas `<00:00:01.234>`. É pior de
  texto — erra nome próprio, não pontua direito — mas é a única das duas que
  permite a legenda word-by-word do template.
* **Legenda manual** (enviada pelo canal) tem texto muito melhor, e só
  timestamp por FRASE. Com ela o clip sai legendado e sincronizado, mas sem o
  destaque palavra a palavra.

O padrão é a automática, porque o destaque é o que o template promete. Quem
preferir o texto limpo troca em YOUTUBE_LEGENDAS_PREFERIR.

O formato das automáticas tem uma armadilha: elas são "rolantes" — cada bloco
repete, em linhas próprias, o texto dos blocos anteriores, para criar o efeito
de rolagem na tela. Lido ingenuamente, isso triplica cada palavra. Ver
`parse_vtt` para as duas defesas (só a última linha de cada bloco, e tempo
monotônico) e por que uma sozinha não basta.
"""
import glob
import logging
import os
import re

import settings

log = logging.getLogger(__name__)


class ErroLegendasYoutube(Exception):
    """Vídeo sem legenda utilizável, ou falha ao buscá-la."""


# 00:00:12.345 --> 00:00:15.678  (o resto da linha é posicionamento, ignorado)
_CUE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
# Marca de tempo embutida no meio do texto, que é onde mora o timestamp de
# palavra das legendas automáticas.
_MARCA = re.compile(r"<(\d{2}:\d{2}:\d{2}[.,]\d{3})>")
# Tags de estilo do VTT (<c>, </c>, <c.colorE5E5E5>) — ruído para nós.
_TAG = re.compile(r"</?c[^>]*>")


def _segundos(marca):
    """'00:01:02.345' -> 62.345"""
    hora, minuto, resto = marca.replace(",", ".").split(":")
    return int(hora) * 3600 + int(minuto) * 60 + float(resto)


def _cues(texto):
    """[(inicio, fim, [linhas_nao_vazias])] do VTT, na ordem.

    O corpo vai ate o PROXIMO bloco, e nao ate a primeira linha em branco. As
    legendas automaticas do YouTube poem uma linha vazia (as vezes um espaco
    solitario) logo depois do tempo no primeiro bloco; parar ali descartava o
    bloco inteiro -- ou seja, a primeira frase do video sumia.
    """
    saida = []
    linhas = texto.splitlines()
    i = 0
    while i < len(linhas):
        achado = _CUE.search(linhas[i])
        if not achado:
            i += 1
            continue
        inicio, fim = _segundos(achado.group(1)), _segundos(achado.group(2))
        corpo = []
        i += 1
        while i < len(linhas) and not _CUE.search(linhas[i]):
            if linhas[i].strip():
                corpo.append(linhas[i])
            i += 1
        if corpo:
            saida.append((inicio, fim, corpo))
    return saida


def _palavras_da_linha(linha, inicio_cue):
    """Linha com marcas embutidas -> palavras com tempo.

    O trecho ANTES da primeira marca começa no início do bloco; cada trecho
    seguinte começa na marca que veio antes dele.
    """
    pedacos = _MARCA.split(_TAG.sub("", linha))
    atual = inicio_cue
    palavras = []
    for indice, pedaco in enumerate(pedacos):
        if indice % 2 == 1:
            atual = _segundos(pedaco)
            continue
        for token in pedaco.split():
            palavras.append({"inicio": atual, "fim": atual, "palavra": token})
    return palavras


def parse_vtt(texto):
    """VTT -> lista de {inicio, fim, palavra}, sem repetição.

    Duas passadas, porque os dois tipos de legenda pedem tratamentos opostos:

    * **Com marca de palavra** (automática). O formato é ROLANTE: cada bloco
      repete, em linhas próprias, o texto dos blocos anteriores, e só a ÚLTIMA
      linha traz o conteúdo novo. Ler o bloco inteiro triplicaria cada palavra
      — e deduplicar só por tempo não salva, porque o texto repetido recebe o
      tempo do bloco atual e passa como se fosse fala nova. Por isso: só a
      última linha, e só de blocos que têm marca.
    * **Sem marca nenhuma** (manual, enviada pelo canal). Aqui cada bloco é uma
      frase inteira e não há repetição; cada um vira uma unidade só.
    """
    cues = _cues(texto)
    com_marca = [c for c in cues if any(_MARCA.search(l) for l in c[2])]

    if not com_marca:
        # Legenda manual: sem timestamp por palavra, cada bloco é uma frase.
        palavras = []
        for inicio, fim, corpo in cues:
            frase = " ".join(" ".join(corpo).split())
            frase = _TAG.sub("", frase).strip()
            if frase:
                palavras.append({"inicio": inicio, "fim": fim, "palavra": frase})
        return _dedup(palavras, fim_padrao=0.35)

    palavras = []
    for inicio, _fim, corpo in com_marca:
        palavras.extend(_palavras_da_linha(corpo[-1], inicio))
    return _dedup(palavras, fim_padrao=0.35)


# Duração máxima de uma palavra falada. Ver _dedup.
DURACAO_MAXIMA_PALAVRA_S = 1.0


def _dedup(palavras, fim_padrao=0.35, duracao_maxima=None):
    """Tira a repetição das legendas rolantes e fecha o `fim` de cada palavra.

    A regra da repetição é o tempo MONOTÔNICO: uma palavra cujo início não
    avançou em relação à última aceita é texto repetido do bloco anterior, não
    fala nova.

    O `fim` de uma palavra é o início da seguinte — é o que mantém a legenda na
    tela sem piscar entre uma palavra e outra —, mas LIMITADO. Encadear sem
    teto apagaria todo silêncio da transcrição: cada palavra passaria a durar
    até a próxima, e uma pausa de cinco segundos viraria uma palavra de cinco
    segundos. Sem silêncio, `montar_segmentos` perde a única pista de onde uma
    frase acaba, e a transcrição sai como um bloco corrido.
    """
    if duracao_maxima is None:
        duracao_maxima = DURACAO_MAXIMA_PALAVRA_S

    saida = []
    for palavra in palavras:
        if saida and palavra["inicio"] <= saida[-1]["inicio"]:
            continue
        if saida and saida[-1]["fim"] <= saida[-1]["inicio"]:
            saida[-1]["fim"] = min(
                palavra["inicio"], saida[-1]["inicio"] + duracao_maxima
            )
        saida.append(dict(palavra))
    if saida and saida[-1]["fim"] <= saida[-1]["inicio"]:
        saida[-1]["fim"] = saida[-1]["inicio"] + fim_padrao
    return saida


def _e_frase(unidade):
    """Unidade que já é uma frase inteira, e não uma palavra.

    Só a legenda manual produz isso, e a marca é simples: palavra de verdade
    não tem espaço no meio.
    """
    return " " in unidade["palavra"].strip()


def montar_segmentos(palavras, intervalo_frase=0.8, maximo_palavras=14):
    """Agrupa palavras em segmentos, como o Whisper devolveria.

    Quebra numa pausa maior que `intervalo_frase` ou ao atingir o teto de
    palavras. O teto existe porque fala corrida sem pausa audível produziria um
    "segmento" de dois minutos, e é o segmento que vira linha no prompt do
    highlight_detect.

    Unidade que já é frase (legenda manual) vira segmento sozinha: juntar duas
    frases num segmento só faria a legenda mostrar as duas ao mesmo tempo, pelo
    tempo somado das duas.
    """
    segmentos = []
    atual = []
    for palavra in palavras:
        if atual:
            pausa = palavra["inicio"] - atual[-1]["fim"]
            if (_e_frase(palavra) or _e_frase(atual[-1])
                    or pausa > intervalo_frase
                    or len(atual) >= maximo_palavras):
                segmentos.append(_fechar(atual))
                atual = []
        atual.append(palavra)
    if atual:
        segmentos.append(_fechar(atual))
    return segmentos


def _fechar(palavras):
    return {
        "inicio": palavras[0]["inicio"],
        "fim": palavras[-1]["fim"],
        "texto": " ".join(p["palavra"] for p in palavras),
        # Uma "palavra" que é a frase inteira (legenda manual) não vira
        # timestamp de palavra: repetir a frase como se fosse uma palavra faria
        # o destaque piscar o bloco todo em vez de andar.
        "palavras": [] if any(_e_frase(p) for p in palavras) else list(palavras),
    }


# --- busca via yt-dlp ---------------------------------------------------------

def _criar_ydl_padrao(opcoes):
    from yt_dlp import YoutubeDL

    return YoutubeDL(opcoes)


def baixar_vtt(video_id, destino_dir, idiomas=None, preferir=None,
               criar_ydl=None):
    """Baixa a legenda e devolve (caminho_do_vtt, automatica?).

    Sem chave de API: o yt-dlp lê a mesma página que o navegador lê.
    """
    idiomas = idiomas or settings.YOUTUBE_LEGENDAS_IDIOMAS
    preferir = preferir or settings.YOUTUBE_LEGENDAS_PREFERIR
    criar_ydl = criar_ydl or _criar_ydl_padrao
    os.makedirs(destino_dir, exist_ok=True)

    automatica = preferir != "manual"
    opcoes = {
        "skip_download": True,
        "writesubtitles": not automatica,
        "writeautomaticsub": automatica,
        "subtitleslangs": list(idiomas),
        "subtitlesformat": "vtt",
        "outtmpl": os.path.join(destino_dir, "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
    }

    url = f"https://www.youtube.com/watch?v={video_id}"
    try:
        with criar_ydl(opcoes) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        raise ErroLegendasYoutube(f"yt-dlp falhou ao buscar legenda: {e}") from e

    encontrados = sorted(glob.glob(os.path.join(destino_dir, f"{video_id}*.vtt")))
    if encontrados:
        return encontrados[0], automatica

    # Vídeo sem o tipo pedido: tenta o outro antes de desistir. Canal grande
    # costuma ter legenda manual; canal pequeno, só a automática.
    outro = "manual" if automatica else "auto"
    log.info("Sem legenda %s em %s; tentando %s.",
             "automática" if automatica else "manual", video_id, outro)
    opcoes["writesubtitles"] = automatica
    opcoes["writeautomaticsub"] = not automatica
    try:
        with criar_ydl(opcoes) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        raise ErroLegendasYoutube(f"yt-dlp falhou ao buscar legenda: {e}") from e

    encontrados = sorted(glob.glob(os.path.join(destino_dir, f"{video_id}*.vtt")))
    if not encontrados:
        raise ErroLegendasYoutube(
            f"{video_id} não tem legenda em {idiomas}. Use "
            "TRANSCRICAO_BACKEND=local ou openai para este vídeo."
        )
    return encontrados[0], not automatica


def transcrever(audio_path=None, duracao_s=0.0, video_id=None, destino_dir=None,
                idiomas=None, preferir=None, criar_ydl=None, caminho_vtt=None):
    """Mesmo contrato de saída dos outros dois backends.

    `audio_path` é ignorado — está na assinatura só para o despachante de
    transcribe.py tratar os três backends igual.
    """
    if caminho_vtt is None:
        if not video_id:
            raise ErroLegendasYoutube("sem video_id para buscar a legenda")
        destino_dir = destino_dir or settings.TRANSCRICOES_DIR
        caminho_vtt, automatica = baixar_vtt(
            video_id, destino_dir, idiomas, preferir, criar_ydl
        )
    else:
        automatica = True

    with open(caminho_vtt, encoding="utf-8") as f:
        palavras = parse_vtt(f.read())
    if not palavras:
        raise ErroLegendasYoutube(f"legenda vazia em {caminho_vtt}")

    segmentos = montar_segmentos(palavras)
    com_palavra = sum(1 for s in segmentos if s["palavras"])
    log.info("Legenda do YouTube: %d segmentos, %d com timestamp por palavra "
             "(%s).", len(segmentos), com_palavra,
             "automática" if automatica else "manual")

    idioma = ""
    nome = os.path.basename(caminho_vtt)
    partes = nome.rsplit(".", 2)
    if len(partes) == 3:
        idioma = partes[1]

    return {
        "idioma": idioma,
        "duracao_s": float(duracao_s or (segmentos[-1]["fim"] if segmentos else 0)),
        "segmentos": segmentos,
        "backend": "youtube:auto" if automatica else "youtube:manual",
    }
