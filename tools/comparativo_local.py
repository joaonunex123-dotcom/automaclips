"""Renderiza os vídeos de comparação (sem/ com efeitos, full/ split) de um trecho.

    python tools/comparativo_local.py --fonte ARQUIVO.mp4 [--inicio 75]
        [--duracao 30] [--saida teste_local/comparativo]

Uso INTERNO de bancada: a fonte é um arquivo local qualquer e o script nunca a
copia para assets/ nem a versiona (teste_local/ é ignorado pelo git). Não chama
transcrição, LLM nem publicação: a legenda é um texto fixo e genérico, e os
picos de energia vêm de análise local do áudio.

Saída: A..G_*.mp4, frames/ (t=8 s), relatorio.json e comparativo.mp4 (grade
horizontal 6x menor). Os mesmos componentes do pipeline fazem o render
(layout, sfx, efeitos, render.renderizar) — só falta o banco.
"""
import argparse
import copy
import json
import logging
import os
import re
import subprocess
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)

from editing import efeitos as efeitos_mod       # noqa: E402
from editing import layout as layout_mod         # noqa: E402
from editing import legendas as legendas_mod     # noqa: E402
from editing import render as render_mod         # noqa: E402
from editing import sfx as sfx_mod               # noqa: E402
from editing import template as template_mod     # noqa: E402

log = logging.getLogger("comparativo")

# Texto genérico e fixo (sem transcrição). "nossa" e "incrível!" ativam o pop.
LEGENDA = [
    "Este é um teste de legenda para comparar os efeitos",
    "Nossa, olha como o zoom acompanha a fala com suavidade",
    "A barra embaixo mostra o progresso do vídeo inteiro",
    "O som de fundo fica baixo e nunca cobre a voz",
    "Resultado final do comparativo, incrível!",
]

VARIANTES = [
    # (arquivo, variante, com_efeitos)
    ("A_full_sem_efeitos", "full", False),
    ("B_full_com_efeitos", "full", True),
    ("C_split_slime_sem_efeitos", "split:slime", False),
    ("D_split_slime_com_efeitos", "split:slime", True),
    ("E_split_satisfatorio_com_efeitos", "split:satisfatorio", True),
    ("F_split_parkour_com_efeitos", "split:parkour", True),
    ("G_split_asmr_geometrico_com_efeitos", "split:asmr_geometrico", True),
]


def palavras_fixas(duracao_s):
    """Distribui LEGENDA ao longo do clip, uma palavra por intervalo igual."""
    todas = [p for frase in LEGENDA for p in frase.split()]
    inicio_util, fim_util = 1.2, duracao_s - 1.0
    passo = (fim_util - inicio_util) / len(todas)
    return [
        {"inicio": inicio_util + i * passo,
         "fim": inicio_util + (i + 1) * passo, "palavra": p}
        for i, p in enumerate(todas)
    ]


def config_base():
    config = template_mod.carregar()
    config["legenda"]["tamanho"] = 92            # legenda grande
    config["legenda"]["palavras_por_linha"] = 3
    return config


def config_da_variante(base, com_efeitos):
    config = copy.deepcopy(base)
    if com_efeitos:
        ef = config["efeitos"]
        ef["zoom"].update(ativo=True, modo="picos")
        for nome in ("flash", "vinheta", "barra", "voz", "ducking", "pad"):
            ef[nome]["ativo"] = True
        config["sfx"]["ativo"] = True
    return template_mod.validar(config)


def _executar(args):
    return subprocess.run(args, capture_output=True, text=True)


def metadados(caminho):
    texto = _executar(["ffmpeg", "-hide_banner", "-i", caminho]).stderr
    dur = re.search(r"Duration:\s*([\d:.]+)", texto)
    video = re.search(r"Video: .*?, (\d+x\d+)", texto)
    audio = re.search(r"Audio: ([^\n]+)", texto)
    h, m, s = (dur.group(1).split(":") if dur else ("0", "0", "0"))
    return {
        "resolucao": video.group(1) if video else "?",
        "duracao_s": round(int(h) * 3600 + int(m) * 60 + float(s), 2),
        "audio": audio.group(1).strip() if audio else "sem trilha",
    }


