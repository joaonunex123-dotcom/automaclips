"""Fecha o laço: o que performou passa a mandar no que será escolhido.

Quatro recalibrações, cada uma alimentando um ponto que as etapas anteriores
deixaram aberto de propósito:

    exemplos_few_shot  -> highlight_detect.montar_sistema(exemplos)
    canais_ruins       -> sourcing/canais.json (ativo: false)
    duracao_ideal      -> select_clips, via tabela `calibracao`
    pesos_por_horario  -> publish/scheduler.pesos_do_historico

**Toda recalibração tem um mínimo de amostras, e abaixo dele ela não
acontece.** Não é cautela genérica: recalibrar com três clips não aprende nada
e ainda estraga o que estava funcionando. Sem dado suficiente, o default do
settings continua valendo — o sistema fica exatamente como estava antes desta
etapa existir.

**Desempenho é views por hora**, não views cruas, pelo mesmo motivo do score de
sourcing: ranquear pelo acumulado premiaria o post mais antigo, que teve mais
tempo para juntar número. E é comparado dentro da MESMA plataforma — YouTube e
Instagram têm escalas de view tão diferentes que misturá-los faria uma
plataforma vencer sempre, independentemente da qualidade do clip.

Um confundidor conhecido e não corrigido: canal de origem grande gera clip com
mais views por mérito do canal, não do trecho. Para escolher CANAL isso é o
sinal certo (é disso que a desativação trata); para aprender o que faz um
TRECHO bom, contamina. `canal_id_fonte` fica gravado em `resultados`
justamente para uma normalização futura por canal.
"""
import json
import logging
import os
from collections import defaultdict

import settings
from db import repositorio

log = logging.getLogger(__name__)


def desempenho(linha, horas_minimas=None):
    """Views por hora de um post. Função pura.

    O piso no denominador é a mesma defesa do score de sourcing: sem ele um
    post de 20 minutos com 50 views marca 150/h e lidera o ranking por ruído
    de amostragem.
    """
    if horas_minimas is None:
        horas_minimas = settings.ANALYTICS_HORAS_MINIMAS
    horas = max(float(linha["horas_publicado"] or 0), horas_minimas)
    return float(linha["views"] or 0) / horas


def _percentil(valores, percentil):
    """Percentil sem numpy — lista pequena, interpolação linear."""
    ordenados = sorted(valores)
    if not ordenados:
        return 0.0
    if len(ordenados) == 1:
        return ordenados[0]
    posicao = (len(ordenados) - 1) * (percentil / 100.0)
    baixo = int(posicao)
    alto = min(baixo + 1, len(ordenados) - 1)
    fracao = posicao - baixo
    return ordenados[baixo] + (ordenados[alto] - ordenados[baixo]) * fracao


def _mediana(valores):
    return _percentil(list(valores), 50.0)


def _por_plataforma(linhas):
    grupos = defaultdict(list)
    for linha in linhas:
        grupos[linha["plataforma"]].append(linha)
    return grupos


def ranquear(linhas, horas_minimas=None):
    """[(desempenho_relativo, linha)] — comparável ENTRE plataformas.

    O desempenho de cada post é dividido pela mediana da própria plataforma,
    então 1.0 significa "típico aqui" nos dois lugares. Sem isso, uma lista
    ordenada por views/hora seria dominada por uma plataforma só.
    """
    relativos = []
    for _plataforma, do_grupo in _por_plataforma(linhas).items():
        valores = [desempenho(l, horas_minimas) for l in do_grupo]
        mediana = _mediana(valores) or 0.0
        for linha, valor in zip(do_grupo, valores):
            # Mediana zero (ninguém teve view nenhuma) faria todo mundo virar
            # infinito; nesse caso o relativo é o próprio valor, e a comparação
            # continua funcionando dentro do grupo.
            relativos.append((valor / mediana if mediana else valor, linha))
    relativos.sort(key=lambda par: -par[0])
    return relativos


