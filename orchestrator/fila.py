"""Fila de clips prontos: reabastece só quando está baixa.

O laço original processava vídeos a cada hora e sempre que houvesse algum
descoberto, gastando download, transcrição e LLM mesmo com semanas de clips já
prontos esperando horário. Com cinco posts por dia, o que importa é manter um
colchão: quando os clips renderizados e ainda não publicados caem abaixo de
`FILA_MINIMA_CLIPS`, o laço pega o próximo vídeo (maior score) e o processa;
acima disso, só termina o que já começou.

Segurança de reinício: nenhum estado vive em memória. A fila é uma consulta ao
clips.db; o vídeo em andamento tem status no banco (`baixado`, `transcrito`) e
é retomado do ponto em que parou na execução seguinte.
"""
import logging

import settings
from db import repositorio
from pipeline import processar

log = logging.getLogger(__name__)

# Em andamento: já custaram dinheiro/tempo, terminar é sempre melhor que parar.
EM_ANDAMENTO = (repositorio.STATUS_BAIXADO, repositorio.STATUS_TRANSCRITO)


def clips_prontos(conn):
    """Clips renderizados e ainda não publicados (nem em falha definitiva).

    Conta o clip que ainda não tem publicação, ou cuja publicação está
    'agendado' ou 'simulado'; um clip com post 'publicado' já saiu da fila.
    """
    linha = conn.execute(
        "SELECT COUNT(*) FROM clips c JOIN renders r ON r.clip_id = c.id"
        " WHERE c.status = ?"
        "   AND NOT EXISTS (SELECT 1 FROM publicacoes p"
        "                   WHERE p.clip_id = c.id AND p.status = ?)"
        "   AND (NOT EXISTS (SELECT 1 FROM publicacoes p WHERE p.clip_id = c.id)"
        "        OR EXISTS (SELECT 1 FROM publicacoes p WHERE p.clip_id = c.id"
        "                   AND p.status IN (?, ?)))",
        (repositorio.CLIP_SELECIONADO, repositorio.PUB_PUBLICADO,
         repositorio.PUB_AGENDADO, repositorio.PUB_SIMULADO),
    ).fetchone()
    return int(linha[0])


def clips_agendados(conn):
    linha = conn.execute(
        "SELECT COUNT(DISTINCT clip_id) FROM publicacoes WHERE status = ?",
        (repositorio.PUB_AGENDADO,)).fetchone()
    return int(linha[0])


def videos_descobertos(conn):
    linha = conn.execute(
        "SELECT COUNT(*) FROM fila_clips WHERE status = ?",
        (repositorio.STATUS_DESCOBERTO,)).fetchone()
    return int(linha[0])


def orcamento_estourado(conn):
    """True quando o gasto acumulado já atingiu ORCAMENTO_USD (0 = sem teto)."""
    teto = settings.ORCAMENTO_USD
    return bool(teto) and repositorio.custo_acumulado(conn) >= teto


def precisa_reabastecer(conn, limiar=None):
    limiar = settings.FILA_MINIMA_CLIPS if limiar is None else limiar
    if not limiar:
        return True          # guarda desligada: comportamento de antes
    return clips_prontos(conn) < limiar


def reabastecer(conn, processar_fila=None, limiar=None, videos_por_ciclo=None):
    """Um passo do pipeline, guiado pela fila. Devolve um dict de contagens.

    1. Orçamento estourado: não faz nada (nem terminar o que está em curso —
       toda etapa seguinte é paga) e avisa.
    2. Sempre termina os vídeos em andamento.
    3. Só inicia vídeo NOVO se a fila de clips prontos está abaixo do limiar,
       até `videos_por_ciclo` por vez.
    """
    processar_fila = processar_fila or processar.processar_fila
    videos_por_ciclo = (settings.FILA_VIDEOS_POR_CICLO
                        if videos_por_ciclo is None else videos_por_ciclo)
    resultado = {}

    if orcamento_estourado(conn):
        log.warning(
            "ORCAMENTO_USD (US$ %.2f) atingido (gasto US$ %.2f): pipeline "
            "parado até o teto subir.", settings.ORCAMENTO_USD,
            repositorio.custo_acumulado(conn))
        return {"orcamento_estourado": 1}

    for estado, contagem in processar_fila(
            conn, estados=EM_ANDAMENTO).items():
        resultado[estado] = resultado.get(estado, 0) + contagem

    prontos = clips_prontos(conn)
    if precisa_reabastecer(conn, limiar):
        log.info("Fila baixa (%d clips prontos < %s): iniciando até %d vídeo(s).",
                 prontos, limiar if limiar is not None
                 else settings.FILA_MINIMA_CLIPS, videos_por_ciclo)
        if orcamento_estourado(conn):       # o que terminou pode ter gasto
            resultado["orcamento_estourado"] = 1
            return resultado
        for estado, contagem in processar_fila(
                conn, estados=(repositorio.STATUS_DESCOBERTO,),
                limite=videos_por_ciclo).items():
            resultado[estado] = resultado.get(estado, 0) + contagem
    else:
        log.info("Fila ok (%d clips prontos >= %d): sem vídeo novo neste ciclo.",
                 prontos, limiar if limiar is not None
                 else settings.FILA_MINIMA_CLIPS)
        resultado["fila_ok"] = prontos
    return resultado
