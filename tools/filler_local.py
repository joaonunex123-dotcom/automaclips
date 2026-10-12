"""Vídeos de teste do layout split com os fillers reais (assets/filler/).

    python tools/filler_local.py --fonte ARQUIVO.mp4 [--inicio 20] [--duracao 28]
        [--saida teste_local/filler]

Uso INTERNO de bancada, como tools/comparativo_local.py (que fornece os
componentes): a fonte é lida do caminho original, nunca copiada nem
versionada; nada é transcrito, baixado nem publicado.

Saída em --saida: <categoria>_split.mp4, subway_split_com_efeitos.mp4,
comparativo.mp4 (grade), frames/ (t=8 s) e relatorio.json (metadados e LUFS).
"""
import argparse
import json
import logging
import os
import sys

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
sys.path.insert(0, os.path.join(RAIZ, "tools"))

import comparativo_local as cl                    # noqa: E402

log = logging.getLogger("filler_local")

VIDEOS = [
    # (nome do arquivo, variante, com_efeitos)
    ("subway_split", "split:subway", False),
    ("slime_split", "split:slime", False),
    ("culinaria_split", "split:culinaria", False),
    ("satisfatorio_split", "split:satisfatorio_outros", False),
    ("subway_split_com_efeitos", "split:subway", True),
]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--fonte", required=True)
    p.add_argument("--inicio", type=float, default=20.0)
    p.add_argument("--duracao", type=float, default=28.0)
    p.add_argument("--saida", default=os.path.join("teste_local", "filler"))
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    destino = os.path.abspath(args.saida)
    os.makedirs(os.path.join(destino, "frames"), exist_ok=True)
    fonte = os.path.abspath(args.fonte)
    base = cl.config_base()
    picos = cl.picos_locais(fonte, args.inicio, args.duracao, destino)

    relatorio, gerados = {}, []
    for nome, variante, com_efeitos in VIDEOS:
        log.info("renderizando %s ...", nome)
        caminho, info = cl.renderizar_variante(
            base, nome, variante, com_efeitos, fonte, args.inicio,
            args.duracao, destino, picos)
        info.update(cl.metadados(caminho))
        info["lufs"], info["pico_dbfs"] = cl.lufs(caminho)
        info["caminho"] = caminho
        cl.frame_em(caminho, 8, os.path.join(destino, "frames", f"{nome}.png"))
        relatorio[nome] = info
        gerados.append(caminho)
        log.info("  %s %s %.1fs %s LUFS", nome, info["resolucao"],
                 info["duracao_s"], info["lufs"])

    cl.grade(gerados, os.path.join(destino, "comparativo.mp4"))
    with open(os.path.join(destino, "relatorio.json"), "w", encoding="utf-8") as f:
        json.dump(relatorio, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
