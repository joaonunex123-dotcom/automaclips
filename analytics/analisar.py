"""Mede o que foi publicado e recalibra a seleção. Ponto de entrada da etapa 7.

    python -m analytics.analisar              # mede e recalibra
    python -m analytics.analisar --simular    # mostra o que mudaria, sem mudar

Duas metades, nesta ordem: **coletar** puxa as métricas de cada post e anexa
uma medição em `resultados`; **recalibrar** transforma o histórico nos quatro
ajustes que fecham o laço do pipeline.

`--simular` existe porque uma das recalibrações escreve num arquivo do usuário
(desativa canal no `canais.json`) e outra passa a mandar no prompt. Ver o que
mudaria antes de deixar mudar é barato; descobrir depois que o sistema parou de
olhar o seu melhor canal, não.
"""
import argparse
import logging
import sys

import settings
from analytics import coletar as coletar_mod
from analytics import recalibrate
from db import repositorio

log = logging.getLogger(__name__)


def _resumo(conn, medidos, resultado):
    linhas = ["", "--- medição ---"]
    if medidos:
        for plataforma in sorted(medidos):
            linhas.append(f"  {plataforma:<12} {medidos[plataforma]} post(s)")
    else:
        linhas.append("  nenhum post novo para medir")
    total = repositorio.contar_resultados(conn)
    linhas.append(f"  {total} medições no histórico")

    linhas += ["", "--- recalibração ---"]
    if resultado is None:
        linhas.append("  não executada")
        return "\n".join(linhas)

    minimo = settings.RECALIBRAR_MIN_CLIPS
    if resultado["exemplos"]:
        linhas.append(f"  few-shot          {resultado['exemplos']} exemplos "
                      "no prompt do highlight_detect")
    else:
        linhas.append(f"  few-shot          sem dado suficiente "
                      f"(mínimo {minimo} clips medidos)")

    desativados = resultado["canais_desativados"]
    linhas.append(
        f"  canais            {len(desativados)} desativado(s)"
        + (f": {', '.join(desativados)}" if desativados else "")
    )

    if resultado["duracao"]:
        minimo_s, maximo_s = resultado["duracao"]
        linhas.append(f"  duração ideal     {minimo_s:.0f}–{maximo_s:.0f}s")
    else:
        linhas.append("  duração ideal     sem faixa comparável; "
                      f"mantendo {settings.CLIP_DURACAO_MINIMA_S:.0f}–"
                      f"{settings.CLIP_DURACAO_MAXIMA_S:.0f}s")

    if resultado["horarios"]:
        linhas.append(f"  horários          {resultado['horarios']} com peso "
                      "medido; o scheduler passa a preferi-los")
    else:
        linhas.append("  horários          sem dado; a agenda segue a ordem "
                      "do relógio")

    if resultado["licoes"]:
        linhas += ["", "--- o que separou os clips que renderam ---",
                   f"  {resultado['licoes']}"]

    calibrado = repositorio.toda_calibracao(conn)
    if calibrado:
        linhas += ["", "--- valores aprendidos em vigor ---"]
        for linha in calibrado:
            valor = str(linha["valor"])
            linhas.append(
                f"  {linha['chave']:<22} {valor[:48]}"
                f"  ({linha['amostras']} amostras)"
            )
    return "\n".join(linhas)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Mede a performance dos clips publicados e recalibra."
    )
    parser.add_argument("--so-coletar", action="store_true",
                        help="só mede, não recalibra")
    parser.add_argument("--so-recalibrar", action="store_true",
                        help="só recalibra sobre o que já foi medido")
    parser.add_argument("--simular", action="store_true",
                        help="mostra o que mudaria sem gravar nada")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    conn = repositorio.conectar()
    try:
        medidos = {}
        if not args.so_recalibrar:
            try:
                medidos = coletar_mod.coletar(conn)
            except Exception as e:
                # A recalibração ainda roda sobre o que já foi medido antes —
                # dado velho continua sendo dado.
                log.warning("Coleta falhou: %s", e)

        resultado = None
        if not args.so_coletar:
            resultado = recalibrate.recalibrar(conn, simular=args.simular)

        print(_resumo(conn, medidos, resultado))
        if args.simular:
            print("\n--simular: nada foi gravado.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
