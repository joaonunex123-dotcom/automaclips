"""Publicação de vídeo no TikTok pela Content Posting API.

    POST /v2/post/publish/creator_info/query/   quem é a conta, e o que ela aceita
    POST /v2/post/publish/video/init/           abre a publicação
    PUT  <upload_url>                           os bytes, em pedaços (modo arquivo)
    POST /v2/post/publish/status/fetch/         acompanha até terminar

Quatro coisas desta API moldam o módulo, e nenhuma delas tem equivalente no
YouTube ou no Instagram:

1. **App não revisado publica PRIVADO, sempre.** Enquanto a TikTok não aprovar
   o app, a única privacidade que a API aceita é ``SELF_ONLY``: o vídeo sobe,
   fica na conta, e só o dono enxerga. Isto NÃO é defeito da integração — é
   como a plataforma trata app em sandbox, e a revisão leva dias ou semanas.
   O código detecta o caso pelo ``creator_info`` (que lista as privacidades
   permitidas), rebaixa o pedido em vez de falhar, e AVISA ALTO no log. Falhar
   seria pior: perderia o clip por causa de uma limitação que não se resolve
   sozinha. Sair calado seria pior ainda: o primeiro post apareceria privado e
   pareceria bug de código.

2. **O access token dura ~24 h.** O do Instagram dura 60 dias; este dura um
   dia. Sem renovação automática a fila para amanhã, então o refresh token é
   parte da configuração mínima, e os dois valores vigentes vivem no banco
   (tabela `tokens`) e não no .env — segredo que o programa reescreve não cabe
   num arquivo que o humano edita.

3. **Aceita upload de arquivo, ao contrário do Instagram.** Dá para mandar os
   bytes (``FILE_UPLOAD``, em pedaços) ou pedir que a TikTok baixe de uma URL
   pública (``PULL_FROM_URL``, reaproveitando a CLIPS_BASE_URL). O padrão é o
   upload direto: não depende de hospedar a pasta render/ em lugar nenhum, e o
   modo URL ainda exige provar a propriedade do domínio no painel de
   desenvolvedor.

4. **A publicação é assíncrona.** O init devolve um ``publish_id``; o vídeo só
   existe quando o status vira ``PUBLISH_COMPLETE``. E um post SELF_ONLY nunca
   ganha id público — por isso o identificador gravado cai no publish_id
   quando não há outro, e a URL fica vazia em vez de apontar para uma página
   que não abre.

O refresh token mora na tabela `tokens` sob um SERVIÇO próprio
(``tiktok_refresh``) porque a tabela tem uma coluna de token só, e o schema
deste projeto é aditivo — nada de ALTER TABLE para acomodar um segundo valor.

AVISO: como no módulo do Instagram, os detalhes desta API mudam com frequência
e nada aqui foi exercitado contra o serviço real. Endpoints, nomes de campo e
limites estão em settings, para serem corrigidos sem mexer no código. Confira
contra a documentação vigente antes do primeiro post de verdade.
"""
import logging
import math
import os
import time
from datetime import datetime, timedelta

import settings
from db import repositorio

log = logging.getLogger(__name__)

SERVICO = "tiktok"
# Serviço separado na mesma tabela: ver o cabeçalho do módulo.
SERVICO_REFRESH = "tiktok_refresh"

# Privacidades da API. SELF_ONLY é a única que um app não revisado aceita.
PRIVADO = "SELF_ONLY"
PUBLICO = "PUBLIC_TO_EVERYONE"

# Estados do acompanhamento.
STATUS_PRONTO = "PUBLISH_COMPLETE"
STATUS_CAIXA_DE_ENTRADA = "SEND_TO_USER_INBOX"
STATUS_FALHOU = "FAILED"

MODO_ARQUIVO = "arquivo"
MODO_URL = "url"

MB = 1024 * 1024


class ErroTikTok(Exception):
    """Falha de configuração, de token, de validação ou de publicação."""


class VideoForaDosLimites(ErroTikTok):
    """O arquivo não passa nos limites da API — conferido antes de subir."""


def _http():
    try:
        import requests
    except ImportError as e:  # pragma: no cover - ambiente sem requests
        raise ErroTikTok(
            "requests não instalado (pip install -r requirements.txt)."
        ) from e
    return requests


def _erro_da_resposta(dados):
    """A mensagem de erro da API, ou '' quando deu certo.

    A TikTok devolve SEMPRE um bloco `error`, com `code: "ok"` no sucesso —
    diferente do Instagram, onde a simples presença da chave já é falha.
    Tratar 'error' presente como problema recusaria toda resposta boa.
    """
    erro = dados.get("error") or {}
    codigo = str(erro.get("code") or "").lower()
    if not codigo or codigo == "ok":
        return ""
    return f"{erro.get('code')}: {erro.get('message') or 'sem mensagem'}"


