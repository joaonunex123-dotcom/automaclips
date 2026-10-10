"""Gera os três efeitos sonoros do template (whoosh, ding, pop) sem baixar nada.

    python -m editing.gerar_sfx [diretorio]      # padrão: assets/sfx

Síntese pura com numpy, determinística (semente fixa): rodar duas vezes dá o
mesmo arquivo. Os .wav ficam em `assets/sfx/`, que o git ignora — o que se
versiona é este gerador, não o binário.

Cada som é normalizado para a mesma sonoridade (K-weighting da BS.1770, sem
gating, sobre o arquivo todo) e limitado a -1 dBFS de pico. Sem isso o "ding"
saía 10 dB mais alto que o "whoosh" no mesmo `volume` do template.
"""
import os
import sys

import numpy as np
import soundfile as sf
from scipy import signal

TAXA = 48000
ALVO_LUFS = -24.0
PICO_MAX_DBFS = -1.0
NOMES = ("whoosh", "ding", "pop")


def _t(duracao_s):
    return np.arange(int(TAXA * duracao_s)) / TAXA


def _fade(x, entrada_s, saida_s):
    n = len(x)
    env = np.ones(n)
    ne, ns = int(TAXA * entrada_s), int(TAXA * saida_s)
    if ne:
        env[:ne] = np.linspace(0.0, 1.0, ne)
    if ns:
        env[-ns:] *= np.linspace(1.0, 0.0, ns)
    return x * env


def whoosh():
    """Ruído filtrado cujo centro sobe de 400 Hz a 3,5 kHz em 350 ms."""
    rng = np.random.default_rng(1)
    dur = 0.35
    ruido = rng.standard_normal(int(TAXA * dur))
    saida = np.zeros_like(ruido)
    bloco = 256
    centros = np.geomspace(400, 3500, len(ruido) // bloco + 1)
    zi = None
    for i, c in enumerate(centros):
        ini, fim = i * bloco, min((i + 1) * bloco, len(ruido))
        if ini >= fim:
            break
        sos = signal.butter(2, [c * 0.6, c * 1.4], btype="band", fs=TAXA,
                            output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        saida[ini:fim], zi = signal.sosfilt(sos, ruido[ini:fim], zi=zi)
    # Envelope em sino: sobe até ~60% e cai — é o que dá a sensação de passar.
    x = np.linspace(0, 1, len(saida))
    return saida * np.sin(np.pi * x ** 0.8) ** 2


def ding():
    """Sino curto: fundamental 1320 Hz + parciais inarmônicas, queda exponencial."""
    dur = 0.45
    t = _t(dur)
    x = (np.sin(2 * np.pi * 1320 * t)
         + 0.45 * np.sin(2 * np.pi * 1320 * 2.76 * t)
         + 0.2 * np.sin(2 * np.pi * 1320 * 5.4 * t))
    x *= np.exp(-t * 9.0)
    return _fade(x, 0.002, 0.03)


def pop():
    """Estalo de bolha: seno que cai de 700 a 180 Hz em 90 ms, ataque imediato."""
    dur = 0.12
    t = _t(dur)
    freq = 180 + 520 * np.exp(-t * 45)
    fase = 2 * np.pi * np.cumsum(freq) / TAXA
    x = np.sin(fase) * np.exp(-t * 38)
    return _fade(x, 0.0005, 0.01)


def lufs_sem_gating(x):
    """Sonoridade K-weighted (BS.1770) sobre o arquivo inteiro, a 48 kHz."""
    pre = signal.lfilter([1.53512485958697, -2.69169618940638, 1.19839281085285],
                         [1.0, -1.69065929318241, 0.73248077421585], x)
    rlb = signal.lfilter([1.0, -2.0, 1.0], [1.0, -1.99004745483398,
                                           0.99007225036621], pre)
    ms = float(np.mean(rlb ** 2))
    return -0.691 + 10 * np.log10(ms) if ms > 0 else float("-inf")


def normalizar(x):
    ganho_db = ALVO_LUFS - lufs_sem_gating(x)
    y = x * 10 ** (ganho_db / 20)
    pico = np.max(np.abs(y))
    limite = 10 ** (PICO_MAX_DBFS / 20)
    if pico > limite:  # som muito esparso: o pico manda, e fica abaixo do alvo
        y *= limite / pico
    return y


def gerar(diretorio):
    os.makedirs(diretorio, exist_ok=True)
    relatorio = []
    for nome, fn in zip(NOMES, (whoosh, ding, pop)):
        y = normalizar(fn())
        caminho = os.path.join(diretorio, f"{nome}.wav")
        sf.write(caminho, y, TAXA, subtype="PCM_16")
        relatorio.append({
            "arquivo": caminho,
            "duracao_s": len(y) / TAXA,
            "lufs": lufs_sem_gating(y),
            "pico_dbfs": 20 * np.log10(np.max(np.abs(y))),
        })
    return relatorio


if __name__ == "__main__":
    destino = sys.argv[1] if len(sys.argv) > 1 else os.path.join("assets", "sfx")
    for r in gerar(destino):
        print(f"{os.path.basename(r['arquivo']):<10} {r['duracao_s'] * 1000:6.0f} ms"
              f"  {r['lufs']:6.1f} LUFS  pico {r['pico_dbfs']:5.1f} dBFS")
