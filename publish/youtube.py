"""Upload para o YouTube.

Credencial DIFERENTE da do sourcing: listar vídeo público aceita chave de API,
subir vídeo exige OAuth em nome do dono do canal. As duas saem do mesmo projeto
no Google Cloud, mas são arquivos distintos e escopos distintos.

O fluxo de autorização abre um navegador e é, por natureza, interativo — não dá
para rodar no meio de uma execução agendada às 3 da manhã. Por isso ele fica
num comando à parte (`python -m publish.publicar --autorizar`) e a publicação
só CONSOME o token já salvo: se ele não existir ou não puder ser renovado
sozinho, a publicação falha com a instrução, em vez de travar esperando um
navegador que ninguém vai abrir.

Custo de quota: 1600 unidades por upload, de 10.000 diárias. A contabilidade
fica em publish/quota.py, junto com a regra do dia do Pacífico.
"""
import logging
import os

import settings

log = logging.getLogger(__name__)

# Escopo mínimo para subir vídeo. `youtube.upload` não dá leitura do canal —
# pedir mais do que se usa é pedir ao usuário uma permissão que o programa não
# precisa.
ESCOPOS = ["https://www.googleapis.com/auth/youtube.upload"]

CUSTO_UPLOAD = 1600


class ErroYouTubeUpload(Exception):
    """Falha de credencial ou de upload."""


def carregar_credenciais(token_path=None, erro_se_ausente=True):
    """Credenciais salvas, renovadas se expiradas. None se não houver.

    A renovação silenciosa (refresh_token) é o caminho normal: o token de
    acesso do Google dura uma hora, então toda execução depois da primeira
    passa por aqui.
    """
    token_path = token_path or settings.YOUTUBE_OAUTH_TOKEN
    if not os.path.exists(token_path):
        if erro_se_ausente:
            raise ErroYouTubeUpload(
                f"sem autorização do YouTube em {token_path}. Rode "
                "`python -m publish.publicar --autorizar` uma vez."
            )
        return None

    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as e:  # pragma: no cover - ambiente sem os SDKs
        raise ErroYouTubeUpload(
            "google-auth não instalado (pip install -r requirements.txt)."
        ) from e

    credenciais = Credentials.from_authorized_user_file(token_path, ESCOPOS)
    if credenciais.valid:
        return credenciais
    if credenciais.expired and credenciais.refresh_token:
        credenciais.refresh(Request())
        salvar_credenciais(credenciais, token_path)
        return credenciais
    raise ErroYouTubeUpload(
        "autorização do YouTube expirada e sem refresh_token. Rode "
        "`python -m publish.publicar --autorizar` de novo."
    )


def salvar_credenciais(credenciais, token_path=None):
    token_path = token_path or settings.YOUTUBE_OAUTH_TOKEN
    os.makedirs(os.path.dirname(token_path) or ".", exist_ok=True)
    with open(token_path, "w", encoding="utf-8") as f:
        f.write(credenciais.to_json())
    return token_path


def autorizar(client_secrets=None, token_path=None):
    """Fluxo interativo. Roda UMA vez, fora da execução agendada."""
    client_secrets = client_secrets or settings.YOUTUBE_CLIENT_SECRETS
    if not os.path.exists(client_secrets):
        raise ErroYouTubeUpload(
            f"{client_secrets} não existe. Baixe as credenciais OAuth de "
            "cliente (tipo 'Aplicativo para computador') no console do Google "
            "Cloud e salve nesse caminho."
        )
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as e:  # pragma: no cover - ambiente sem o SDK
        raise ErroYouTubeUpload(
            "google-auth-oauthlib não instalado (pip install -r requirements.txt)."
        ) from e

    fluxo = InstalledAppFlow.from_client_secrets_file(client_secrets, ESCOPOS)
    credenciais = fluxo.run_local_server(port=0)
    caminho = salvar_credenciais(credenciais, token_path)
    log.info("Autorização do YouTube salva em %s.", caminho)
    return caminho


def construir_cliente(credenciais=None, token_path=None):
    """Cliente da Data API autenticado por OAuth."""
    credenciais = credenciais or carregar_credenciais(token_path)
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=credenciais, cache_discovery=False)


def corpo_do_upload(titulo, descricao, tags=None, categoria=None,
                    privacidade=None):
    """O body do videos.insert. Função pura — testável sem rede."""
    return {
        "snippet": {
            "title": titulo,
            "description": descricao,
            "tags": list(tags or []),
            "categoryId": str(categoria or settings.YOUTUBE_CATEGORIA),
        },
        "status": {
            "privacyStatus": privacidade or settings.YOUTUBE_PRIVACIDADE,
            # Declaração obrigatória desde 2020; sem ela a API recusa o insert.
            # False é a resposta correta para clip de canal adulto genérico —
            # se o seu nicho for infantil, isto precisa mudar.
            "selfDeclaredMadeForKids": False,
        },
    }


def enviar(cliente, caminho, titulo, descricao, tags=None, categoria=None,
           privacidade=None, criar_midia=None):
    """Sobe o arquivo. Devolve (video_id, url).

    `criar_midia` é injetável para os testes: é o único ponto que precisa do
    googleapiclient de verdade.
    """
    if not os.path.exists(caminho):
        raise ErroYouTubeUpload(f"arquivo não encontrado: {caminho}")

    if criar_midia is None:
        from googleapiclient.http import MediaFileUpload

        def criar_midia(arquivo):
            # chunksize=-1 envia num pedido só. Clip vertical de 60 s tem
            # dezenas de MB; fatiar traria a complexidade do retomar sem
            # ganho real nesse tamanho.
            return MediaFileUpload(arquivo, chunksize=-1, resumable=True)

    corpo = corpo_do_upload(titulo, descricao, tags, categoria, privacidade)
    try:
        requisicao = cliente.videos().insert(
            part="snippet,status", body=corpo, media_body=criar_midia(caminho)
        )
        resposta = requisicao.execute()
    except ErroYouTubeUpload:
        raise
    except Exception as e:
        raise ErroYouTubeUpload(f"upload de {os.path.basename(caminho)} falhou: {e}") from e

    video_id = (resposta or {}).get("id")
    if not video_id:
        raise ErroYouTubeUpload(f"upload sem id na resposta: {resposta!r}")
    return video_id, f"https://www.youtube.com/watch?v={video_id}"