def _pedir(url, token, corpo=None, http=None, timeout=60):
    """Uma chamada JSON autenticada, com o erro da API virando exceção."""
    http = http or _http()
    cabecalhos = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    resposta = http.request("POST", url, json=corpo or {},
                            headers=cabecalhos, timeout=timeout)
    try:
        dados = resposta.json()
    except Exception:
        dados = {}

    detalhe = _erro_da_resposta(dados)
    if resposta.status_code >= 400 or detalhe:
        detalhe = detalhe or (resposta.text or "")[:200]
        raise ErroTikTok(f"POST {url} falhou ({resposta.status_code}): {detalhe}")
    return dados.get("data") or {}


def _url(caminho, base=None):
    base = base or settings.TIKTOK_API_BASE
    return f"{base.rstrip('/')}/{caminho.strip('/')}/"


# --- token --------------------------------------------------------------------

def token_atual(conn):
    """(access token, validade) — o do banco se houver, senão o do .env."""
    linha = repositorio.obter_token(conn, SERVICO)
    if linha and linha["token"]:
        return linha["token"], linha["expira_em"]
    if settings.TIKTOK_ACCESS_TOKEN:
        return settings.TIKTOK_ACCESS_TOKEN, None
    raise ErroTikTok(
        "sem access token do TikTok. Preencha TIKTOK_ACCESS_TOKEN no .env com "
        "o token devolvido pelo OAuth de developers.tiktok.com."
    )


def refresh_atual(conn):
    """O refresh token vigente, ou '' se nunca houve um."""
    linha = repositorio.obter_token(conn, SERVICO_REFRESH)
    if linha and linha["token"]:
        return linha["token"]
    return settings.TIKTOK_REFRESH_TOKEN or ""


def precisa_renovar(expira_em, agora=None, antecedencia_s=None):
    """Se o access token deve ser renovado agora.

    Sem validade conhecida a resposta é SIM — é o caso do token colado no .env,
    que pode ter sido gerado ontem. Um token de 24 h não dá margem para
    descobrir o vencimento na hora do post.
    """
    if antecedencia_s is None:
        antecedencia_s = settings.TIKTOK_RENOVAR_ANTES_S
    if not expira_em:
        return True
    agora = agora or datetime.now()
    try:
        limite = datetime.fromisoformat(str(expira_em))
    except ValueError:
        log.warning("Validade de token ilegível (%r); renovando.", expira_em)
        return True
    return agora >= limite - timedelta(seconds=antecedencia_s)