# --- 1. exemplos few-shot -----------------------------------------------------

def exemplos_few_shot(conn, percentil=None, maximo=None, minimo_clips=None,
                      idade_minima_h=None):
    """Os clips do decil superior, no formato que montar_sistema espera.

    Devolve [] enquanto não houver clips medidos suficientes — e [] é
    exatamente o que o prompt já sabe receber: sem exemplos, ele volta a ser o
    prompt original. Alimentar few-shot com dois clips ensinaria o modelo a
    imitar o acaso.
    """
    percentil = settings.RECALIBRAR_PERCENTIL_TOPO if percentil is None else percentil
    maximo = settings.RECALIBRAR_MAX_EXEMPLOS if maximo is None else maximo
    if minimo_clips is None:
        minimo_clips = settings.RECALIBRAR_MIN_CLIPS
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    linhas = repositorio.ultimos_resultados(conn, idade_minima_h)
    if len(linhas) < minimo_clips:
        log.info("Few-shot: %d clips medidos, mínimo é %d — mantendo o prompt "
                 "sem exemplos.", len(linhas), minimo_clips)
        return []

    relativos = ranquear(linhas)
    corte = _percentil([r for r, _l in relativos], percentil)
    topo = [linha for relativo, linha in relativos if relativo >= corte]

    exemplos = []
    for linha in topo[:maximo]:
        hook = (linha["hook_text"] or "").strip()
        motivo = (linha["motivo"] or "").strip()
        if hook or motivo:
            exemplos.append({"hook_text": hook, "motivo": motivo})
    log.info("Few-shot: %d exemplos do topo de %d clips.", len(exemplos), len(linhas))
    return exemplos


# --- 2. canais que não rendem -------------------------------------------------

def canais_ruins(conn, minimo_clips=None, fracao=None, idade_minima_h=None):
    """[(canal_id, canal_nome, motivo)] dos canais consistentemente fracos.

    Compara a MEDIANA do canal contra a mediana geral, não a média: um único
    clip que viralizou levantaria a média de um canal que não rende, e é
    justamente o canal que rende uma vez a cada vinte que se quer desligar.
    """
    if minimo_clips is None:
        minimo_clips = settings.RECALIBRAR_MIN_CLIPS_CANAL
    fracao = settings.RECALIBRAR_FRACAO_CANAL_RUIM if fracao is None else fracao
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    relativos = ranquear(repositorio.ultimos_resultados(conn, idade_minima_h))
    if not relativos:
        return []

    por_canal = defaultdict(list)
    nomes = {}
    for relativo, linha in relativos:
        canal_id = linha["canal_id_fonte"] or ""
        if not canal_id:
            continue
        por_canal[canal_id].append(relativo)
        nomes[canal_id] = linha["canal_fonte"] or canal_id

    geral = _mediana([r for r, _l in relativos])
    if not geral:
        return []

    ruins = []
    for canal_id, valores in por_canal.items():
        if len(valores) < minimo_clips:
            continue
        mediana = _mediana(valores)
        if mediana < geral * fracao:
            ruins.append((
                canal_id, nomes[canal_id],
                f"mediana {mediana:.2f} contra {geral:.2f} geral, "
                f"em {len(valores)} clips",
            ))
    return ruins


