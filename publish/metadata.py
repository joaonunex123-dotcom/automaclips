"""Título, descrição, caption e hashtags de cada clip, pelo Claude.

Uma chamada por clip produz o metadado das DUAS plataformas de uma vez. Duas
chamadas separadas custariam o dobro e ainda dariam textos que não conversam
entre si — e o material de entrada é o mesmo: o trecho, o gancho e de onde ele
saiu.

O que entra no prompt é curto (a fala do trecho, não a transcrição inteira),
então esta chamada é barata perto do `highlight_detect`. Vale o mesmo cuidado
de sempre com o cache: o system é idêntico entre clips e leva o breakpoint; o
contexto do clip vem depois dele.

Limite de tamanho é aplicado AQUI, em Python, e não pedido ao modelo: "peça ao
modelo para não passar de 100 caracteres" é exatamente o tipo de restrição que
ele cumpre quase sempre — o que significa que a plataforma recusa o post de vez
em quando, no horário agendado, sem ninguém olhando.

Esta etapa roda no OpenRouter (`llm_client`), não no Claude direto: escrever
caption é trabalho de menor exigência que escolher o trecho, e modelo mais
barato resolve. A consequência é que a saída estruturada GARANTIDA se perde —
por isso o formato do JSON é descrito no prompt (derivado do ESQUEMA, para os
dois não divergirem) e há um modelo de fallback quando a resposta vem
ininteligível.
"""
import logging
import re

import llm_client
import settings
from db import repositorio

log = logging.getLogger(__name__)


class ErroMetadata(llm_client.ErroLLM):
    """Falha ao gerar o metadado."""


_INSTRUCOES = """\
Você escreve o texto que acompanha um clip vertical curto (Shorts, Reels) ao \
ser publicado.

Recebe: o trecho falado, o gancho escolhido para a abertura, e de qual vídeo e \
canal ele saiu.

Escreva no MESMO idioma da fala do trecho.

O título precisa fazer alguém parar de rolar o feed sem prometer o que o clip \
não entrega — título que promete mais do que o vídeo tem derruba a retenção e \
o algoritmo pune isso mais do que puniria um título morno. Nada de "VOCÊ NÃO \
VAI ACREDITAR", nada de reticências deixando o assunto em segredo.

A descrição do YouTube tem duas ou três linhas: o que acontece no clip, e o \
crédito ao canal de origem.

A caption do Instagram é mais solta e mais curta que a descrição, escrita para \
ser lida embaixo do vídeo, e pode terminar com uma pergunta se o assunto pedir \
uma. Não repita o título palavra por palavra.

As hashtags são específicas do assunto, não genéricas de plataforma. \
"#viral" e "#fyp" não dizem nada sobre o conteúdo e competem com milhões de \
posts; o nome do tema, da pessoa ou do nicho competem com centenas. Devolva \
sem o '#', uma palavra ou expressão por item."""


