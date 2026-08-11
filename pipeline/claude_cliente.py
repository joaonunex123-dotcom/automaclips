"""Fiação compartilhada das chamadas ao Claude.

Existe porque o projeto tem duas chamadas ao modelo — escolher os trechos
(pipeline/highlight_detect.py) e escrever o metadado (publish/metadata.py) — e
tudo que não é o prompt é idêntico entre elas: onde o breakpoint de cache cai,
por que a chamada é em streaming, o que fazer quando `stop_reason` vem
'refusal' ou 'max_tokens', como o fallback server-side é pedido.

Duplicar isso significaria duas cópias do tratamento de recusa que envelhecem
em ritmos diferentes — e a segunda cópia é sempre a que ninguém lembra de
corrigir.

Cada chamador passa o próprio tipo de exceção: o erro que sobe é o do módulo
de quem chamou, não um erro genérico de infraestrutura que o chamador teria de
traduzir.
"""
import logging

import settings

log = logging.getLogger(__name__)

BETA_FALLBACK = "server-side-fallback-2026-07-01"


class ErroClaude(Exception):
    """Falha de configuração, de chamada ou de resposta do modelo."""


def construir_cliente(api_key=None, max_retries=None, erro=ErroClaude):
    """Cliente da Anthropic. Import local: módulo importável sem o SDK.

    max_retries cobre 429/5xx/erro de conexão com backoff exponencial dentro
    do próprio SDK. Não há retry manual em lugar nenhum deste projeto de
    propósito: seria uma segunda camada exponencial em cima da primeira.
    """
    api_key = api_key if api_key is not None else settings.ANTHROPIC_API_KEY
    if not api_key:
        raise erro(
            "ANTHROPIC_API_KEY não configurada. Preencha no .env (ver .env.example)."
        )
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - ambiente sem o SDK
        raise erro(
            "SDK anthropic não instalado (pip install -r requirements.txt)."
        ) from e
    return anthropic.Anthropic(
        api_key=api_key,
        max_retries=(settings.CLAUDE_MAX_RETRIES if max_retries is None
                     else max_retries),
    )


def blocos_system(sistema):
    """O system com o breakpoint de cache no fim.

    A ordem de renderização do prompt é tools -> system -> messages, então o
    breakpoint aqui cobre exatamente o prefixo estável e deixa de fora o que
    muda a cada chamada. Marcar o fim do prompt inteiro faria cada chamada
    gravar uma entrada nova no cache e nunca ler nenhuma.
    """
    return [
        {"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}
    ]


def contar_tokens(cliente, sistema, usuario, modelo=None):
    """Tamanho do prompt. Nunca estime por caractere."""
    resposta = cliente.messages.count_tokens(
        model=modelo or settings.CLAUDE_MODELO,
        system=blocos_system(sistema),
        messages=[{"role": "user", "content": usuario}],
    )
    return resposta.input_tokens


def chamar(cliente, sistema, usuario, esquema, modelo=None, max_tokens=None,
           effort=None, usar_fallbacks=None):
    """Uma chamada com saída estruturada. Devolve a mensagem final.

    `output_config.format` faz a resposta ser JSON válido por construção — o
    que apaga o parser tolerante, o retry-on-parse e a extração por regex que
    toda integração de LLM acumula. O schema NÃO aceita restrição numérica nem
    de tamanho, então faixa e limite continuam validados em Python, por quem
    chamou.
    """
    parametros = {
        "model": modelo or settings.CLAUDE_MODELO,
        "max_tokens": max_tokens or settings.CLAUDE_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort or settings.CLAUDE_EFFORT,
            "format": {"type": "json_schema", "schema": esquema},
        },
        "system": blocos_system(sistema),
        "messages": [{"role": "user", "content": usuario}],
    }

    if usar_fallbacks is None:
        usar_fallbacks = settings.CLAUDE_FALLBACKS

    # Streaming mesmo sem consumir os eventos: com entrada longa e raciocínio
    # adaptativo a requisição pode passar de minutos, e a versão não-streaming
    # estoura o timeout de HTTP antes de responder.
    if usar_fallbacks:
        with cliente.beta.messages.stream(
            betas=[BETA_FALLBACK], fallbacks="default", **parametros
        ) as stream:
            return stream.get_final_message()
    with cliente.messages.stream(**parametros) as stream:
        return stream.get_final_message()


def texto_da_resposta(mensagem, erro=ErroClaude):
    """Valida o desfecho e devolve o texto JSON.

    stop_reason é conferido ANTES de ler content: numa recusa o content vem
    vazio (ou parcial), e indexar content[0] direto quebraria com IndexError
    em vez de dizer o que aconteceu. Com fallback ligado, content também pode
    começar com um bloco `fallback` — por isso a busca é pelo primeiro bloco
    de TEXTO, não pelo primeiro bloco.
    """
    if mensagem.stop_reason == "refusal":
        detalhes = getattr(mensagem, "stop_details", None)
        categoria = getattr(detalhes, "category", None) if detalhes else None
        raise erro(
            f"Claude recusou o pedido (categoria: {categoria or 'não informada'})."
        )
    if mensagem.stop_reason == "max_tokens":
        raise erro(
            "resposta truncada por max_tokens — aumente CLAUDE_MAX_TOKENS "
            f"(atual: {settings.CLAUDE_MAX_TOKENS})."
        )
    texto = next((b.text for b in mensagem.content if b.type == "text"), None)
    if texto is None:
        raise erro(
            f"resposta sem bloco de texto (stop_reason={mensagem.stop_reason})."
        )
    return texto
