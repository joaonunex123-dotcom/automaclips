"""Confere se a publicação real pode ser ligada, antes de ligar.

Existe porque a etapa 6 muda a natureza do erro. Em modo sombra, configuração
faltando é um post que não sai; com a publicação real ligada, é a fila inteira
falhando às 3 da manhã, uma linha de `erro` por vez, sem ninguém olhando — e
cada tentativa gasta quota ou queima um horário de queda.

A verificação é declarativa de propósito: devolve a LISTA de problemas em vez
de parar no primeiro. Quem está ligando a publicação quer resolver tudo numa
sentada, não descobrir mais um item a cada execução.

Nada aqui faz chamada de rede. São checagens locais — arquivo existe, chave
preenchida, quota do dia — porque uma verificação que depende de rede falharia
por motivo errado e ensinaria a ignorá-la.
"""
import logging
import os

import settings
from db import repositorio
from publish import quota
from publish import tiktok as tiktok_mod

log = logging.getLogger(__name__)

# Um problema BLOQUEIA a plataforma (publicar vai falhar). Um aviso não impede,
# mas muda o que vai acontecer.
BLOQUEIO = "bloqueio"
AVISO = "aviso"


def _item(nivel, plataforma, mensagem, como_resolver=""):
    return {
        "nivel": nivel,
        "plataforma": plataforma,
        "mensagem": mensagem,
        "como_resolver": como_resolver,
    }


def parada_de_emergencia_ativa(caminho=None):
    """Se algum arquivo de freio existe. Checado antes de tudo, sempre.

    São dois quando há perfis: o do canal e o da raiz, que para todos. Quem
    cria o arquivo no meio de um incidente quer que TUDO pare, não que pare o
    canal cujo nome ele lembrou de digitar. Sem perfil os dois são o mesmo
    arquivo, e a checagem é a de sempre.
    """
    if caminho is not None:
        return os.path.exists(caminho)
    return any(os.path.exists(c) for c in (
        settings.ARQUIVO_PARAR_PUBLICACAO,
        settings.ARQUIVO_PARAR_PUBLICACAO_GLOBAL,
    ))


def _checar_metadata():
    if settings.OPENROUTER_API_KEY:
        return []
    return [_item(
        BLOQUEIO, "-", "OPENROUTER_API_KEY vazia: o metadado não é gerado, "
        "então nada chega a ser agendado.",
        "preencha no .env",
    )]


def _checar_youtube(conn, agora=None):
    problemas = []
    if not os.path.exists(settings.YOUTUBE_OAUTH_TOKEN):
        problemas.append(_item(
            BLOQUEIO, "youtube",
            f"sem autorização OAuth em {settings.YOUTUBE_OAUTH_TOKEN}",
            "python -m publish.publicar --autorizar",
        ))
    if not quota.cabe(conn, agora=agora):
        # Aviso e não bloqueio: a quota vira à meia-noite do Pacífico sozinha.
        problemas.append(_item(
            AVISO, "youtube", f"sem quota hoje — {quota.resumo(conn, agora=agora)}",
            "espere a virada do dia no Pacífico",
        ))
    if settings.YOUTUBE_PRIVACIDADE != "public":
        problemas.append(_item(
            AVISO, "youtube",
            f"privacidade em '{settings.YOUTUBE_PRIVACIDADE}': o vídeo sobe mas "
            "não fica visível.",
            "YOUTUBE_PRIVACIDADE=public quando confiar na fila",
        ))
    return problemas


def _checar_instagram(conn):
    problemas = []
    if not settings.INSTAGRAM_USER_ID:
        problemas.append(_item(
            BLOQUEIO, "instagram", "INSTAGRAM_USER_ID vazio", "preencha no .env",
        ))
    tem_token = bool(settings.INSTAGRAM_TOKEN_INICIAL) or bool(
        repositorio.obter_token(conn, "instagram")
    )
    if not tem_token:
        problemas.append(_item(
            BLOQUEIO, "instagram", "sem token de longa duração",
            "preencha INSTAGRAM_TOKEN_INICIAL no .env",
        ))
    if not settings.CLIPS_BASE_URL:
        problemas.append(_item(
            BLOQUEIO, "instagram",
            "CLIPS_BASE_URL vazia: a API baixa o vídeo por URL e não aceita "
            "upload de arquivo.",
            "publique a pasta render/ numa URL pública, ou tire 'instagram' "
            "de PLATAFORMAS",
        ))
    return problemas


