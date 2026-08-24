"""Puxa a performance real de cada clip publicado.

Roda diariamente e anexa uma linha em `resultados` por post medido. Histórico e
não estado: um clip medido uma vez só não diz se cresceu ou estagnou, e é
exatamente essa diferença que separa um clip que pegou de um que teve um pico
de notificação e morreu.

Três escolhas que economizam credencial e quota:

* **YouTube pela chave de API, não pelo OAuth.** Views, likes e comentários de
  um vídeo público saem do mesmo `videos.list` que o sourcing já usa — 1
  unidade por até 50 vídeos, contra as 1600 de um upload. Não precisa da
  autorização do canal.
* **Retenção fica de fora por padrão.** `averageViewPercentage` só existe na
  YouTube Analytics API, que é outro escopo de OAuth e outra autorização.
  Ligar isso é decisão de quem opera (ANALYTICS_RETENCAO); sem ela a coluna
  fica NULL e a recalibração de duração degrada para views/hora por faixa —
  pior, mas medido.

* **TikTok pelo mesmo access token da publicação**, mas por outro escopo
  (`video.list`) e em lotes de 20 ids por chamada. Quem autorizou o app só
  para publicar continua publicando e não consegue medir — a resposta diz
  `scope_not_authorized`, e o aviso sai no log sem derrubar as outras
  plataformas.

Post 'simulado' nunca é medido: ele não existe em plataforma nenhuma, e
medi-lo devolveria zero para sempre — zeros que entrariam na média e puxariam
a recalibração para baixo. E post do TikTok que saiu PRIVADO (app ainda não
revisado) também não: sem id público não há o que consultar, e o que ficou
gravado foi o publish_id. Ele é pulado com aviso, não com erro.
"""
import logging

import settings
from db import repositorio
from publish import instagram as instagram_mod
from publish import quota
from publish import tiktok as tiktok_mod
from sourcing import youtube as youtube_sourcing

log = logging.getLogger(__name__)

# `videos.list` custa o mesmo do sourcing: 1 unidade por chamada, até 50 ids.
CUSTO_VIDEOS_LIST = 1

# Quem tem coletor aqui. Derivar o aviso de "plataforma sem métrica" desta
# lista, em vez de repetir os nomes lá embaixo, é o que faz o aviso sumir
# sozinho quando a plataforma passa a ser medida.
PLATAFORMAS_MEDIDAS = (settings.PLATAFORMA_YOUTUBE,
                       settings.PLATAFORMA_INSTAGRAM,
                       settings.PLATAFORMA_TIKTOK)


class ErroColeta(Exception):
    """Falha ao puxar métricas."""


def _inteiro(dados, *chaves):
    """Primeiro valor inteiro encontrado entre as chaves. 0 se nenhuma existe.

    As APIs renomeiam métrica com o tempo (view_count -> plays -> views) e
    omitem o campo quando ele é zero. Aceitar vários nomes evita que uma
    renomeação vire uma série histórica de zeros silenciosos.
    """
    for chave in chaves:
        valor = dados.get(chave)
        if valor not in (None, ""):
            try:
                return int(valor)
            except (TypeError, ValueError):
                continue
    return 0


# --- YouTube ------------------------------------------------------------------

def metricas_youtube(cliente, video_ids):
    """{video_id: {views, likes, comentarios}} — 1 unidade por 50 vídeos."""
    saida = {}
    for lote in youtube_sourcing._lotes(list(video_ids)):
        resposta = cliente.videos().list(
            part="statistics", id=",".join(lote),
            maxResults=youtube_sourcing.MAX_POR_CHAMADA,
        ).execute()
        for item in resposta.get("items", []):
            estatisticas = item.get("statistics", {})
            saida[item["id"]] = {
                "views": _inteiro(estatisticas, "viewCount"),
                "likes": _inteiro(estatisticas, "likeCount"),
                "comentarios": _inteiro(estatisticas, "commentCount"),
            }
    faltando = set(video_ids) - set(saida)
    if faltando:
        # Vídeo removido, privado ou derrubado por copyright. Não é erro do
        # pipeline, e a última medição continua valendo.
        log.info("Sem estatísticas para %d vídeo(s): %s",
                 len(faltando), ", ".join(sorted(faltando)))
    return saida


def retencao_youtube(cliente_analytics, video_id):
    """Fração média assistida (0–1), ou None.

    Exige a YouTube Analytics API — outro escopo de OAuth. O cliente é sempre
    injetado; este módulo não sabe construí-lo de propósito, para que ligar a
    retenção seja uma decisão explícita de quem opera.
    """
    if cliente_analytics is None:
        return None
    try:
        resposta = cliente_analytics.reports().query(
            ids="channel==MINE", startDate="2020-01-01", endDate="2099-12-31",
            metrics="averageViewPercentage", filters=f"video=={video_id}",
        ).execute()
    except Exception as e:
        log.warning("Retenção indisponível para %s: %s", video_id, e)
        return None
    linhas = resposta.get("rows") or []
    if not linhas or not linhas[0]:
        return None
    return float(linhas[0][0]) / 100.0


