"""Layouts A/B: tela cheia ou clip em cima + vídeo de preenchimento embaixo.

Uma VARIANTE é a string que identifica o layout de um render: "full" ou
"split:<categoria>" (ex.: "split:parkour"). É ela que vai gravada em `renders`
e em `resultados`, e que o analytics compara.

Duas regras que este módulo existe para garantir:

* **Um clip, uma variante.** A escolha é função pura de (semente, clip_id) —
  hash, não sorteio —, então reprocessar o mesmo clip cai na mesma variante.
  E `variante_do_clip` devolve a que já foi gravada antes de sortear de novo:
  mudar pesos ou semente depois de um render/publicação não pode fazer o mesmo
  clip sair em dois layouts no canal.

* **Nada vem da internet.** Os preenchimentos são arquivos do usuário em
  assets/filler/<categoria>/. Categoria sem arquivo = variante ignorada, com
  log; o clip cai nas variantes que sobraram (no limite, "full").
"""
import hashlib
import logging
import os
import random
import re
import shutil
import subprocess

log = logging.getLogger(__name__)

FULL = "full"
EXTENSOES_FILLER = (".mp4", ".mov", ".mkv", ".webm", ".m4v")

_RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AVISADAS = set()
_DURACAO = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class ErroLayout(Exception):
    """Preenchimento ausente ou ilegível para um render split."""


# --- variantes ----------------------------------------------------------------

def separar(variante):
    """'split:parkour' -> ('split', 'parkour'); 'full' -> ('full', '')."""
    layout, _, categoria = str(variante or FULL).partition(":")
    return layout, categoria


def normalizar(variante, config):
    """Completa 'split' sem categoria com split.categoria. Função pura."""
    layout, categoria = separar(variante)
    if layout == "split":
        return f"split:{categoria or config['split']['categoria']}"
    return FULL


def pesos(config):
    """{variante: peso} do template, ou {layout: 1} quando não há teste A/B."""
    informados = config["variantes"]["pesos"]
    if not informados:
        return {normalizar(config["layout"], config): 1.0}
    saida = {}
    for nome, peso in informados.items():
        if peso > 0:
            saida[normalizar(nome, config)] = saida.get(
                normalizar(nome, config), 0.0) + float(peso)
    return saida


def diretorio_filler(config, base_dir=None):
    raiz = config["split"]["diretorio"] or ""
    if os.path.isabs(raiz):
        return raiz
    return os.path.join(base_dir or _RAIZ, raiz)


def listar_fillers(config, categoria, base_dir=None):
    """Arquivos de vídeo de assets/filler/<categoria>/, em ordem estável."""
    pasta = os.path.join(diretorio_filler(config, base_dir), categoria)
    if not categoria or not os.path.isdir(pasta):
        return []
    return sorted(
        os.path.join(pasta, nome) for nome in os.listdir(pasta)
        if nome.lower().endswith(EXTENSOES_FILLER)
        and os.path.isfile(os.path.join(pasta, nome))
    )


def variantes_disponiveis(config, base_dir=None):
    """Os pesos do template SEM as variantes split que não têm preenchimento.

    Se nada sobrar (ex.: só split configurado e a pasta vazia), volta
    {"full": 1}: um clip sem layout algum seria pior do que um clip tela cheia.
    """
    disponiveis = {}
    for variante, peso in pesos(config).items():
        layout, categoria = separar(variante)
        if layout == "split" and not listar_fillers(config, categoria, base_dir):
            # Uma vez por processo: a fila chama isto a cada clip.
            if variante not in _AVISADAS:
                _AVISADAS.add(variante)
                log.warning(
                    "Variante %r ignorada: sem arquivos em %s. Ver "
                    "assets/filler/README.md.", variante,
                    os.path.join(diretorio_filler(config, base_dir), categoria),
                )
            continue
        disponiveis[variante] = peso
    if not disponiveis:
        if "*" not in _AVISADAS:
            _AVISADAS.add("*")
            log.warning("Nenhuma variante utilizável; usando 'full'.")
        return {FULL: 1.0}
    return disponiveis


