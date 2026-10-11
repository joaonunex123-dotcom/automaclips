"""Compara os layouts A/B pelo que renderam.

    python -m analytics.analisar     # a seção "layouts" do resumo

Métrica: views por hora do post MAIS RECENTEMENTE medido, dividida pela
mediana da própria plataforma (`recalibrate.ranquear`). Dividir por plataforma
é o que torna YouTube, Instagram e TikTok comparáveis — sem isso, a variante
que por acaso caiu mais vezes no YouTube pareceria melhor. 1.00 = típico.

Duas honestidades embutidas:

* Todo número sai com o n ao lado, e variante com n abaixo de
  `settings.ANALYTICS_MIN_POR_VARIANTE` (15) diz "dado insuficiente" em vez de
  uma média. Views têm cauda longa: dez posts em que um viralizou parecem uma
  vitória que não existe.
* Só se aponta uma variante à frente quando TODAS as comparadas têm n
  suficiente. Comparar uma amostra boa com uma pobre é o caso que mais engana.

O confundidor que fica: o canal-fonte influencia as views tanto quanto o
layout. A atribuição por hash do clip_id mistura os canais entre as variantes,
mas com n pequeno isso não se equilibra — outro motivo para o mínimo.
"""
from collections import defaultdict

import settings
from analytics import recalibrate
from db import repositorio


def comparar(conn, minimo=None, idade_minima_h=None):
    """[{variante, n, mediana, media, suficiente}], melhor mediana primeiro.

    Variantes com n insuficiente vão ao fim, ordenadas por n.
    """
    if minimo is None:
        minimo = settings.ANALYTICS_MIN_POR_VARIANTE
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    linhas = repositorio.ultimos_resultados(conn, idade_minima_h)
    grupos = defaultdict(list)
    for relativo, linha in recalibrate.ranquear(linhas):
        grupos[(linha["variante"] or "full")].append(relativo)

    saida = []
    for variante, valores in grupos.items():
        n = len(valores)
        saida.append({
            "variante": variante,
            "n": n,
            "mediana": recalibrate._mediana(valores),
            "media": sum(valores) / n,
            "suficiente": n >= minimo,
        })
    saida.sort(key=lambda v: (not v["suficiente"],
                              -v["mediana"] if v["suficiente"] else -v["n"]))
    return saida


def formatar(comparacao, minimo=None):
    """Linhas de texto para o resumo do analisar."""
    if minimo is None:
        minimo = settings.ANALYTICS_MIN_POR_VARIANTE
    linhas = ["--- layouts (A/B, views/hora relativo à plataforma) ---"]
    if not comparacao:
        linhas.append("  nenhum post medido ainda")
        return linhas

    for v in comparacao:
        if v["suficiente"]:
            linhas.append(
                f"  {v['variante']:<20} n={v['n']:<4} mediana {v['mediana']:.2f}x"
                f"  média {v['media']:.2f}x"
            )
        else:
            linhas.append(
                f"  {v['variante']:<20} n={v['n']:<4} dado insuficiente "
                f"(mínimo {minimo})"
            )

    if len(comparacao) < 2:
        linhas.append("  só uma variante medida: nada a comparar")
    elif all(v["suficiente"] for v in comparacao):
        melhor, pior = comparacao[0], comparacao[-1]
        linhas.append(
            f"  à frente: {melhor['variante']} "
            f"({melhor['mediana'] / pior['mediana']:.2f}x a mediana de "
            f"{pior['variante']})" if pior["mediana"] else
            f"  à frente: {melhor['variante']}"
        )
    else:
        linhas.append("  sem veredito: falta amostra em alguma variante")
    return linhas