def desativar_canais(conn, canais, caminho=None, simular=False):
    """Marca `ativo: false` no canais.json. Devolve os ids desativados.

    Desativa em vez de remover: a linha fica no arquivo com o histórico
    intacto, e reativar é trocar uma palavra. Remover apagaria a informação de
    que aquele canal já foi avaliado — e ele voltaria na próxima vez que
    alguém montasse a lista de memória.
    """
    if not canais:
        return []
    caminho = caminho or settings.CANAIS_PATH
    if not os.path.exists(caminho):
        log.warning("Sem %s para desativar canal nenhum.", caminho)
        return []

    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)

    alvos = {canal_id for canal_id, _nome, _motivo in canais}
    motivos = {canal_id: motivo for canal_id, _nome, motivo in canais}
    desativados = []
    for entrada in dados.get("canais") or []:
        if entrada.get("id") in alvos and entrada.get("ativo", True):
            entrada["ativo"] = False
            entrada["_desativado_porque"] = motivos[entrada["id"]]
            desativados.append(entrada["id"])

    if desativados and not simular:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=2)
            f.write("\n")
    if desativados:
        log.warning("%s%d canal(is) desativado(s): %s",
                    "[simulação] " if simular else "", len(desativados),
                    ", ".join(desativados))
    return desativados


# --- 3. duração ideal ---------------------------------------------------------