def lufs(caminho):
    """Integrated loudness (ebur128) do arquivo, ou None sem áudio."""
    r = _executar(["ffmpeg", "-hide_banner", "-nostats", "-i", caminho,
                   "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    resumo = r.stderr.rsplit("Summary:", 1)[-1]
    achado = re.search(r"I:\s*(-?[\d.]+)\s*LUFS", resumo)
    pico = re.search(r"Peak:\s*(-?[\d.]+)\s*dBFS", resumo)
    return (float(achado.group(1)) if achado else None,
            float(pico.group(1)) if pico else None)


def picos_locais(fonte, inicio, duracao, destino):
    from pipeline import energia
    # O librosa não lê mp4: o trecho vai para um WAV mono temporário.
    wav = os.path.join(destino, "_audio_trecho.wav")
    try:
        r = _executar(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(inicio),
                       "-t", str(duracao), "-i", fonte, "-vn", "-ac", "1",
                       "-ar", "16000", wav])
        if r.returncode != 0:
            raise RuntimeError(r.stderr[-200:])
        return energia.picos_do_audio(wav)
    except Exception as e:      # sem librosa ou áudio ilegível
        log.warning("Sem picos de energia (%s): zoom por intervalo.", e)
        return None


def renderizar_variante(base, nome, variante, com_efeitos, fonte, inicio,
                        duracao, destino, picos):
    config = config_da_variante(base, com_efeitos)
    if com_efeitos and not picos:
        config["efeitos"]["zoom"]["modo"] = "intervalo"
    config_layout = layout_mod.config_do_layout(config, variante)
    palavras = palavras_fixas(duracao)
    ass = legendas_mod.gerar_ass(config_layout, palavras,
                                 hook_text="TESTE DE EFEITOS",
                                 duracao_s=duracao)
    extras = {}
    sons = []
    if com_efeitos:
        biblioteca = sfx_mod.carregar_biblioteca(config)
        sons = sfx_mod.planejar(config, duracao, picos=picos, palavras=palavras,
                                biblioteca=biblioteca)
        categoria = layout_mod.separar(variante)[1] or layout_mod.FULL
        sons = sons + efeitos_mod.plano_pad(config, categoria)
        extras["efeitos"] = {"picos": picos or []}
    plano = layout_mod.filler_do_layout(config, variante, duracao, clip_id=nome)
    if plano:
        extras["filler"] = plano
    caminho = render_mod.renderizar(
        config_layout, fonte, inicio, inicio + duracao, ass, nome,
        destino_dir=destino, sons=sons, **extras,
    )
    ativos = []
    if com_efeitos:
        ef = config["efeitos"]
        ativos = [n for n in ("zoom", "flash", "vinheta", "barra", "voz",
                              "ducking", "pad") if ef[n]["ativo"]]
        ativos.append("sfx")
        ativos[0] = f"zoom({config['efeitos']['zoom']['modo']})"
    return caminho, {"variante": variante, "efeitos": ativos,
                     "filler": plano["caminho"] if plano else None,
                     "sons": len(sons), "picos": len(picos or [])}


def frame_em(caminho, t, destino):
    _executar(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(t), "-i",
               caminho, "-frames:v", "1", destino])


def grade(videos, destino, largura=180, altura=320):
    entradas = []
    for v in videos:
        entradas += ["-i", v]
    n = len(videos)
    filtros = ";".join(f"[{i}:v]scale={largura}:{altura},setsar=1[s{i}]"
                       for i in range(n))
    filtros += ";" + "".join(f"[s{i}]" for i in range(n)) + \
        f"hstack=inputs={n}:shortest=1[v]"
    r = _executar(["ffmpeg", "-y", "-loglevel", "error", *entradas,
                   "-filter_complex", filtros, "-map", "[v]", "-an",
                   "-c:v", "libx264", "-crf", "24", "-pix_fmt", "yuv420p",
                   destino])
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fonte", required=True)
    p.add_argument("--inicio", type=float, default=75.0)
    p.add_argument("--duracao", type=float, default=30.0)
    p.add_argument("--saida", default=os.path.join("teste_local", "comparativo"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    destino = os.path.abspath(args.saida)
    os.makedirs(os.path.join(destino, "frames"), exist_ok=True)
    fonte = os.path.abspath(args.fonte)
    base = config_base()
    picos = picos_locais(fonte, args.inicio, args.duracao, destino)
    log.info("picos de energia no trecho: %s", "n/d" if picos is None else len(picos))

    relatorio = {}
    gerados = []
    for nome, variante, com_efeitos in VARIANTES:
        log.info("renderizando %s ...", nome)
        caminho, info = renderizar_variante(
            base, nome, variante, com_efeitos, fonte, args.inicio,
            args.duracao, destino, picos)
        info.update(metadados(caminho))
        info["lufs"], info["pico_dbfs"] = lufs(caminho)
        info["caminho"] = caminho
        frame_em(caminho, 8, os.path.join(destino, "frames", f"{nome}.png"))
        relatorio[nome] = info
        gerados.append(caminho)
        log.info("  %s  %s  %.1fs  %s LUFS", nome, info["resolucao"],
                 info["duracao_s"], info["lufs"])

    grade(gerados, os.path.join(destino, "comparativo.mp4"))
    with open(os.path.join(destino, "relatorio.json"), "w", encoding="utf-8") as f:
        json.dump(relatorio, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
