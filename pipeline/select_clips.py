"""Da lista bruta do Claude para a fila de edição.

Quatro decisões, nesta ordem, e a ordem importa:

1. **Duração** — o Claude erra a borda com frequência (devolve 25 s ou 80 s
   quando a faixa é 30–60). Ajustar antes de tudo porque a duração final é o
   denominador da densidade de picos no passo seguinte.
2. **Energia** — a confirmação objetiva. O Claude leu texto; o áudio diz se
   houve reação. Vira um FATOR multiplicativo sobre a nota, não uma nota
   separada: energia sozinha não escolhe clip nenhum (uma vinheta é puro pico),
   ela só reforça ou enfraquece o que o Claude já apontou.
3. **Limiar** — corta o que não vale renderizar.
4. **Sobreposição** — dois trechos que dividem o mesmo minuto viram dois clips
   quase iguais no mesmo canal; fica o de maior score.

Nada é jogado fora: tudo que sai vira uma linha com status 'descartado' e o
motivo. É o histórico que a etapa 7 usa para saber se o corte está no lugar —
sem ele, só se sabe como performou o que passou, nunca o que foi barrado.
"""
import logging

import settings
from db import repositorio
from pipeline import energia as energia_mod

log = logging.getLogger(__name__)


def fator_energia(picos, duracao_s, fator_min=None, fator_max=None,
                  densidade_plena=None):
    """Multiplicador da nota conforme a densidade de picos (picos por minuto).

    Interpolação linear de fator_min (nenhum pico) até fator_max (densidade
    igual ou acima de densidade_plena).

    O piso é penalidade, não veto: trecho sem pico costuma ser fala parada, mas
    às vezes é a revelação dita baixinho — que é exatamente o tipo de trecho que
    o Claude acerta e o áudio não vê. Perder 30% da nota deixa um trecho
    excelente ainda competitivo; zerá-lo perderia a melhor escolha do modelo por
    causa do volume.
    """
    fator_min = settings.FATOR_ENERGIA_MIN if fator_min is None else fator_min
    fator_max = settings.FATOR_ENERGIA_MAX if fator_max is None else fator_max
    if densidade_plena is None:
        densidade_plena = settings.DENSIDADE_PICOS_PLENA

    if duracao_s <= 0 or densidade_plena <= 0:
        return fator_min
    densidade = picos / (duracao_s / 60.0)
    proporcao = min(1.0, max(0.0, densidade / densidade_plena))
    return fator_min + proporcao * (fator_max - fator_min)


def ajustar_duracao(inicio_s, fim_s, duracao_video, minima=None, maxima=None):
    """Encaixa o trecho na faixa. Devolve (inicio, fim) ou None se impossível.

    Longo demais: corta o FIM. O gancho está no começo — o Claude foi instruído
    a começar dentro do assunto —, então sobra do lado certo.

    Curto demais: estende para os dois lados igualmente. Um trecho de 25 s com
    2,5 s de respiro em cada ponta continua sendo o mesmo momento; recusá-lo
    perderia uma escolha boa do modelo por 5 segundos.

    None só quando nem esticando cabe (vídeo mais curto que a duração mínima).
    """
    minima = settings.CLIP_DURACAO_MINIMA_S if minima is None else minima
    maxima = settings.CLIP_DURACAO_MAXIMA_S if maxima is None else maxima

    inicio = max(0.0, float(inicio_s))
    fim = float(fim_s)
    if duracao_video and duracao_video > 0:
        fim = min(fim, float(duracao_video))
    if fim <= inicio:
        return None

    if fim - inicio > maxima:
        fim = inicio + maxima

    if fim - inicio < minima:
        falta = minima - (fim - inicio)
        inicio -= falta / 2.0
        fim += falta / 2.0
        # Bateu numa borda do vídeo: o que sobrou de um lado é recuperado do
        # outro. Sem isto, um trecho bom perto do começo (ou do fim) do vídeo
        # sairia curto demais e seria reprovado por segundos que existiam,
        # só não do lado em que se tentou pegá-los.
        if inicio < 0:
            fim += -inicio
            inicio = 0.0
        if duracao_video and duracao_video > 0 and fim > duracao_video:
            inicio = max(0.0, inicio - (fim - duracao_video))
            fim = float(duracao_video)
        if fim - inicio < minima:
            return None

    return inicio, fim


def _sobrepoe(a, b):
    return a["inicio_s"] < b["fim_s"] and b["inicio_s"] < a["fim_s"]


def selecionar(trechos, picos=None, duracao_video=0, threshold=None,
               duracao_minima=None, duracao_maxima=None):
    """Aplica as quatro regras. Devolve a lista pronta para registrar_clips.

    Todos os trechos voltam — os aprovados com status 'selecionado', os demais
    com 'descartado' e motivo_descarte preenchido —, ordenados do melhor
    score_final para o pior.
    """
    threshold = settings.CLIP_SCORE_THRESHOLD if threshold is None else threshold
    picos = picos or []

    avaliados = []
    for trecho in trechos:
        item = dict(trecho)
        faixa = ajustar_duracao(
            item["inicio_s"], item["fim_s"], duracao_video,
            minima=duracao_minima, maxima=duracao_maxima,
        )
        if faixa is None:
            item.update(
                picos_energia=0,
                score_final=0.0,
                status=repositorio.CLIP_DESCARTADO,
                motivo_descarte="duração fora da faixa e não ajustável",
            )
            avaliados.append(item)
            continue

        item["inicio_s"], item["fim_s"] = faixa
        duracao = item["fim_s"] - item["inicio_s"]
        # Guardados relativos ao início do trecho: a etapa 4 posiciona o efeito
        # sonoro dentro do clip recortado, não dentro do vídeo-fonte.
        item["picos_instantes"] = energia_mod.picos_em(
            picos, item["inicio_s"], item["fim_s"], relativos=True
        )
        item["picos_energia"] = len(item["picos_instantes"])
        item["score_final"] = round(
            item["score_claude"] * fator_energia(item["picos_energia"], duracao), 3
        )

        if item["score_final"] < threshold:
            item.update(
                status=repositorio.CLIP_DESCARTADO,
                motivo_descarte=(
                    f"score {item['score_final']:.2f} < {threshold:.2f}"
                ),
            )
        else:
            item.update(status=repositorio.CLIP_SELECIONADO, motivo_descarte="")
        avaliados.append(item)

    # Sobreposição por último: só faz sentido entre trechos que já passaram no
    # limiar, e resolver pelo score_final exige que ele já esteja calculado.
    avaliados.sort(key=lambda i: (-i["score_final"], i["inicio_s"]))
    aceitos = []
    for item in avaliados:
        if item["status"] != repositorio.CLIP_SELECIONADO:
            continue
        conflito = next((a for a in aceitos if _sobrepoe(a, item)), None)
        if conflito is None:
            aceitos.append(item)
        else:
            item.update(
                status=repositorio.CLIP_DESCARTADO,
                motivo_descarte=(
                    f"sobrepõe o trecho {conflito['inicio_s']:.0f}–"
                    f"{conflito['fim_s']:.0f}s, de score maior"
                ),
            )

    selecionados = sum(
        1 for i in avaliados if i["status"] == repositorio.CLIP_SELECIONADO
    )
    log.info("%d de %d trechos selecionados (limiar %.1f).",
             selecionados, len(avaliados), threshold)
    return avaliados