def duracao_ideal(conn, faixa_s=None, minimo_por_faixa=None, idade_minima_h=None):
    """(min, max) da faixa de duração que melhor performou, ou None.

    Usa retenção quando a plataforma informa e cai em views/hora quando não —
    o que é pior, porque views/hora mede o alcance e não o quanto o clip
    segurou, mas continua sendo medição e não palpite.
    """
    faixa_s = settings.RECALIBRAR_FAIXA_DURACAO_S if faixa_s is None else faixa_s
    if minimo_por_faixa is None:
        minimo_por_faixa = settings.RECALIBRAR_MIN_CLIPS_FAIXA
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    linhas = repositorio.ultimos_resultados(conn, idade_minima_h)
    if not linhas:
        return None

    tem_retencao = any(l["retencao"] is not None for l in linhas)
    relativos = {id(l): r for r, l in ranquear(linhas)}

    faixas = defaultdict(list)
    for linha in linhas:
        duracao = float(linha["trecho_duracao_s"] or 0)
        if duracao <= 0:
            continue
        inicio = int(duracao // faixa_s) * faixa_s
        valor = (linha["retencao"] if tem_retencao and linha["retencao"] is not None
                 else relativos.get(id(linha), 0.0))
        faixas[inicio].append(valor)

    comparaveis = {
        inicio: sum(valores) / len(valores)
        for inicio, valores in faixas.items()
        if len(valores) >= minimo_por_faixa
    }
    if not comparaveis:
        log.info("Duração: nenhuma faixa com %d clips — mantendo o settings.",
                 minimo_por_faixa)
        return None

    melhor = max(comparaveis, key=comparaveis.get)
    log.info("Duração: melhor faixa %.0f–%.0fs (%s), sobre %d faixas comparáveis.",
             melhor, melhor + faixa_s,
             "retenção" if tem_retencao else "views/hora", len(comparaveis))
    return melhor, melhor + faixa_s


# --- 4. pesos de horário ------------------------------------------------------

def pesos_por_horario(conn, minimo_posts=None, idade_minima_h=None):
    """{(hora, minuto): peso} para o scheduler ordenar os horários.

    O peso é o desempenho relativo médio dos posts que saíram naquele horário.
    Horário com poucos posts fica de fora em vez de entrar com peso frágil: um
    único clip que viralizou às 3 da manhã não é evidência de que 3 da manhã
    funciona.
    """
    if minimo_posts is None:
        minimo_posts = settings.RECALIBRAR_MIN_POSTS_HORARIO
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    relativos = ranquear(repositorio.ultimos_resultados(conn, idade_minima_h))
    por_horario = defaultdict(list)
    for relativo, linha in relativos:
        hora = linha["hora_publicado"]
        if hora is None:
            continue
        por_horario[(int(hora), int(linha["minuto_publicado"] or 0))].append(relativo)

    return {
        horario: sum(valores) / len(valores)
        for horario, valores in por_horario.items()
        if len(valores) >= minimo_posts
    }


# --- lições em texto (OpenRouter) ---------------------------------------------

_SISTEMA_LICOES = """\
Você compara clips que performaram bem com clips que performaram mal e escreve \
o que os separa.

Recebe dois grupos, cada um com o gancho de abertura e o motivo pelo qual o \
trecho foi escolhido. Os números de performance já foram calculados — não \
repita nem cite números.

Escreva no máximo três frases, no idioma dos ganchos, dizendo o que os trechos \
do primeiro grupo têm que os do segundo não têm. Se os dois grupos parecerem \
iguais em tudo que importa, diga isso — não invente um padrão para preencher \
espaço.

Responda com um objeto JSON, e nada além dele:
{
  "licoes": "..."
}"""


def licoes_do_historico(conn, cliente=None, modelo=None, minimo_clips=None,
                        idade_minima_h=None):
    """Uma frase curta sobre o que está funcionando, para entrar no prompt.

    Roda no OpenRouter (MODEL_RECALIBRATE): é trabalho de menor exigência, e o
    Claude continua reservado para escolher o trecho.

    Devolve '' quando não há dado suficiente — e '' não vai para o prompt.
    """
    import llm_client

    modelo = modelo or settings.MODEL_RECALIBRATE
    if minimo_clips is None:
        minimo_clips = settings.RECALIBRAR_MIN_CLIPS
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H

    relativos = ranquear(repositorio.ultimos_resultados(conn, idade_minima_h))
    if len(relativos) < minimo_clips:
        return ""

    metade = max(3, len(relativos) // 4)
    def _descrever(fatia):
        return "\n".join(
            f"- \"{(l['hook_text'] or '').strip()}\" — {(l['motivo'] or '').strip()}"
            for _r, l in fatia
        )

    contexto = (
        "Grupo que performou BEM:\n" + _descrever(relativos[:metade]) +
        "\n\nGrupo que performou MAL:\n" + _descrever(relativos[-metade:])
    )

    try:
        dados = llm_client.call_llm(
            contexto, model=modelo, system=_SISTEMA_LICOES, expect_json=True,
            fallback_model=settings.MODEL_FALLBACK, cliente=cliente,
        )
    except llm_client.ErroLLM as e:
        # Lições são um extra: sem elas o prompt continua com os few-shot, que
        # são o sinal forte. Derrubar a recalibração inteira por causa disso
        # custaria as outras três.
        log.warning("Lições não geradas: %s", e)
        return ""
    return str((dados or {}).get("licoes") or "").strip()


# --- orquestração -------------------------------------------------------------

def recalibrar(conn, simular=False, cliente_llm=None):
    """Roda as quatro recalibrações. Devolve o que mudou."""
    resumo = {"exemplos": 0, "canais_desativados": [], "duracao": None,
              "horarios": 0, "licoes": ""}

    exemplos = exemplos_few_shot(conn)
    resumo["exemplos"] = len(exemplos)

    ruins = canais_ruins(conn)
    resumo["canais_desativados"] = desativar_canais(conn, ruins, simular=simular)

    faixa = duracao_ideal(conn)
    if faixa and not simular:
        minimo, maximo = faixa
        motivo = f"faixa de melhor desempenho em {repositorio.contar_resultados(conn)} medições"
        repositorio.salvar_calibracao(
            conn, settings.CALIBRACAO_DURACAO_MIN, minimo,
            amostras=repositorio.contar_resultados(conn), motivo=motivo,
        )
        repositorio.salvar_calibracao(
            conn, settings.CALIBRACAO_DURACAO_MAX, maximo,
            amostras=repositorio.contar_resultados(conn), motivo=motivo,
        )
    resumo["duracao"] = faixa

    resumo["horarios"] = len(pesos_por_horario(conn))

    licoes = licoes_do_historico(conn, cliente=cliente_llm)
    if licoes and not simular:
        repositorio.salvar_calibracao(
            conn, settings.CALIBRACAO_LICOES, licoes,
            amostras=repositorio.contar_resultados(conn),
        )
    resumo["licoes"] = licoes
    return resumo
