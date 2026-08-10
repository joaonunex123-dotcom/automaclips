"""Transcrição pela Whisper API da OpenAI — alternativa ao faster-whisper local.

Existe por um motivo prático: sem GPU, transcrever quatro horas de áudio em CPU
leva horas, e o pipeline inteiro fica parado atrás disso. A API devolve em
minutos. O preço é cobrado por minuto de áudio, então o gasto é previsível
antes da chamada — e é isso que torna possível a guarda de orçamento em
`processar.py`, que RECUSA a transcrição que estouraria o teto em vez de
descobrir o saldo vazio no meio da fila.

Modelo: **whisper-1**, e não os modelos de transcrição mais recentes. É o que
aceita `timestamp_granularities=["word"]`, e sem timestamp por palavra a
legenda word-by-word da etapa 3 simplesmente não existe.

Dois obstáculos que ditam quase todo o código abaixo:

1. **Teto de upload.** O wav de trabalho (16 kHz mono) tem ~1,9 MB por minuto:
   treze minutos já batem no limite da API. Por isso o áudio é recomprimido
   para mp3 mono de 32 kbps antes de subir — cerca de 50x menor, e a
   transcrição sai igual, porque 32 kbps é folgado para voz. O wav original
   continua em disco, intacto, para a análise de energia.

2. **Duração.** Mesmo comprimido, um podcast longo passa do teto, então o
   áudio é fatiado. O corte não cai num ponto qualquer: o ffmpeg localiza os
   silêncios e a fatia termina no silêncio mais próximo do alvo. Cortar no meio
   de uma palavra estraga uma palavra por fronteira — e como as fronteiras caem
   em posições arbitrárias, uma delas eventualmente cai dentro de um clip.

Os timestamps de cada fatia voltam relativos ao começo DELA; a soma do
deslocamento acontece na normalização, antes de qualquer coisa ver o resultado.
"""
import json
import logging
import os
import re
import subprocess

import settings

log = logging.getLogger(__name__)

SERVICO = "openai:whisper-1"

_RE_SILENCIO_INICIO = re.compile(r"silence_start:\s*(-?[\d.]+)")
_RE_SILENCIO_FIM = re.compile(r"silence_end:\s*(-?[\d.]+)")


class ErroWhisperAPI(Exception):
    """Falha de configuração, de conversão ou da API."""


def estimar_custo(duracao_s, usd_por_minuto=None):
    """Quanto uma transcrição vai custar, em USD. Função pura.

    Calculável antes da chamada porque o preço é por minuto de áudio, não por
    token — é o que permite decidir se cabe no orçamento sem gastar nada para
    descobrir.
    """
    if usd_por_minuto is None:
        usd_por_minuto = settings.WHISPER_API_USD_POR_MINUTO
    return max(0.0, float(duracao_s)) / 60.0 * usd_por_minuto


def construir_cliente(api_key=None):
    """Cliente da OpenAI. Import local: módulo importável sem o SDK."""
    api_key = api_key if api_key is not None else settings.OPENAI_API_KEY
    if not api_key:
        raise ErroWhisperAPI(
            "OPENAI_API_KEY não configurada. Preencha no .env (ver .env.example), "
            "ou use TRANSCRICAO_BACKEND=local."
        )
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover - ambiente sem o SDK
        raise ErroWhisperAPI(
            "SDK openai não instalado (pip install -r requirements.txt)."
        ) from e
    return OpenAI(api_key=api_key)


# --- preparo do áudio ---------------------------------------------------------

def _ffmpeg(caminho=None):
    from pipeline.download import _garantir_ffmpeg

    return _garantir_ffmpeg(caminho)


def _rodar(executar, comando, o_que):
    resultado = executar(comando, capture_output=True, text=True)
    if resultado.returncode != 0:
        ultima = (resultado.stderr or "").strip().splitlines()
        raise ErroWhisperAPI(
            f"ffmpeg falhou {o_que}: {ultima[-1] if ultima else 'sem stderr'}"
        )
    return resultado


def comprimir(audio_path, destino_dir, bitrate=None, executar=None, ffmpeg=None):
    """wav de trabalho -> mp3 mono, para caber no upload. Devolve o caminho."""
    bitrate = bitrate or settings.OPENAI_MP3_BITRATE
    executar = executar or subprocess.run
    ffmpeg = _ffmpeg(ffmpeg)
    os.makedirs(destino_dir, exist_ok=True)

    nome = os.path.splitext(os.path.basename(audio_path))[0]
    saida = os.path.join(destino_dir, f"{nome}.mp3")
    if os.path.exists(saida) and os.path.getsize(saida) > 0:
        return saida

    _rodar(
        executar,
        [ffmpeg, "-y", "-i", audio_path, "-vn", "-ac", "1",
         "-c:a", "libmp3lame", "-b:a", bitrate, saida],
        f"ao comprimir {audio_path}",
    )
    return saida