def _checar_tiktok(conn):
    """O que precisa estar de pé para o TikTok aceitar um post.

    O item que mais confunde é o último: um app que ainda não passou pela
    revisão da TikTok publica SEMPRE em SELF_ONLY, visível só para a própria
    conta. Não é bug, não tem contorno, e o único jeito de sair disso é a
    revisão ser aprovada — o que leva dias ou semanas. Melhor ler isso aqui,
    antes de ligar, do que concluir que a integração está quebrada quando o
    primeiro post sair privado.
    """
    problemas = []
    if not (settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET):
        problemas.append(_item(
            BLOQUEIO, "tiktok",
            "TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET vazias",
            "crie o app em developers.tiktok.com e preencha no .env",
        ))

    tem_token = bool(settings.TIKTOK_ACCESS_TOKEN) or bool(
        repositorio.obter_token(conn, tiktok_mod.SERVICO)
    )
    if not tem_token:
        problemas.append(_item(
            BLOQUEIO, "tiktok", "sem access token",
            "preencha TIKTOK_ACCESS_TOKEN no .env",
        ))

    tem_refresh = bool(settings.TIKTOK_REFRESH_TOKEN) or bool(
        repositorio.obter_token(conn, tiktok_mod.SERVICO_REFRESH)
    )
    if not tem_refresh:
        # Aviso e não bloqueio: o post de hoje sai. O de amanhã é que não.
        problemas.append(_item(
            AVISO, "tiktok",
            "sem refresh token: o access token do TikTok vale ~24 h, e sem "
            "renovar a fila para sozinha amanhã.",
            "preencha TIKTOK_REFRESH_TOKEN no .env",
        ))

    modo = (settings.TIKTOK_MODO_UPLOAD or "").lower()
    if modo == tiktok_mod.MODO_URL and not settings.CLIPS_BASE_URL:
        problemas.append(_item(
            BLOQUEIO, "tiktok",
            "TIKTOK_MODO_UPLOAD=url exige CLIPS_BASE_URL, que está vazia.",
            "configure a URL pública da pasta render/, ou use "
            "TIKTOK_MODO_UPLOAD=arquivo (envia o arquivo direto)",
        ))

    if not settings.TIKTOK_APP_AUDITADO:
        problemas.append(_item(
            AVISO, "tiktok",
            "app declarado como NÃO revisado: enquanto a TikTok não aprovar a "
            "revisão, todo post sai como SELF_ONLY (só você vê). É limitação "
            "da plataforma, não falha da integração.",
            "acompanhe a revisão em developers.tiktok.com; quando sair, "
            "TIKTOK_APP_AUDITADO=true e TIKTOK_PRIVACIDADE=PUBLIC_TO_EVERYONE",
        ))
    elif settings.TIKTOK_PRIVACIDADE == tiktok_mod.PRIVADO:
        problemas.append(_item(
            AVISO, "tiktok",
            "privacidade em SELF_ONLY: o vídeo sobe mas só você vê.",
            "TIKTOK_PRIVACIDADE=PUBLIC_TO_EVERYONE quando confiar na fila",
        ))
    return problemas


def verificar(conn, plataformas=None, agora=None):
    """Lista de problemas. Vazia = pronto para publicar de verdade."""
    plataformas = plataformas or settings.PLATAFORMAS
    problemas = []

    if parada_de_emergencia_ativa():
        problemas.append(_item(
            BLOQUEIO, "-",
            f"parada de emergência ativa ({settings.ARQUIVO_PARAR_PUBLICACAO})",
            "apague o arquivo para liberar",
        ))

    problemas += _checar_metadata()
    if settings.PLATAFORMA_YOUTUBE in plataformas:
        problemas += _checar_youtube(conn, agora=agora)
    if settings.PLATAFORMA_INSTAGRAM in plataformas:
        problemas += _checar_instagram(conn)
    if settings.PLATAFORMA_TIKTOK in plataformas:
        problemas += _checar_tiktok(conn)
    return problemas


def bloqueios(problemas):
    return [p for p in problemas if p["nivel"] == BLOQUEIO]


def plataformas_bloqueadas(problemas):
    """As plataformas que não conseguem publicar. '-' bloqueia todas."""
    nomes = {p["plataforma"] for p in bloqueios(problemas)}
    if "-" in nomes:
        return set(settings.PLATAFORMAS) | {"-"}
    return nomes


def formatar(problemas):
    """Relatório legível, agrupado por gravidade."""
    if not problemas:
        return "Pronto para publicar: nenhum impedimento."

    linhas = []
    for nivel, titulo in ((BLOQUEIO, "IMPEDE A PUBLICAÇÃO"), (AVISO, "avisos")):
        do_nivel = [p for p in problemas if p["nivel"] == nivel]
        if not do_nivel:
            continue
        linhas.append(f"  {titulo}:")
        for p in do_nivel:
            linhas.append(f"    [{p['plataforma']}] {p['mensagem']}")
            if p["como_resolver"]:
                linhas.append(f"        -> {p['como_resolver']}")
    return "\n".join(linhas)