def renovar(conn, refresh=None, http=None, agora=None, base=None):
    """Troca o refresh token por um access token novo. Devolve o novo access.

    A resposta traz um refresh token novo junto, e o antigo deixa de valer:
    gravar os dois é obrigatório, não higiene. Guardar só o access faria a
    renovação seguinte falhar com um refresh já queimado.
    """
    refresh = refresh if refresh is not None else refresh_atual(conn)
    if not refresh:
        raise ErroTikTok(
            "sem refresh token do TikTok: o access token vale ~24 h e não há "
            "como renovar. Preencha TIKTOK_REFRESH_TOKEN no .env."
        )
    if not (settings.TIKTOK_CLIENT_KEY and settings.TIKTOK_CLIENT_SECRET):
        raise ErroTikTok(
            "TIKTOK_CLIENT_KEY/TIKTOK_CLIENT_SECRET vazias: a renovação do "
            "token é autenticada pelo app."
        )

    http = http or _http()
    agora = agora or datetime.now()
    base = base or settings.TIKTOK_API_BASE
    # O endpoint de OAuth é o único que não fala JSON: é formulário, e recusa
    # o corpo JSON que todos os outros exigem.
    resposta = http.request(
        "POST", f"{base.rstrip('/')}/oauth/token/",
        data={
            "client_key": settings.TIKTOK_CLIENT_KEY,
            "client_secret": settings.TIKTOK_CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=60,
    )
    try:
        dados = resposta.json()
    except Exception:
        dados = {}
    if resposta.status_code >= 400 or dados.get("error"):
        detalhe = (dados.get("error_description") or dados.get("error")
                   or (resposta.text or "")[:200])
        raise ErroTikTok(
            f"renovação do token falhou ({resposta.status_code}): {detalhe}"
        )

    novo = dados.get("access_token")
    if not novo:
        raise ErroTikTok(f"renovação sem access_token na resposta: {dados!r}")

    segundos = int(dados.get("expires_in") or 0)
    expira_em = (agora + timedelta(seconds=segundos)).isoformat() if segundos else None
    repositorio.salvar_token(conn, SERVICO, novo, expira_em)
    if dados.get("refresh_token"):
        repositorio.salvar_token(conn, SERVICO_REFRESH, dados["refresh_token"])
    log.info("Access token do TikTok renovado; validade até %s.",
             expira_em or "desconhecida")
    return novo


def garantir_token(conn, http=None, agora=None):
    """O access token bom para usar agora, renovando se estiver por vencer."""
    token, expira_em = token_atual(conn)
    if not precisa_renovar(expira_em, agora):
        return token
    try:
        return renovar(conn, http=http, agora=agora)
    except ErroTikTok as e:
        # Um token com validade conhecida e ainda dentro dela serve para o post
        # de agora; a próxima execução tenta renovar de novo. Sem validade
        # conhecida não há o que apostar — melhor falhar com a mensagem certa
        # do que gastar o upload numa chamada que vai voltar 401.
        if expira_em:
            log.warning("Renovação falhou (%s); seguindo com o token atual.", e)
            return token
        raise


# --- a conta, e o que ela permite ---------------------------------------------

def consultar_criador(token, http=None, base=None):
    """creator_info: apelido, privacidades permitidas e limites da conta.

    A API exige esta consulta antes do post direto, e ela é também o único
    lugar que responde, sem adivinhação, se o app ainda está em modo restrito:
    um app não revisado recebe SELF_ONLY como única privacidade possível.
    """
    return _pedir(_url("post/publish/creator_info/query", base), token, http=http)


def resolver_privacidade(criador, pedida=None):
    """(privacidade que a API aceita, motivo do rebaixamento).

    Motivo vazio = saiu como pedido. Motivo preenchido = o post NÃO vai sair
    como configurado, e quem chamou precisa dizer isso alto no log.
    """
    pedida = (pedida or settings.TIKTOK_PRIVACIDADE or PRIVADO).upper()
    opcoes = [str(o).upper() for o in (criador.get("privacy_level_options") or [])]
    if not opcoes:
        # Conta que não informou as opções: segue com o pedido e deixa a API
        # decidir. Inventar um rebaixamento aqui esconderia um post público.
        return pedida, ""
    if pedida in opcoes:
        return pedida, ""

    escolhida = PRIVADO if PRIVADO in opcoes else opcoes[0]
    return escolhida, (
        f"a conta só aceita {', '.join(opcoes)} — sinal de que o app ainda não "
        f"passou pela revisão da TikTok. O post vai sair como {escolhida}"
    )


def opcoes_de_interacao(criador):
    """disable_* do post, respeitando o que o CRIADOR desligou na conta.

    Tentar ligar comentário num perfil que os desligou faz a API recusar o
    post inteiro — o valor do criador manda sobre o do .env.
    """
    return {
        "disable_comment": bool(settings.TIKTOK_DESABILITAR_COMENTARIO
                                or criador.get("comment_disabled")),
        "disable_duet": bool(settings.TIKTOK_DESABILITAR_DUETO
                             or criador.get("duet_disabled")),
        "disable_stitch": bool(settings.TIKTOK_DESABILITAR_STITCH
                               or criador.get("stitch_disabled")),
    }


# --- o arquivo ----------------------------------------------------------------

def validar_video(caminho, duracao_s=None, max_duracao_s=None,
                  exigir_arquivo=True):
    """Confere o clip contra os limites da API. Levanta se não passar.

    Aqui e não na API: um vídeo fora do limite recusado lá custa o upload
    inteiro antes da negativa, e a mensagem que volta ('invalid_params') não
    diz qual limite foi. Devolve o tamanho em bytes.
    """
    extensao = os.path.splitext(caminho or "")[1].lower()
    formatos = [str(f).lower() for f in settings.TIKTOK_FORMATOS]
    if formatos and extensao not in formatos:
        raise VideoForaDosLimites(
            f"formato {extensao or '(sem extensão)'} não aceito pelo TikTok "
            f"(aceitos: {', '.join(formatos)})"
        )

    limite_s = max_duracao_s or settings.TIKTOK_DURACAO_MAXIMA_S
    if duracao_s and limite_s and float(duracao_s) > float(limite_s):
        raise VideoForaDosLimites(
            f"clip de {float(duracao_s):.0f}s passa do máximo de "
            f"{float(limite_s):.0f}s desta conta"
        )

    if not os.path.exists(caminho or ""):
        if exigir_arquivo:
            raise VideoForaDosLimites(f"arquivo não encontrado: {caminho}")
        return 0

    tamanho = os.path.getsize(caminho)
    if not tamanho:
        raise VideoForaDosLimites(f"arquivo vazio: {caminho}")
    teto = settings.TIKTOK_TAMANHO_MAXIMO_MB * MB
    if teto and tamanho > teto:
        raise VideoForaDosLimites(
            f"arquivo de {tamanho / MB:.0f} MB passa do máximo de "
            f"{settings.TIKTOK_TAMANHO_MAXIMO_MB} MB"
        )
    return tamanho


def plano_de_chunks(tamanho, chunk_mb=None):
    """(tamanho do pedaço, quantidade, [(primeiro byte, último byte)]).

    Regra da API: cada pedaço tem entre 5 e 64 MB, e o ÚLTIMO leva o resto —
    fatiar em partes iguais deixaria uma sobra menor que o mínimo, que a API
    recusa. Arquivo pequeno vai inteiro num pedaço só, que é o caso de todo
    clip de 45 segundos.
    """
    chunk_mb = settings.TIKTOK_CHUNK_MB if chunk_mb is None else chunk_mb
    tamanho = int(tamanho)
    if tamanho <= 0:
        raise VideoForaDosLimites("arquivo vazio: não há o que enviar")

    chunk = max(1, int(chunk_mb)) * MB
    if tamanho <= chunk:
        return tamanho, 1, [(0, tamanho - 1)]

    quantidade = int(math.floor(tamanho / chunk))
    faixas = [(i * chunk, (i + 1) * chunk - 1) for i in range(quantidade)]
    # O último engole a sobra, em vez de virar um pedaço abaixo do mínimo.
    faixas[-1] = (faixas[-1][0], tamanho - 1)
    return chunk, quantidade, faixas


def url_publica(caminho_render, base=None):
    """Caminho local -> URL pública, para o modo PULL_FROM_URL."""
    base = settings.CLIPS_BASE_URL if base is None else base
    if not base:
        raise ErroTikTok(
            "CLIPS_BASE_URL vazia: no modo 'url' a TikTok baixa o vídeo por "
            "HTTP. Configure a URL pública da pasta render/, ou use "
            "TIKTOK_MODO_UPLOAD=arquivo."
        )
    return f"{base.rstrip('/')}/{os.path.basename(caminho_render)}"


# --- publicação ---------------------------------------------------------------

def montar_post_info(caption, privacidade, criador):
    info = {
        "title": caption,
        "privacy_level": privacidade,
        "video_cover_timestamp_ms": 0,
    }
    info.update(opcoes_de_interacao(criador))
    return info


def iniciar(caption, privacidade, criador, token, caminho=None, tamanho=0,
            modo=None, http=None, base=None):
    """Abre a publicação. Devolve (publish_id, upload_url).

    upload_url vem vazia no modo 'url': lá quem baixa o arquivo é a TikTok.
    """
    modo = (modo or settings.TIKTOK_MODO_UPLOAD or MODO_ARQUIVO).lower()
    corpo = {"post_info": montar_post_info(caption, privacidade, criador)}

    if modo == MODO_URL:
        corpo["source_info"] = {
            "source": "PULL_FROM_URL",
            "video_url": url_publica(caminho),
        }
    else:
        chunk, quantidade, _faixas = plano_de_chunks(tamanho)
        corpo["source_info"] = {
            "source": "FILE_UPLOAD",
            "video_size": int(tamanho),
            "chunk_size": int(chunk),
            "total_chunk_count": int(quantidade),
        }

    dados = _pedir(_url("post/publish/video/init", base), token, corpo, http=http)
    publish_id = dados.get("publish_id")
    if not publish_id:
        raise ErroTikTok(f"init sem publish_id na resposta: {dados!r}")
    return publish_id, dados.get("upload_url") or ""


def enviar_arquivo(upload_url, caminho, tamanho=None, http=None, chunk_mb=None):
    """Manda os bytes para a URL que o init devolveu, pedaço a pedaço."""
    http = http or _http()
    tamanho = int(tamanho or os.path.getsize(caminho))
    _chunk, _quantidade, faixas = plano_de_chunks(tamanho, chunk_mb)

    with open(caminho, "rb") as arquivo:
        for inicio, fim in faixas:
            arquivo.seek(inicio)
            pedaco = arquivo.read(fim - inicio + 1)
            resposta = http.request(
                "PUT", upload_url, data=pedaco,
                headers={
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(pedaco)),
                    "Content-Range": f"bytes {inicio}-{fim}/{tamanho}",
                },
                timeout=600,
            )
            if resposta.status_code >= 400:
                raise ErroTikTok(
                    f"upload do pedaço {inicio}-{fim} falhou "
                    f"({resposta.status_code}): {(resposta.text or '')[:200]}"
                )
    return len(faixas)


