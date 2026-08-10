"""Cliente da YouTube Data API v3 — só leitura pública.

Custo de quota, porque ele decide o desenho deste módulo (o teto diário é
10.000 unidades e o upload da etapa 6 sozinho custa 1.600 por vídeo):

    search.list          100 unidades
    channels.list          1 unidade   (até 50 canais por chamada)
    playlistItems.list     1 unidade   (até 50 itens por chamada)
    videos.list            1 unidade   (até 50 vídeos por chamada)

Por isso a descoberta NÃO usa search.list. O caminho é canal -> playlist de
uploads -> vídeos: para 10 canais com 20 vídeos cada, são 1 + 10 + 4 = 15
unidades por varredura, contra 1.000 se cada canal fosse um search. A quatro
varreduras por dia isso é 60 unidades contra 4.000 — a diferença entre sourcing
que cabe no orçamento e sourcing que consome o teto antes de publicar nada.

O cliente da API é sempre injetado nas funções, nunca construído dentro delas:
é o que permite testar a montagem e a paginação com um duplo, sem rede e sem
chave.
"""
import logging
import re

import settings

log = logging.getLogger(__name__)

PLATAFORMA = "youtube"

# Teto de itens por chamada imposto pela própria API.
MAX_POR_CHAMADA = 50

_RE_DURACAO = re.compile(
    r"^P(?:(?P<d>\d+)D)?(?:T(?:(?P<h>\d+)H)?(?:(?P<m>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


class ErroYouTube(Exception):
    """Falha de configuração ou de resposta da API."""


def construir_cliente(api_key=None):
    """Cliente autenticado por chave de API.

    O import é local de propósito: mantém o módulo importável (e o resto dos
    testes rodando) numa máquina sem google-api-python-client instalado.

    cache_discovery=False silencia o aviso de cache de discovery em ambiente
    sem oauth2client — o cache não vale nada aqui, é uma chamada por processo.
    """
    api_key = api_key or settings.YOUTUBE_API_KEY
    if not api_key:
        raise ErroYouTube(
            "YOUTUBE_API_KEY não configurada. Preencha no .env (ver .env.example)."
        )
    from googleapiclient.discovery import build

    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def duracao_para_segundos(iso):
    """'PT1H2M3S' -> 3723. Formato irreconhecível vira 0.

    Zero é deliberadamente o valor "inutilizável": o filtro de duração em
    descobrir.py tem um mínimo, então um vídeo cuja duração não pôde ser lida
    é ignorado em vez de baixado às cegas. Live em andamento devolve 'P0D' e
    cai exatamente nesse caso, que é o desejado — não há como recortar um
    vídeo que ainda não terminou.
    """
    if not iso:
        return 0
    m = _RE_DURACAO.match(str(iso).strip())
    if not m:
        return 0
    partes = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return partes["d"] * 86400 + partes["h"] * 3600 + partes["m"] * 60 + partes["s"]


def _lotes(itens, tamanho=MAX_POR_CHAMADA):
    for i in range(0, len(itens), tamanho):
        yield itens[i:i + tamanho]


def playlists_de_uploads(cliente, canal_ids):
    """{canal_id: (playlist_de_uploads, nome_do_canal)} — 1 unidade por 50 canais.

    Todo canal tem uma playlist automática com os seus uploads; ler dela é o
    substituto barato do search.list por canal.
    """
    saida = {}
    for lote in _lotes(list(canal_ids)):
        resposta = cliente.channels().list(
            part="contentDetails,snippet", id=",".join(lote), maxResults=MAX_POR_CHAMADA
        ).execute()
        for item in resposta.get("items", []):
            uploads = (
                item.get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            if not uploads:
                # Canal sem playlist de uploads: existe (conta de marca sem
                # vídeo próprio, por exemplo). Avisa e segue — um canal
                # inválido na lista não pode derrubar a varredura inteira.
                log.warning("Canal %s sem playlist de uploads; pulando.", item.get("id"))
                continue
            saida[item["id"]] = (uploads, item.get("snippet", {}).get("title", ""))

    faltando = set(canal_ids) - set(saida)
    if faltando:
        log.warning(
            "Canais não encontrados na API (ID errado ou canal removido): %s",
            ", ".join(sorted(faltando)),
        )
    return saida


def ids_de_uploads_recentes(cliente, playlist_id, maximo):
    """IDs dos uploads mais recentes da playlist, do mais novo para o mais velho.

    A playlist de uploads já vem em ordem cronológica reversa, então parar na
    primeira página costuma bastar; o laço existe para maximo > 50.
    """
    ids = []
    pagina = None
    while len(ids) < maximo:
        resposta = cliente.playlistItems().list(
            part="contentDetails",
            playlistId=playlist_id,
            maxResults=min(MAX_POR_CHAMADA, maximo - len(ids)),
            pageToken=pagina,
        ).execute()
        for item in resposta.get("items", []):
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                ids.append(video_id)
        pagina = resposta.get("nextPageToken")
        if not pagina:
            break
    return ids[:maximo]


def detalhes_de_videos(cliente, video_ids):
    """Título, publicação, duração e views — 1 unidade por 50 vídeos.

    Vídeo sem statistics.viewCount (canal que esconde a contagem) entra com
    views=0: sem numerador, o score dá zero e ele fica abaixo de qualquer
    threshold. Ficar de fora por não ter o dado é correto; chutar um número
    seria pior.
    """
    saida = []
    for lote in _lotes(list(video_ids)):
        resposta = cliente.videos().list(
            part="snippet,contentDetails,statistics",
            id=",".join(lote),
            maxResults=MAX_POR_CHAMADA,
        ).execute()
        for item in resposta.get("items", []):
            snippet = item.get("snippet", {})
            saida.append(
                {
                    "plataforma": PLATAFORMA,
                    "video_id": item["id"],
                    "canal_id": snippet.get("channelId", ""),
                    "canal_nome": snippet.get("channelTitle", ""),
                    "titulo": snippet.get("title", ""),
                    "url": f"https://www.youtube.com/watch?v={item['id']}",
                    "publicado_em": snippet.get("publishedAt", ""),
                    "duracao_s": duracao_para_segundos(
                        item.get("contentDetails", {}).get("duration")
                    ),
                    "views": int(item.get("statistics", {}).get("viewCount", 0) or 0),
                }
            )
    return saida


def coletar(cliente, canais, max_por_canal=None):
    """Vídeos recentes de todos os canais, prontos para pontuação.

    Falha de um canal não derruba os outros: um ID errado no canais.json ou uma
    playlist privada devem custar aquele canal, não a varredura das seis horas.
    """
    max_por_canal = max_por_canal or settings.MAX_VIDEOS_POR_CANAL
    canal_ids = [c["id"] for c in canais]
    if not canal_ids:
        return []

    uploads = playlists_de_uploads(cliente, canal_ids)

    video_ids = []
    for canal_id, (playlist_id, nome) in uploads.items():
        try:
            ids = ids_de_uploads_recentes(cliente, playlist_id, max_por_canal)
        except Exception as e:
            log.warning("Falha ao listar uploads de %s (%s): %s", nome or canal_id, canal_id, e)
            continue
        log.info("Canal %s: %d uploads recentes.", nome or canal_id, len(ids))
        video_ids.extend(ids)

    if not video_ids:
        return []
    return detalhes_de_videos(cliente, video_ids)
