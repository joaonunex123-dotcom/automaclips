"""Publicação de Reels e renovação do token de longa duração do Instagram.

Duas coisas desta API moldam o módulo, e as duas surpreendem quem chega do
YouTube:

1. **Não existe upload de arquivo.** A API baixa o vídeo de uma URL que você
   informa, então o clip precisa estar publicamente acessível na internet
   antes de qualquer coisa (CLIPS_BASE_URL). Sem isso a publicação no
   Instagram simplesmente não é possível — e o modo sombra avisa disso agora,
   em vez de descobrir no primeiro dia de publicação real.

2. **A publicação é em duas etapas e assíncrona.** Primeiro cria-se um
   contêiner de mídia, que o Instagram processa por conta própria; só quando
   ele termina é que o `media_publish` funciona. Publicar sem esperar devolve
   erro, então há um laço de espera com teto.

O token de longa duração dura ~60 dias e é renovável. Renovar é obrigação do
programa, não do humano: um token que morre num domingo derruba a fila até
alguém notar. Por isso o valor vigente vive na tabela `tokens` (muda em
runtime) e não no .env (que o humano edita).

AVISO: os detalhes desta API mudam com frequência e não foram exercitados
contra o serviço real — endpoints e nomes de campo estão em settings, para
serem corrigidos sem mexer no código. Confira contra a documentação vigente
antes do primeiro post de verdade.
"""
import logging
import time
from datetime import datetime, timedelta

import settings
from db import repositorio

log = logging.getLogger(__name__)

SERVICO = "instagram"

# Estados que o contêiner de mídia pode reportar enquanto o Instagram processa.
STATUS_PRONTO = "FINISHED"
STATUS_ERRO = ("ERROR", "EXPIRED")


class ErroInstagram(Exception):
    """Falha de configuração, de token ou de publicação."""


def _http():
    try:
        import requests
    except ImportError as e:  # pragma: no cover - ambiente sem requests
        raise ErroInstagram(
            "requests não instalado (pip install -r requirements.txt)."
        ) from e
    return requests


def _pedir(metodo, url, parametros, http=None, timeout=60):
    """Uma chamada HTTP, com o erro da API virando exceção legível."""
    http = http or _http()
    resposta = http.request(metodo, url, params=parametros, timeout=timeout)
    try:
        dados = resposta.json()
    except Exception:
        dados = {}
    if resposta.status_code >= 400 or "error" in dados:
        detalhe = (dados.get("error") or {}).get("message") or resposta.text[:200]
        raise ErroInstagram(f"{metodo} {url} falhou ({resposta.status_code}): {detalhe}")
    return dados


# --- token --------------------------------------------------------------------

def token_atual(conn):
    """O token vigente: o do banco se houver, senão o inicial do .env.

    A primeira execução usa o do .env e, ao renovar, grava o novo no banco —
    daí em diante o .env vira só a semente. É o que evita o humano ter de
    colar um token novo a cada dois meses.
    """
    linha = repositorio.obter_token(conn, SERVICO)
    if linha and linha["token"]:
        return linha["token"], linha["expira_em"]
    if settings.INSTAGRAM_TOKEN_INICIAL:
        return settings.INSTAGRAM_TOKEN_INICIAL, None
    raise ErroInstagram(
        "sem token do Instagram. Preencha INSTAGRAM_TOKEN_INICIAL no .env com "
        "um token de longa duração."
    )


def precisa_renovar(expira_em, agora=None, antecedencia_dias=None):
    """Se o token deve ser renovado agora.

    Sem data de expiração conhecida, a resposta é SIM: é o caso do token que
    veio do .env e nunca passou por aqui, e renovar um token ainda válido é
    barato — deixá-lo morrer não é.
    """
    if antecedencia_dias is None:
        antecedencia_dias = settings.INSTAGRAM_RENOVAR_ANTES_DIAS
    if not expira_em:
        return True
    agora = agora or datetime.now()
    try:
        limite = datetime.fromisoformat(str(expira_em))
    except ValueError:
        log.warning("Validade de token ilegível (%r); renovando.", expira_em)
        return True
    return agora >= limite - timedelta(days=antecedencia_dias)