def escolher(clip_id, disponiveis, semente=""):
    """Variante do clip: hash estável e ponderado. Função pura.

    Itera em ordem alfabética para que a ordem do JSON não mude a atribuição.
    """
    if len(disponiveis) == 1:
        return next(iter(disponiveis))
    digest = hashlib.sha256(f"{semente}:{clip_id}".encode("utf-8")).digest()
    ponto = int.from_bytes(digest[:8], "big") / 2 ** 64      # [0, 1)
    total = sum(disponiveis.values())
    acumulado = 0.0
    for variante in sorted(disponiveis):
        acumulado += disponiveis[variante] / total
        if ponto < acumulado:
            return variante
    return sorted(disponiveis)[-1]      # arredondamento de ponto flutuante


def variante_do_clip(conn, clip_id, config, base_dir=None):
    """A variante deste clip: a já gravada, se utilizável; senão, a sorteada."""
    from db import repositorio

    disponiveis = variantes_disponiveis(config, base_dir)
    anterior = repositorio.variante_gravada(conn, clip_id)
    if anterior:
        if anterior == FULL or anterior in disponiveis:
            return anterior
        log.warning(
            "Clip %s já saiu como %r, mas essa variante não tem preenchimento "
            "agora; renderizando 'full'.", clip_id, anterior,
        )
        return FULL
    return escolher(clip_id, disponiveis, config["variantes"].get("semente", ""))


# --- geometria e preenchimento ------------------------------------------------

def alturas(config):
    """(altura_do_clip, altura_do_preenchimento), pares (exigência do x264)."""
    total = int(config["saida"]["altura"])
    clip = int(round(total * float(config["split"]["proporcao_clip"]) / 2.0)) * 2
    return clip, total - clip


def config_do_layout(config, variante):
    """Config efetivo do layout: no split, a legenda usa a posição do split."""
    layout, _ = separar(variante)
    if layout != "split":
        return config
    import copy
    efetivo = copy.deepcopy(config)
    efetivo["legenda"].update(config["split"]["legenda"])
    return efetivo


def duracao_do_arquivo(caminho, ffmpeg="ffmpeg", executar=None):
    """Duração em segundos lida do `ffmpeg -i`. Dispensa o ffprobe.

    `ffmpeg -i arquivo` sem saída sai com erro, de propósito, mas imprime a
    duração no stderr — é tudo de que precisamos.
    """
    executar = executar or subprocess.run
    resultado = executar([ffmpeg, "-hide_banner", "-i", caminho],
                         capture_output=True, text=True)
    achado = _DURACAO.search(resultado.stderr or "")
    if not achado:
        raise ErroLayout(f"não consegui ler a duração de {caminho}")
    horas, minutos, segundos = achado.groups()
    return int(horas) * 3600 + int(minutos) * 60 + float(segundos)


def planejar_filler(config, variante, duracao_clip_s, rng=None, clip_id=0,
                    ffmpeg="ffmpeg", executar=None, base_dir=None):
    """{'caminho', 'inicio_s', 'loop'} do preenchimento deste clip.

    Arquivo e trecho são aleatórios, mas o gerador padrão é semeado pelo
    clip_id: refazer o render do mesmo clip reproduz o mesmo preenchimento.
    Arquivo mais curto que o clip: começa do zero e entra em loop.
    """
    _, categoria = separar(variante)
    arquivos = listar_fillers(config, categoria, base_dir)
    if not arquivos:
        raise ErroLayout(f"sem preenchimento para a variante {variante!r}")
    rng = rng or random.Random(f"{config['variantes'].get('semente', '')}:{clip_id}")

    caminho = rng.choice(arquivos)
    duracao = duracao_do_arquivo(caminho, ffmpeg, executar)
    if duracao <= duracao_clip_s:
        return {"caminho": caminho, "inicio_s": 0.0, "loop": True}
    inicio = rng.uniform(0.0, duracao - duracao_clip_s)
    return {"caminho": caminho, "inicio_s": round(inicio, 3), "loop": False}


def filler_do_layout(config, variante, duracao_clip_s, planejar=None, **kwargs):
    """None para 'full'; o plano do preenchimento para 'split'."""
    if separar(variante)[0] != "split":
        return None
    kwargs.setdefault("ffmpeg", shutil.which("ffmpeg") or "ffmpeg")
    return (planejar or planejar_filler)(
        config, variante, duracao_clip_s, **kwargs)