# --- Instagram ----------------------------------------------------------------

def metricas_instagram(media_id, token, http=None, base=None):
    """{views, likes, comentarios} de um Reel.

    Likes e comentários vêm dos campos do próprio objeto (mais estáveis); o
    número de plays vem de `insights`, que é onde ele mora.
    """
    base = base or settings.INSTAGRAM_API_BASE
    campos = instagram_mod._pedir(
        "GET", f"{base}/{media_id}",
        {"fields": "like_count,comments_count", "access_token": token},
        http=http,
    )
    metricas = {
        "views": 0,
        "likes": _inteiro(campos, "like_count"),
        "comentarios": _inteiro(campos, "comments_count"),
        "retencao": None,
    }

    try:
        insights = instagram_mod._pedir(
            "GET", f"{base}/{media_id}/insights",
            {"metric": "plays,reach", "access_token": token}, http=http,
        )
    except instagram_mod.ErroInstagram as e:
        # Insights somem para mídia antiga e para conta que perdeu o vínculo
        # com a página. Likes e comentários já foram lidos e continuam valendo.
        log.info("Insights indisponíveis para %s: %s", media_id, e)
        return metricas

    for entrada in insights.get("data") or []:
        valores = entrada.get("values") or [{}]
        if entrada.get("name") == "plays":
            metricas["views"] = _inteiro(valores[0], "value")
    return metricas


# --- TikTok -------------------------------------------------------------------

# A API aceita até 20 ids por consulta, e recusa o lote inteiro acima disso:
# quem fatia é o cliente.
MAX_IDS_TIKTOK = 20

# `share_count` entra porque é o sinal mais forte daqui: quem manda o clip
# para alguém está fazendo a distribuição que o algoritmo cobra. É a única
# plataforma das três que informa o número, e a coluna `compartilhamentos`
# fica 0 nas outras duas.
CAMPOS_TIKTOK = "id,view_count,like_count,comment_count,share_count"


def metricas_tiktok(video_ids, token, http=None, base=None):
    """{video_id: {views, likes, comentarios}} — em lotes de 20.

    O escopo é `video.list`, DIFERENTE do `video.publish` que publica: um app
    autorizado só para postar responde `scope_not_authorized` aqui, e é essa a
    mensagem que chega ao log.
    """
    ids = [str(v) for v in video_ids]
    url = tiktok_mod._url("video/query", base) + "?fields=" + CAMPOS_TIKTOK

    saida = {}
    for inicio in range(0, len(ids), MAX_IDS_TIKTOK):
        lote = ids[inicio:inicio + MAX_IDS_TIKTOK]
        dados = tiktok_mod._pedir(url, token, {"filters": {"video_ids": lote}},
                                  http=http)
        for video in dados.get("videos") or []:
            identificador = str(video.get("id") or "")
            if not identificador:
                continue
            saida[identificador] = {
                # Os dois nomes de view porque a API já chamou o mesmo número
                # de play_count, e métrica ausente vem omitida em vez de zero.
                "views": _inteiro(video, "view_count", "play_count"),
                "likes": _inteiro(video, "like_count"),
                "comentarios": _inteiro(video, "comment_count"),
                "compartilhamentos": _inteiro(video, "share_count"),
                "retencao": None,
            }

    faltando = set(ids) - set(saida)
    if faltando:
        # Vídeo apagado, ou que a conta tornou privado depois de publicado.
        # Não é erro do pipeline, e a última medição continua valendo.
        log.info("Sem métricas para %d vídeo(s) do TikTok: %s",
                 len(faltando), ", ".join(sorted(faltando)))
    return saida