def renovar(conn, token=None, http=None, agora=None, base=None):
    """Troca o token por um novo de 60 dias e grava. Devolve o novo token."""
    if token is None:
        token, _ = token_atual(conn)
    base = base or settings.INSTAGRAM_API_BASE
    agora = agora or datetime.now()

    dados = _pedir(
        "GET", f"{base}/refresh_access_token",
        {"grant_type": "ig_refresh_token", "access_token": token},
        http=http,
    )
    novo = dados.get("access_token")
    if not novo:
        raise ErroInstagram(f"renovação sem access_token na resposta: {dados!r}")

    segundos = int(dados.get("expires_in") or 0)
    expira_em = (agora + timedelta(seconds=segundos)).isoformat() if segundos else None
    repositorio.salvar_token(conn, SERVICO, novo, expira_em)
    log.info("Token do Instagram renovado; validade até %s.", expira_em or "desconhecida")
    return novo


def garantir_token(conn, http=None, agora=None):
    """O token bom para usar agora, renovando se estiver perto de vencer."""
    token, expira_em = token_atual(conn)
    if precisa_renovar(expira_em, agora):
        try:
            return renovar(conn, token, http=http, agora=agora)
        except ErroInstagram as e:
            # Um token ainda válido não deve ser descartado porque a renovação
            # falhou: a publicação de hoje ainda funciona, e a próxima execução
            # tenta renovar de novo.
            if expira_em:
                log.warning("Renovação falhou (%s); seguindo com o token atual.", e)
                return token
            raise
    return token


# --- publicação ---------------------------------------------------------------

def url_publica(caminho_render, base=None):
    """Caminho local -> URL pública, pela CLIPS_BASE_URL.

    A API baixa o vídeo por HTTP; um caminho de disco não serve.
    """
    base = settings.CLIPS_BASE_URL if base is None else base
    if not base:
        raise ErroInstagram(
            "CLIPS_BASE_URL vazia: o Instagram baixa o vídeo por URL e não "
            "aceita upload de arquivo. Publique a pasta render/ numa URL "
            "pública, ou tire 'instagram' de PLATAFORMAS."
        )
    import os

    return f"{base.rstrip('/')}/{os.path.basename(caminho_render)}"


def criar_container(url_video, caption, token, user_id=None, http=None, base=None):
    base = base or settings.INSTAGRAM_API_BASE
    user_id = user_id or settings.INSTAGRAM_USER_ID
    if not user_id:
        raise ErroInstagram("INSTAGRAM_USER_ID não configurado.")

    dados = _pedir(
        "POST", f"{base}/{user_id}/media",
        {"media_type": "REELS", "video_url": url_video, "caption": caption,
         "access_token": token},
        http=http,
    )
    container = dados.get("id")
    if not container:
        raise ErroInstagram(f"criação de contêiner sem id: {dados!r}")
    return container


def esperar_processamento(container_id, token, http=None, base=None,
                          tentativas=20, espera_s=15, dormir=None):
    """Aguarda o Instagram terminar de processar o vídeo.

    Publicar antes disso devolve erro, então a espera não é otimização — é
    parte do protocolo. O teto existe para um vídeo problemático não segurar
    a execução para sempre.
    """
    base = base or settings.INSTAGRAM_API_BASE
    dormir = dormir or time.sleep

    for tentativa in range(tentativas):
        dados = _pedir(
            "GET", f"{base}/{container_id}",
            {"fields": "status_code", "access_token": token}, http=http,
        )
        status = dados.get("status_code")
        if status == STATUS_PRONTO:
            return True
        if status in STATUS_ERRO:
            raise ErroInstagram(f"contêiner {container_id} falhou: {status}")
        if tentativa < tentativas - 1:
            dormir(espera_s)
    raise ErroInstagram(
        f"contêiner {container_id} não ficou pronto em "
        f"{tentativas * espera_s}s (último status: {status!r})"
    )


def publicar_container(container_id, token, user_id=None, http=None, base=None):
    base = base or settings.INSTAGRAM_API_BASE
    user_id = user_id or settings.INSTAGRAM_USER_ID
    dados = _pedir(
        "POST", f"{base}/{user_id}/media_publish",
        {"creation_id": container_id, "access_token": token}, http=http,
    )
    media_id = dados.get("id")
    if not media_id:
        raise ErroInstagram(f"publicação sem id na resposta: {dados!r}")
    return media_id, f"https://www.instagram.com/reel/{media_id}/"


def publicar(conn, caminho_render, caption, http=None, dormir=None, agora=None):
    """As três etapas numa chamada. Devolve (media_id, url)."""
    token = garantir_token(conn, http=http, agora=agora)
    url_video = url_publica(caminho_render)

    container = criar_container(url_video, caption, token, http=http)
    esperar_processamento(container, token, http=http, dormir=dormir)
    return publicar_container(container, token, http=http)
