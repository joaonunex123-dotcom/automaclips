"""Gera os efeitos sonoros do template (whoosh, ding, pop) e os pads ambiente
por categoria (pad_<categoria>.wav) sem baixar nada.

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

# Camas ambiente por categoria (efeitos.pad). Loop de 24 s sem emenda: toda
# frequência é múltipla de 1/24 Hz, então termina exatamente na fase em que
# começa. -28 LUFS: 12 dB sob a voz normalizada em -16.
PAD_DURACAO_S = 24
PAD_LUFS = -28.0
PADS = {
    #            notas (Hz)                       LFO (ciclos por loop)
    "full": ((98.0, 146.8, 196.0), (1, 2)),
    "slime": ((130.8, 196.0, 246.9, 293.7), (2, 3)),
    "satisfatorio": ((174.6, 220.0, 261.6, 329.6), (1, 3)),
    "parkour": ((110.0, 164.8, 220.0, 329.6), (3, 4)),
    "asmr_geometrico": ((146.8, 220.0, 293.7, 370.0), (1, 2)),
}


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


def shutter():
    """Clique de obturador: fecha (seco) e reabre (mais fraco), com tum grave.

    Dois transientes de ruído passa-banda 1,5-6 kHz a 55 ms um do outro e uma
    ressonância curta em 180 Hz — o "tchk-tk" de uma câmera mecânica.
    """
    rng = np.random.default_rng(7)
    dur = 0.16
    n = int(TAXA * dur)
    t = np.arange(n) / TAXA
    sos = signal.butter(3, [1500, 6000], btype="band", fs=TAXA, output="sos")

    def estalo(inicio_s, ganho, tau):
        x = np.zeros(n)
        i0 = int(TAXA * inicio_s)
        seg = signal.sosfilt(sos, rng.standard_normal(n - i0))
        x[i0:] = seg * np.exp(-np.arange(n - i0) / TAXA / tau)
        return ganho * x

    x = estalo(0.0, 1.0, 0.006) + estalo(0.055, 0.55, 0.008)
    x += 0.5 * np.sin(2 * np.pi * 180 * t) * np.exp(-t * 90)
    return _fade(x, 0.0003, 0.02)


def impacto():
    """Impacto grave: queda de 120 a 38 Hz com cauda e um estalo de ataque."""
    rng = np.random.default_rng(11)
    dur = 0.6
    t = np.arange(int(TAXA * dur)) / TAXA
    freq = 38 + 82 * np.exp(-t * 14)
    fase = 2 * np.pi * np.cumsum(freq) / TAXA
    corpo = np.sin(fase) * np.exp(-t * 7.5)
    sos = signal.butter(2, 400, btype="low", fs=TAXA, output="sos")
    sopro = signal.sosfilt(sos, rng.standard_normal(len(t))) * np.exp(-t * 40)
    return _fade(corpo + 0.8 * sopro, 0.0005, 0.05)


def riser():
    """Subida de 0,8 s: ruído filtrado que abre de 300 Hz a 5 kHz + tom que sobe.

    O instante de ataque é o FIM do som: a intenção é que ele "chegue" no
    efeito visual, então a âncora fica em `RISER_S` e o render atrasa menos.
    """
    rng = np.random.default_rng(13)
    dur = 0.8
    n = int(TAXA * dur)
    t = np.arange(n) / TAXA
    ruido = rng.standard_normal(n)
    saida = np.zeros(n)
    bloco = 512
    centros = np.geomspace(300, 5000, n // bloco + 1)
    zi = None
    for i, c in enumerate(centros):
        ini, fim = i * bloco, min((i + 1) * bloco, n)
        if ini >= fim:
            break
        sos = signal.butter(2, [c * 0.7, c * 1.3], btype="band", fs=TAXA,
                            output="sos")
        if zi is None:
            zi = np.zeros((sos.shape[0], 2))
        saida[ini:fim], zi = signal.sosfilt(sos, ruido[ini:fim], zi=zi)
    env = (t / dur) ** 2
    freq = 200 * (1200 / 200) ** (t / dur)
    tom = np.sin(2 * np.pi * np.cumsum(freq) / TAXA)
    x = env * (saida / max(np.std(saida), 1e-9) * 0.6 + 0.35 * tom)
    return _fade(x, 0.01, 0.004)


def ancora_do_sinal(x, janela_s=0.01):
    """Instante (s) do pico da envoltória RMS: onde o som "bate".

    É o que o render alinha ao momento-chave do efeito visual. Para clique,
    pop e impacto é ~0; para o riser, o fim; para o whoosh, o meio.
    """
    j = max(1, int(TAXA * janela_s))
    n = len(x) // j
    if n == 0:
        return 0.0
    rms = np.sqrt(np.mean(np.reshape(x[:n * j], (n, j)) ** 2, axis=1))
    return float((np.argmax(rms) + 0.5) * j / TAXA)


SONS = {"whoosh": whoosh, "ding": ding, "pop": pop, "shutter": shutter,
        "impacto": impacto, "riser": riser}
NOMES = tuple(SONS)
# Os que o render usa para os efeitos visuais (editing/efeitos.py).
SONS_VISUAIS = ("whoosh", "shutter", "pop", "impacto", "riser")


def pad(notas, lfos, semente=0):
    """Acorde de senos com tremolo lento, periódico em PAD_DURACAO_S."""
    t = _t(PAD_DURACAO_S)
    rng = np.random.default_rng(100 + semente)
    x = np.zeros_like(t)
    for i, f in enumerate(notas):
        f = round(f * PAD_DURACAO_S) / PAD_DURACAO_S      # ciclos inteiros
        lfo = lfos[i % len(lfos)]
        fase = rng.uniform(0, 2 * np.pi)
        trem = 0.6 + 0.4 * np.sin(2 * np.pi * lfo * t / PAD_DURACAO_S + fase)
        x += trem * (np.sin(2 * np.pi * f * t + fase)
                     + 0.25 * np.sin(2 * np.pi * 2 * f * t))
    return x


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


def normalizar_para(x, alvo_lufs):
    y = x * 10 ** ((alvo_lufs - lufs_sem_gating(x)) / 20)
    limite = 10 ** (PICO_MAX_DBFS / 20)
    pico = np.max(np.abs(y))
    return y * (limite / pico) if pico > limite else y


def gerar_pads(diretorio):
    os.makedirs(diretorio, exist_ok=True)
    relatorio = []
    for i, (categoria, (notas, lfos)) in enumerate(PADS.items()):
        y = normalizar_para(pad(notas, lfos, semente=i), PAD_LUFS)
        caminho = os.path.join(diretorio, f"pad_{categoria}.wav")
        sf.write(caminho, y, TAXA, subtype="PCM_16")
        relatorio.append({
            "arquivo": caminho, "duracao_s": len(y) / TAXA,
            "lufs": lufs_sem_gating(y),
            "pico_dbfs": 20 * np.log10(np.max(np.abs(y))),
        })
    return relatorio


def gerar_um(nome, diretorio):
    """Escreve só `nome`.wav (se ainda não existir). Devolve o caminho."""
    os.makedirs(diretorio, exist_ok=True)
    caminho = os.path.join(diretorio, f"{nome}.wav")
    if not os.path.exists(caminho):
        sf.write(caminho, normalizar(SONS[nome]()), TAXA, subtype="PCM_16")
    return caminho


def ancora_do_arquivo(nome):
    """Âncora (s) do som `nome`, medida no sinal normalizado."""
    return ancora_do_sinal(normalizar(SONS[nome]()))


def gerar(diretorio):
    os.makedirs(diretorio, exist_ok=True)
    relatorio = []
    for nome, fn in SONS.items():
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
    for r in gerar(destino) + gerar_pads(destino):
        print(f"{os.path.basename(r['arquivo']):<10} {r['duracao_s'] * 1000:6.0f} ms"
              f"  {r['lufs']:6.1f} LUFS  pico {r['pico_dbfs']:5.1f} dBFS")