def medir_tiktok(conn, linhas, http=None):
    """Mede os posts do TikTok que dá para medir. Devolve quantos entraram.

    Os privados são separados ANTES da chamada: mandar um publish_id no lugar
    de um id de vídeo não devolve erro útil, devolve um lote sem resultado — e
    o motivo verdadeiro (o app ainda não passou pela revisão) não apareceria
    em lugar nenhum.
    """
    publicos = [l for l in linhas if tiktok_mod.id_publico(l["id_externo"])]
    privados = len(linhas) - len(publicos)
    if privados:
        log.info(
            "%d post(s) do TikTok sem id público: saíram como SELF_ONLY, que é "
            "o que um app não revisado consegue publicar. Não há métrica a "
            "consultar enquanto for assim.", privados,
        )
    if not publicos:
        return 0

    try:
        token = tiktok_mod.garantir_token(conn, http=http)
        metricas = metricas_tiktok([l["id_externo"] for l in publicos], token,
                                   http=http)
    except Exception as e:
        # O TikTok fora do ar, ou sem o escopo video.list, não pode apagar as
        # medições do YouTube e do Instagram já gravadas nesta execução — nem
        # impedir a recalibração de rodar sobre elas.
        log.warning("Métricas do TikTok falharam: %s", e)
        return 0

    medidos = 0
    for linha in publicos:
        dados = metricas.get(str(linha["id_externo"]))
        if dados is None:
            continue
        repositorio.registrar_resultado(
            conn, linha, dados, horas_publicado=linha["horas_publicado"]
        )
        medidos += 1
    return medidos


# --- orquestração -------------------------------------------------------------

def coletar(conn, agora=None, cliente_youtube=None, cliente_analytics=None,
            http=None, idade_minima_h=None, idade_maxima_h=None, limite=None):
    """Mede todos os posts elegíveis. Devolve {plataforma: quantidade}."""
    if idade_minima_h is None:
        idade_minima_h = settings.ANALYTICS_IDADE_MINIMA_H
    if idade_maxima_h is None:
        idade_maxima_h = settings.ANALYTICS_IDADE_MAXIMA_H

    candidatos = repositorio.publicacoes_para_medir(
        conn, idade_minima_h=idade_minima_h, agora=agora, limite=limite
    )
    # A janela superior corta aqui e não no SQL para o motivo ficar legível:
    # depois de um mês a curva estabilizou e cada medição nova gasta quota para
    # confirmar o que já se sabe.
    elegiveis = [c for c in candidatos
                 if (c["horas_publicado"] or 0) <= idade_maxima_h]
    if not elegiveis:
        return {}
    log.info("%d posts para medir.", len(elegiveis))

    por_plataforma = {}
    for linha in elegiveis:
        por_plataforma.setdefault(linha["plataforma"], []).append(linha)

    contagem = {}

    do_youtube = por_plataforma.get(settings.PLATAFORMA_YOUTUBE) or []
    if do_youtube:
        cliente = cliente_youtube or youtube_sourcing.construir_cliente()
        ids = [l["id_externo"] for l in do_youtube if l["id_externo"]]
        metricas = metricas_youtube(cliente, ids)
        quota.registrar(conn, CUSTO_VIDEOS_LIST * max(1, len(ids) // 50 + 1),
                        agora=None)
        for linha in do_youtube:
            dados = metricas.get(linha["id_externo"])
            if dados is None:
                continue
            if settings.ANALYTICS_RETENCAO:
                dados = dict(dados)
                dados["retencao"] = retencao_youtube(
                    cliente_analytics, linha["id_externo"]
                )
            repositorio.registrar_resultado(
                conn, linha, dados, horas_publicado=linha["horas_publicado"]
            )
            contagem["youtube"] = contagem.get("youtube", 0) + 1

    do_instagram = por_plataforma.get(settings.PLATAFORMA_INSTAGRAM) or []
    if do_instagram:
        token = instagram_mod.garantir_token(conn, http=http)
        for linha in do_instagram:
            try:
                dados = metricas_instagram(linha["id_externo"], token, http=http)
            except Exception as e:
                # Uma mídia que sumiu não pode derrubar a medição das outras.
                log.warning("Métricas de %s falharam: %s", linha["id_externo"], e)
                continue
            repositorio.registrar_resultado(
                conn, linha, dados, horas_publicado=linha["horas_publicado"]
            )
            contagem["instagram"] = contagem.get("instagram", 0) + 1

    do_tiktok = por_plataforma.get(settings.PLATAFORMA_TIKTOK) or []
    if do_tiktok:
        medidos = medir_tiktok(conn, do_tiktok, http=http)
        if medidos:
            contagem["tiktok"] = medidos

    sem_coletor = sorted(set(por_plataforma) - set(PLATAFORMAS_MEDIDAS))
    if sem_coletor:
        # Uma plataforma nova publica antes de ser medida — é a ordem natural
        # de implementar. Dizer isso em voz alta evita a conclusão errada de
        # que aqueles posts não renderam: eles não foram perguntados.
        log.info(
            "Sem coletor de métricas para %s: %d post(s) ficam de fora da "
            "recalibração.", ", ".join(sem_coletor),
            sum(len(por_plataforma[p]) for p in sem_coletor),
        )

    log.info("Medições gravadas: %s", contagem or "nenhuma")
    return contagem
