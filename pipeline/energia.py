"""Picos de energia do áudio — a confirmação objetiva do palpite do Claude.

O Claude escolhe trechos lendo TEXTO, e texto não carrega reação: uma frase
que parece morna transcrita pode ser a que fez a mesa inteira rir, e uma que
parece ótima pode ter sido dita em tom monótono. A envoltória de energia do
áudio é o sinal que falta — risada, grito, palma e corte de edição são todos
saltos de RMS.

Divisão do módulo, e a razão dela: só `carregar_audio` toca o librosa. Todo o
cálculo (RMS, limiar, seleção de picos) é numpy puro sobre um array, então a
regra que decide o que é pico é testável com um sinal sintético de dez linhas,
sem áudio, sem arquivo e sem a dependência instalada.
"""
import logging

import numpy as np

import settings

log = logging.getLogger(__name__)


class ErroEnergia(Exception):
    """Falha ao ler ou analisar o áudio."""


def carregar_audio(caminho, sample_rate=None):
    """Devolve (sinal_mono_float, sample_rate). Único ponto que usa librosa."""
    sample_rate = sample_rate or settings.AUDIO_SAMPLE_RATE
    try:
        import librosa
    except ImportError as e:  # pragma: no cover - ambiente sem librosa
        raise ErroEnergia(
            "librosa não instalado (pip install -r requirements.txt)."
        ) from e
    try:
        y, sr = librosa.load(caminho, sr=sample_rate, mono=True)
    except Exception as e:
        raise ErroEnergia(f"falha ao ler {caminho}: {e}") from e
    return y, sr


def rms(sinal, sample_rate, janela_s=None):
    """Envoltória de energia: (valores_rms, instante_central_de_cada_quadro).

    Quadros sem sobreposição. Sobreposição suavizaria a curva, mas aqui o que
    interessa é justamente o salto abrupto — e o custo de memória de uma janela
    deslizante sobre 4 h de áudio não se paga.
    """
    janela_s = janela_s or settings.ENERGIA_JANELA_S
    sinal = np.asarray(sinal, dtype=np.float64)
    n = max(1, int(sample_rate * janela_s))
    total = len(sinal) // n
    if total == 0:
        return np.array([]), np.array([])
    quadros = sinal[: total * n].reshape(total, n)
    valores = np.sqrt(np.mean(quadros ** 2, axis=1))
    tempos = (np.arange(total) * n + n / 2.0) / sample_rate
    return valores, tempos


def detectar_picos(valores, tempos, percentil=None, distancia_minima_s=None):
    """Instantes (em segundos, crescentes) dos picos de energia.

    Limiar RELATIVO (percentil do próprio vídeo) e não absoluto: canal com
    áudio comprimido a -3 dB e canal gravado no celular não compartilham
    nenhum valor de RMS que signifique "alto" nos dois.

    A distância mínima é o que faz a contagem medir MOMENTOS: sem ela uma única
    gargalhada de três segundos vira doze picos, e um trecho com uma reação
    grande pontuaria como um trecho com doze reações.
    """
    percentil = settings.ENERGIA_PERCENTIL if percentil is None else percentil
    if distancia_minima_s is None:
        distancia_minima_s = settings.ENERGIA_DISTANCIA_MINIMA_S

    valores = np.asarray(valores, dtype=np.float64)
    tempos = np.asarray(tempos, dtype=np.float64)
    if valores.size == 0:
        return []

    # Sinal sem faixa dinâmica (silêncio, tom contínuo, áudio corrompido) não
    # tem pico algum. Sem esta guarda o percentil de um array constante deixa
    # TODO quadro acima do limiar, e o vídeo inteiro viraria um pico só.
    if not np.isfinite(valores).any() or np.ptp(valores) <= 0:
        return []

    limiar = np.percentile(valores, percentil)
    candidatos = np.flatnonzero(valores >= limiar)
    if candidatos.size == 0:
        return []

    # Guloso do mais alto para o mais baixo: entre dois candidatos próximos
    # demais fica o mais energético, que é o centro real da reação.
    ordem = candidatos[np.argsort(valores[candidatos])[::-1]]
    escolhidos = []
    for i in ordem:
        t = float(tempos[i])
        if all(abs(t - anterior) >= distancia_minima_s for anterior in escolhidos):
            escolhidos.append(t)
    return sorted(escolhidos)


def picos_do_audio(caminho, sample_rate=None, janela_s=None, percentil=None,
                   distancia_minima_s=None, carregar=None):
    """Do arquivo aos instantes de pico. `carregar` injetável nos testes."""
    carregar = carregar or carregar_audio
    sinal, sr = carregar(caminho, sample_rate)
    valores, tempos = rms(sinal, sr, janela_s)
    picos = detectar_picos(valores, tempos, percentil, distancia_minima_s)
    log.info("%d picos de energia em %s.", len(picos), caminho)
    return picos


def picos_em(picos, inicio_s, fim_s, relativos=False):
    """Picos dentro de [inicio_s, fim_s).

    Com `relativos=True` o instante volta contado a partir de inicio_s — que é
    o que a etapa 4 precisa para posicionar o efeito sonoro dentro do clip
    recortado, e não dentro do vídeo-fonte.
    """
    dentro = [t for t in picos if inicio_s <= t < fim_s]
    return [t - inicio_s for t in dentro] if relativos else dentro


def contar_picos_em(picos, inicio_s, fim_s):
    """Quantos picos caem em [inicio_s, fim_s)."""
    return len(picos_em(picos, inicio_s, fim_s))