def consultar_status(publish_id, token, http=None, base=None):
    return _pedir(_url("post/publish/status/fetch", base), token,
                  {"publish_id": publish_id}, http=http)


def esperar_publicacao(publish_id, token, http=None, base=None, tentativas=20,
                       espera_s=15, dormir=None):
    """Acompanha até a TikTok terminar. Devolve os dados do último status.

    O teto existe pelo mesmo motivo do Instagram: um vídeo problemático não
    pode segurar a execução para sempre. Aqui ele é mais provável — no modo
    'url' quem baixa o arquivo é a TikTok, e um servidor lento vira espera.
    """
    dormir = dormir or time.sleep
    dados = {}

    for tentativa in range(tentativas):
        dados = consultar_status(publish_id, token, http=http, base=base)
        status = str(dados.get("status") or "")
        if status == STATUS_PRONTO:
            return dados
        if status == STATUS_CAIXA_DE_ENTRADA:
            # Acontece quando o app só tem escopo de upload (video.upload) em
            # vez de publicação: o vídeo chega como rascunho e espera a pessoa
            # postar pelo celular. Não é falha, mas não é post.
            log.warning(
                "TikTok: publish_id %s foi para a caixa de entrada da conta, "
                "não para o feed. O app provavelmente tem o escopo "
                "video.upload em vez de video.publish.", publish_id,
            )
            return dados
        if status == STATUS_FALHOU:
            raise ErroTikTok(
                f"publicação {publish_id} falhou: "
                f"{dados.get('fail_reason') or 'sem motivo informado'}"
            )
        if tentativa < tentativas - 1:
            dormir(espera_s)

    raise ErroTikTok(
        f"publicação {publish_id} não terminou em {tentativas * espera_s}s "
        f"(último status: {dados.get('status')!r})"
    )