ESQUEMA = {
    "type": "object",
    "properties": {
        "titulo": {"type": "string"},
        "descricao": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["titulo", "descricao", "caption", "hashtags"],
    "additionalProperties": False,
}


def descricao_do_formato(esquema=None):
    """O formato do JSON, em texto, DERIVADO do ESQUEMA.

    Escrever a forma à mão no prompt criaria duas fontes de verdade que se
    afastam na primeira vez que alguém acrescentar um campo — e o sintoma
    seria o modelo devolvendo uma chave que o código não lê, em silêncio.
    """
    esquema = esquema or ESQUEMA
    tipos = {"string": '"..."', "array": '["...", "..."]'}
    campos = [
        f'  "{nome}": {tipos.get(corpo.get("type"), "...")}'
        for nome, corpo in esquema["properties"].items()
    ]
    return "Responda com um objeto JSON, e nada além dele:\n{\n" + ",\n".join(campos) + "\n}"


def montar_sistema(max_hashtags=None, limite_titulo=None):
    """System prompt. Estável entre clips."""
    max_hashtags = settings.MAX_HASHTAGS if max_hashtags is None else max_hashtags
    limite_titulo = (settings.LIMITE_TITULO_YOUTUBE if limite_titulo is None
                     else limite_titulo)
    return (
        f"{_INSTRUCOES}\n\n"
        f"O título precisa caber em {limite_titulo:d} caracteres. "
        f"Devolva até {max_hashtags:d} hashtags.\n\n"
        f"{descricao_do_formato()}"
    )


def montar_contexto(clip, fala="", titulo_fonte="", canal=""):
    """A parte que muda a cada clip. Vai DEPOIS do system, fora do cache."""
    partes = []
    if canal:
        partes.append(f"Canal de origem: {canal}")
    if titulo_fonte:
        partes.append(f"Vídeo de origem: {titulo_fonte}")
    if clip.get("hook_text"):
        partes.append(f"Gancho da abertura: {clip['hook_text']}")
    if clip.get("motivo"):
        partes.append(f"Por que este trecho foi escolhido: {clip['motivo']}")
    partes.append("")
    partes.append("Fala do trecho:")
    partes.append(fala or "(sem transcrição disponível)")
    return "\n".join(partes)


def fala_do_trecho(transcricao, inicio_s, fim_s):
    """O que é dito dentro do trecho, como texto corrido.

    Só o trecho, e não a transcrição inteira: o metadado descreve os 45
    segundos que vão ao ar, e mandar as quatro horas do vídeo custaria caro
    para piorar o resultado — o modelo passaria a resumir o vídeo, não o clip.
    """
    partes = []
    for segmento in (transcricao or {}).get("segmentos", []):
        if segmento.get("fim", 0) <= inicio_s or segmento.get("inicio", 0) >= fim_s:
            continue
        texto = (segmento.get("texto") or "").strip()
        if texto:
            partes.append(texto)
    return " ".join(partes)


def _limpar_hashtag(bruta):
    """'#Meu Assunto!' -> 'meuassunto'. Vazio quando não sobra nada."""
    texto = str(bruta).strip().lstrip("#")
    # Plataforma nenhuma aceita espaço ou pontuação dentro da hashtag; colar as
    # palavras é o que o humano faria à mão.
    return re.sub(r"[^0-9A-Za-zÀ-ÿ_]", "", texto).lower()


def normalizar_hashtags(brutas, maximo=None):
    """Limpa, tira repetição e corta no teto, preservando a ordem."""
    maximo = settings.MAX_HASHTAGS if maximo is None else maximo
    vistas, saida = set(), []
    for bruta in brutas or []:
        limpa = _limpar_hashtag(bruta)
        if limpa and limpa not in vistas:
            vistas.add(limpa)
            saida.append(limpa)
    return saida[:maximo]


def _cortar(texto, limite):
    """Corta no limite sem partir palavra, quando dá para não partir."""
    texto = " ".join(str(texto or "").split())
    if len(texto) <= limite:
        return texto
    cortado = texto[:limite]
    espaco = cortado.rfind(" ")
    # Só respeita a palavra se sobrar texto de verdade; um limite muito curto
    # não pode devolver string vazia por causa disso.
    if espaco > limite * 0.6:
        cortado = cortado[:espaco]
    return cortado.rstrip(" ,.;:-")


def normalizar(bruto, limites=None):
    """Resposta do modelo -> metadado pronto para gravar, dentro dos limites."""
    limites = limites or {}
    titulo = limites.get("titulo", settings.LIMITE_TITULO_YOUTUBE)
    descricao = limites.get("descricao", settings.LIMITE_DESCRICAO_YOUTUBE)
    caption = limites.get("caption", settings.LIMITE_CAPTION_INSTAGRAM)

    return {
        "titulo": _cortar(bruto.get("titulo"), titulo),
        "descricao": _cortar(bruto.get("descricao"), descricao),
        "caption": _cortar(bruto.get("caption"), caption),
        "hashtags": normalizar_hashtags(bruto.get("hashtags")),
    }


def gerar(clip, fala="", titulo_fonte="", canal="", cliente=None, modelo=None,
          fallback=None, conn=None):
    """Metadado de um clip. Devolve o dict já normalizado e cortado.

    `conn` é opcional e serve só ao registro de qual modelo respondeu — a
    geração funciona sem banco, o que mantém a função testável isolada.
    """
    modelo = modelo or settings.MODEL_METADATA
    fallback = settings.MODEL_FALLBACK if fallback is None else fallback

    try:
        dados, detalhes = llm_client.call_llm(
            montar_contexto(clip, fala, titulo_fonte, canal),
            model=modelo,
            system=montar_sistema(),
            expect_json=True,
            fallback_model=fallback,
            cliente=cliente,
            com_detalhes=True,
        )
    except llm_client.ErroLLM as e:
        raise ErroMetadata(str(e)) from e

    if not isinstance(dados, dict):
        raise ErroMetadata(f"resposta não é um objeto JSON: {type(dados).__name__}")

    if conn is not None:
        repositorio.registrar_geracao(
            conn, repositorio.ETAPA_METADATA, modelo,
            referencia=clip.get("id", ""),
            modelo_respondeu=detalhes.get("modelo_respondeu", ""),
            usou_fallback=detalhes.get("usou_fallback", False),
            tokens_entrada=detalhes.get("tokens_entrada"),
            tokens_saida=detalhes.get("tokens_saida"),
        )

    meta = normalizar(dados)
    if not meta["titulo"]:
        # Sem título não há o que publicar no YouTube, e um título vazio só
        # apareceria na hora do upload — no horário agendado, sem ninguém
        # olhando.
        raise ErroMetadata("o modelo devolveu título vazio")
    log.info("Metadado gerado por %s: %r (%d hashtags).",
             detalhes.get("modelo_respondeu"), meta["titulo"],
             len(meta["hashtags"]))
    return meta


# --- montagem por plataforma --------------------------------------------------

def para_youtube(meta, url_fonte=""):
    """Título, descrição e tags no formato que o upload espera.

    As hashtags entram no CORPO da descrição, além de irem como tags: no
    YouTube são as três primeiras da descrição que aparecem acima do título,
    e as tags puras têm peso pequeno hoje.
    """
    linhas = [meta["descricao"]]
    if url_fonte:
        linhas += ["", f"Trecho de: {url_fonte}"]
    if meta["hashtags"]:
        linhas += ["", " ".join(f"#{h}" for h in meta["hashtags"])]

    return {
        "titulo": _cortar(meta["titulo"], settings.LIMITE_TITULO_YOUTUBE),
        "descricao": _cortar("\n".join(linhas), settings.LIMITE_DESCRICAO_YOUTUBE),
        "tags": list(meta["hashtags"]),
    }


def para_instagram(meta):
    """Caption com as hashtags no fim, dentro do limite da plataforma."""
    corpo = meta["caption"] or meta["titulo"]
    marcas = " ".join(f"#{h}" for h in meta["hashtags"])
    texto = f"{corpo}\n\n{marcas}".strip() if marcas else corpo
    return {"caption": _cortar(texto, settings.LIMITE_CAPTION_INSTAGRAM)}