def detectar_silencios(audio_path, executar=None, ffmpeg=None, limiar_db=None,
                       minimo_s=None):
    """Trechos de silêncio, como [(inicio, fim), ...].

    Usa o filtro `silencedetect` do ffmpeg em vez de carregar o áudio em
    memória: quatro horas a 16 kHz seriam quase um gigabyte de float na RAM só
    para escolher três pontos de corte.
    """
    executar = executar or subprocess.run
    ffmpeg = _ffmpeg(ffmpeg)
    limiar_db = limiar_db or settings.OPENAI_CORTE_SILENCIO_DB
    if minimo_s is None:
        minimo_s = settings.OPENAI_CORTE_SILENCIO_MIN_S

    resultado = executar(
        [ffmpeg, "-hide_banner", "-nostats", "-i", audio_path,
         "-af", f"silencedetect=noise={limiar_db}:d={minimo_s}", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    # returncode não é conferido: o silencedetect escreve no stderr e o
    # "-f null" às vezes sai diferente de zero em builds antigas. Sem silêncio
    # detectado o corte cai no alvo, que é degradação aceitável — falhar aqui
    # derrubaria a transcrição por causa de uma otimização de fronteira.
    texto = resultado.stderr or ""

    silencios, inicio = [], None
    for linha in texto.splitlines():
        achado = _RE_SILENCIO_INICIO.search(linha)
        if achado:
            inicio = float(achado.group(1))
            continue
        achado = _RE_SILENCIO_FIM.search(linha)
        if achado and inicio is not None:
            silencios.append((inicio, float(achado.group(1))))
            inicio = None
    return silencios


def pontos_de_corte(duracao_s, silencios, max_chunk_s=None, tolerancia_s=None):
    """Instantes onde fatiar. Função pura — o miolo testável desta parte.

    Cada corte busca o silêncio mais próximo do alvo dentro da tolerância; sem
    candidato, corta no alvo mesmo. O filtro `> atual` garante progresso, então
    o laço termina qualquer que seja a lista de silêncios.
    """
    max_chunk_s = max_chunk_s or settings.OPENAI_CHUNK_MAX_S
    if tolerancia_s is None:
        tolerancia_s = settings.OPENAI_CORTE_TOLERANCIA_S

    cortes = []
    atual = 0.0
    while duracao_s - atual > max_chunk_s:
        alvo = atual + max_chunk_s
        candidatos = []
        for inicio, fim in silencios:
            meio = (inicio + fim) / 2.0
            if meio > atual + 1.0 and abs(meio - alvo) <= tolerancia_s:
                candidatos.append(meio)
        corte = min(candidatos, key=lambda t: abs(t - alvo)) if candidatos else alvo
        cortes.append(corte)
        atual = corte
    return cortes


def fatiar(audio_path, cortes, duracao_s, destino_dir, bitrate=None,
           executar=None, ffmpeg=None):
    """Corta o mp3 nos pontos dados. Devolve [(caminho, deslocamento), ...]."""
    if not cortes:
        return [(audio_path, 0.0)]

    bitrate = bitrate or settings.OPENAI_MP3_BITRATE
    executar = executar or subprocess.run
    ffmpeg = _ffmpeg(ffmpeg)
    os.makedirs(destino_dir, exist_ok=True)
    nome = os.path.splitext(os.path.basename(audio_path))[0]

    fronteiras = [0.0] + list(cortes) + [float(duracao_s)]
    fatias = []
    for i in range(len(fronteiras) - 1):
        inicio, fim = fronteiras[i], fronteiras[i + 1]
        saida = os.path.join(destino_dir, f"{nome}.parte{i:02d}.mp3")
        # -ss ANTES de -i (busca rápida) e -t (duração), não -to: com -ss antes
        # da entrada, -to é interpretado de formas diferentes conforme a versão
        # do ffmpeg, e -t é a duração de saída em todas elas.
        _rodar(
            executar,
            [ffmpeg, "-y", "-ss", f"{inicio:.3f}", "-t", f"{fim - inicio:.3f}",
             "-i", audio_path, "-vn", "-ac", "1",
             "-c:a", "libmp3lame", "-b:a", bitrate, saida],
            f"ao fatiar {audio_path} em {inicio:.1f}s",
        )
        fatias.append((saida, inicio))
    return fatias


# --- chamada e normalização ---------------------------------------------------

def _como_dict(resposta):
    """A resposta do SDK como dict, seja modelo pydantic, objeto ou json."""
    for atributo in ("model_dump", "to_dict", "dict"):
        metodo = getattr(resposta, atributo, None)
        if callable(metodo):
            return metodo()
    if isinstance(resposta, dict):
        return resposta
    if isinstance(resposta, str):
        return json.loads(resposta)
    raise ErroWhisperAPI(f"resposta em formato inesperado: {type(resposta)!r}")


def _normalizar(dados, deslocamento=0.0):
    """Resposta da API -> segmentos no formato do nosso .json.

    As palavras vêm numa lista de PRIMEIRO NÍVEL, não dentro de cada segmento
    (ao contrário do faster-whisper), então elas são atribuídas ao segmento
    pelo intervalo de tempo. O deslocamento da fatia é somado aqui, antes de
    qualquer outra coisa ver o resultado.
    """
    palavras = dados.get("words") or []
    segmentos_brutos = dados.get("segments") or []

    if not segmentos_brutos:
        # Sem segmentos: usa o texto inteiro como um só, para não perder a
        # transcrição por causa do formato.
        texto = (dados.get("text") or "").strip()
        if not texto:
            return []
        fim = float(dados.get("duration") or 0)
        segmentos_brutos = [{"start": 0.0, "end": fim, "text": texto}]

    segmentos = []
    for bruto in segmentos_brutos:
        inicio = float(bruto.get("start") or 0.0)
        fim = float(bruto.get("end") or 0.0)
        do_segmento = [
            {
                "inicio": float(p["start"]) + deslocamento,
                "fim": float(p["end"]) + deslocamento,
                "palavra": (p.get("word") or "").strip(),
            }
            for p in palavras
            if p.get("start") is not None and inicio <= float(p["start"]) < fim
        ]
        segmentos.append(
            {
                "inicio": inicio + deslocamento,
                "fim": fim + deslocamento,
                "texto": (bruto.get("text") or "").strip(),
                "palavras": do_segmento,
            }
        )
    return segmentos


def _transcrever_arquivo(cliente, caminho, modelo=None, idioma=None):
    modelo = modelo or settings.OPENAI_WHISPER_MODELO
    argumentos = {
        "model": modelo,
        "response_format": "verbose_json",
        # ["word"] sozinho faz a API omitir os segmentos; pedir os dois é o que
        # dá frase E palavra numa chamada só.
        "timestamp_granularities": ["word", "segment"],
    }
    if idioma:
        argumentos["language"] = idioma
    try:
        with open(caminho, "rb") as arquivo:
            return cliente.audio.transcriptions.create(file=arquivo, **argumentos)
    except ErroWhisperAPI:
        raise
    except Exception as e:
        raise ErroWhisperAPI(f"Whisper API falhou em {os.path.basename(caminho)}: {e}") from e


def transcrever(audio_path, duracao_s=0.0, cliente=None, destino_dir=None,
                modelo=None, idioma=None, executar=None, ffmpeg=None,
                max_chunk_s=None):
    """Transcreve pela API e devolve o mesmo dict do backend local.

    Mesmo contrato de saída de pipeline/transcribe.transcrever, de propósito: o
    resto do pipeline não sabe (nem precisa saber) qual backend rodou.
    """
    if not os.path.exists(audio_path):
        raise ErroWhisperAPI(f"áudio não encontrado: {audio_path}")

    cliente = cliente if cliente is not None else construir_cliente()
    destino_dir = destino_dir or os.path.dirname(audio_path) or "."
    idioma = idioma if idioma is not None else settings.WHISPER_IDIOMA

    mp3 = comprimir(audio_path, destino_dir, executar=executar, ffmpeg=ffmpeg)
    tamanho_mb = os.path.getsize(mp3) / (1024 * 1024)
    log.info("Áudio comprimido para %.1f MB.", tamanho_mb)

    cortes = []
    if tamanho_mb > settings.OPENAI_UPLOAD_MAX_MB and duracao_s > 0:
        silencios = detectar_silencios(mp3, executar=executar, ffmpeg=ffmpeg)
        cortes = pontos_de_corte(duracao_s, silencios, max_chunk_s=max_chunk_s)
        log.info("%.1f MB acima do teto: %d cortes (%d silêncios candidatos).",
                 tamanho_mb, len(cortes), len(silencios))

    fatias = fatiar(mp3, cortes, duracao_s, destino_dir, executar=executar,
                    ffmpeg=ffmpeg)

    segmentos, idioma_detectado = [], ""
    for caminho, deslocamento in fatias:
        dados = _como_dict(_transcrever_arquivo(cliente, caminho, modelo, idioma))
        idioma_detectado = idioma_detectado or (dados.get("language") or "")
        segmentos.extend(_normalizar(dados, deslocamento))

    segmentos.sort(key=lambda s: s["inicio"])
    log.info("Transcrito %s pela API: %d segmentos em %d fatia(s).",
             os.path.basename(audio_path), len(segmentos), len(fatias))
    return {
        "idioma": idioma_detectado,
        "duracao_s": float(duracao_s or (segmentos[-1]["fim"] if segmentos else 0.0)),
        "segmentos": segmentos,
        "backend": SERVICO,
    }