def identificar(publish_id, dados, criador):
    """(id gravado, url) do post que acabou de sair.

    Um post SELF_ONLY não ganha id público — a lista volta vazia, e é o
    esperado, não um erro. Nesse caso fica o publish_id, que é o que permite
    consultar o status depois, e a URL fica vazia em vez de apontar para uma
    página que ninguém consegue abrir.
    """
    publicos = (dados.get("publicaly_available_post_id")
                or dados.get("publicly_available_post_id") or [])
    if not publicos:
        return publish_id, ""

    post_id = str(publicos[0])
    usuario = str(criador.get("creator_username") or "").lstrip("@")
    if not usuario:
        return post_id, ""
    return post_id, f"https://www.tiktok.com/@{usuario}/video/{post_id}"


def publicar(conn, caminho_render, caption, duracao_s=None, http=None,
             dormir=None, agora=None, modo=None):
    """Todas as etapas numa chamada. Devolve (id_externo, url).

    A ordem não é arbitrária: o creator_info vem primeiro porque é ele que diz
    a duração máxima da conta e a privacidade possível — validar contra o
    default do settings e só depois descobrir que a conta é mais restrita
    gastaria o upload inteiro.
    """
    modo = (modo or settings.TIKTOK_MODO_UPLOAD or MODO_ARQUIVO).lower()
    token = garantir_token(conn, http=http, agora=agora)
    criador = consultar_criador(token, http=http)

    tamanho = validar_video(
        caminho_render, duracao_s=duracao_s,
        max_duracao_s=criador.get("max_video_post_duration_sec"),
        exigir_arquivo=(modo != MODO_URL),
    )

    privacidade, rebaixou = resolver_privacidade(criador)
    if rebaixou:
        # Alto de propósito: é a diferença entre um post que o público vê e um
        # que só o dono vê, e ela não aparece em nenhum outro lugar da saída.
        log.warning("TikTok publicando em modo restrito — %s.", rebaixou)

    publish_id, upload_url = iniciar(
        caption, privacidade, criador, token, caminho=caminho_render,
        tamanho=tamanho, modo=modo, http=http,
    )
    if modo != MODO_URL:
        if not upload_url:
            raise ErroTikTok(f"init do {publish_id} não devolveu upload_url")
        enviar_arquivo(upload_url, caminho_render, tamanho=tamanho, http=http)

    dados = esperar_publicacao(publish_id, token, http=http, dormir=dormir)
    id_externo, url = identificar(publish_id, dados, criador)
    log.info("TikTok: %s (%s)", url or "sem URL pública (post privado)",
             privacidade)
    return id_externo, url
