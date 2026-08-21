"""Chamada a modelos via OpenRouter, para as etapas de menor exigência.

Quem passa por aqui: `publish/metadata.py` (título, caption, hashtags),
`analytics/recalibrate.py` e — só quando HIGHLIGHT_PROVEDOR não é 'anthropic' —
`pipeline/highlight_detect.py`.

Dois destinos possíveis pelo mesmo SDK, escolhidos em LLM_PROVEDOR: o
OpenRouter (catálogo grande, modelos baratos) e a própria OpenAI (útil quando
o crédito já está lá). Muda a base_url e a chave; o resto é idêntico.

O que muda ao sair da API da Anthropic, e por que este módulo tem o formato
que tem:

**A saída estruturada garantida acabou.** Com `output_config.format`, a
resposta era JSON válido *por construção*. No caminho compatível com OpenAI o
melhor disponível é `response_format={"type": "json_object"}`, que pede JSON
mas não o garante — e modelos menores devolvem cerca com ```json, prosa antes
do objeto, ou vírgula sobrando. Daí as duas defesas, nesta ordem:

1. um extrator tolerante, que tira cerca e prosa e tenta de novo;
2. o `fallback_model`, que refaz a chamada num modelo mais forte.

A segunda existe porque a primeira não cobre tudo: JSON sintaticamente válido
com a chave errada passa pelo extrator e quebra depois. Reprocessar custa uma
chamada; perder o clip custa o clip.

**Qual modelo respondeu é registrado.** Sem isso não há como comparar custo
contra qualidade na etapa 7 — e o modelo que responde nem sempre é o pedido,
porque o fallback pode ter entrado no meio.
"""
import json
import logging
import re

import settings

log = logging.getLogger(__name__)


class ErroLLM(Exception):
    """Falha de configuração, de chamada ou de resposta do modelo."""


# ```json ... ``` ou ``` ... ```, que é como modelo menor costuma embrulhar.
_CERCA = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def credenciais(provedor=None):
    """(api_key, base_url, nome_da_variavel) do provedor escolhido.

    Os dois falam o mesmo protocolo pelo mesmo SDK; o que muda é para onde
    apontar e qual chave usar. Devolver o NOME da variável junto é o que
    permite a mensagem de erro dizer exatamente qual linha do .env preencher —
    "sem chave" com dois provedores possíveis não ajuda ninguém.
    """
    provedor = (provedor or settings.LLM_PROVEDOR or "").strip().lower()
    if provedor == "openai":
        return settings.OPENAI_API_KEY, settings.OPENAI_BASE_URL, "OPENAI_API_KEY"
    if provedor in ("", "openrouter"):
        return (settings.OPENROUTER_API_KEY, settings.OPENROUTER_BASE_URL,
                "OPENROUTER_API_KEY")
    raise ErroLLM(
        f"LLM_PROVEDOR inválido: {provedor!r} (use 'openrouter' ou 'openai')."
    )


def construir_cliente(api_key=None, base_url=None, max_retries=None,
                      provedor=None):
    """Cliente OpenAI-compatível, apontado para o provedor configurado.

    O SDK da `openai` já é dependência do projeto (a transcrição usa a Whisper
    API), então falar com o OpenRouter — ou com a própria OpenAI — não
    acrescenta dependência nenhuma: só muda a `base_url`.
    """
    padrao_key, padrao_url, nome_variavel = credenciais(provedor)
    api_key = api_key if api_key is not None else padrao_key
    base_url = base_url or padrao_url
    if not api_key:
        raise ErroLLM(
            f"{nome_variavel} não configurada. Preencha no .env "
            "(ver .env.example)."
        )
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover - ambiente sem o SDK
        raise ErroLLM(
            "SDK openai não instalado (pip install -r requirements.txt)."
        ) from e
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=(settings.OPENROUTER_MAX_RETRIES if max_retries is None
                     else max_retries),
    )


def extrair_json(texto):
    """Texto do modelo -> objeto. Levanta ValueError se não der.

    Três tentativas, da mais limpa para a mais tolerante. Nenhuma delas
    "conserta" JSON quebrado: só descascam o que o modelo pôs em volta.
    """
    bruto = (texto or "").strip()
    if not bruto:
        raise ValueError("resposta vazia")

    try:
        return json.loads(bruto)
    except json.JSONDecodeError:
        pass

    cercado = _CERCA.search(bruto)
    if cercado:
        try:
            return json.loads(cercado.group(1))
        except json.JSONDecodeError:
            pass

    # Último recurso: o maior trecho entre a primeira chave e a última. Pega o
    # caso de prosa antes ou depois do objeto, que é o mais comum.
    inicio, fim = bruto.find("{"), bruto.rfind("}")
    if inicio != -1 and fim > inicio:
        try:
            return json.loads(bruto[inicio:fim + 1])
        except json.JSONDecodeError:
            pass

    raise ValueError(f"não há JSON interpretável na resposta: {bruto[:200]!r}")


def _mensagens(prompt, system=None):
    mensagens = []
    if system:
        mensagens.append({"role": "system", "content": system})
    mensagens.append({"role": "user", "content": prompt})
    return mensagens


def _uma_chamada(cliente, prompt, model, system, expect_json, max_tokens,
                 temperature):
    parametros = {
        "model": model,
        "messages": _mensagens(prompt, system),
    }
    if max_tokens:
        parametros["max_tokens"] = max_tokens
    if temperature is not None:
        parametros["temperature"] = temperature
    if expect_json:
        parametros["response_format"] = {"type": "json_object"}

    try:
        resposta = cliente.chat.completions.create(**parametros)
    except Exception as e:
        # Slug de modelo errado volta como 404 com o nome dentro; deixar a
        # mensagem original passar é o que torna isso diagnosticável.
        raise ErroLLM(f"chamada a {model} falhou: {e}") from e

    escolhas = getattr(resposta, "choices", None) or []
    if not escolhas:
        raise ErroLLM(f"{model} respondeu sem choices")
    conteudo = getattr(escolhas[0].message, "content", None)
    if conteudo is None:
        raise ErroLLM(f"{model} respondeu sem conteúdo")

    uso = getattr(resposta, "usage", None)
    return conteudo, {
        "modelo_pedido": model,
        # O OpenRouter pode rotear para uma variante; `resposta.model` é quem
        # realmente atendeu, e é esse que interessa comparar depois.
        "modelo_respondeu": getattr(resposta, "model", None) or model,
        "tokens_entrada": getattr(uso, "prompt_tokens", None) if uso else None,
        "tokens_saida": getattr(uso, "completion_tokens", None) if uso else None,
    }


def call_llm(prompt, model, expect_json=True, fallback_model=None, system=None,
             cliente=None, max_tokens=None, temperature=None, com_detalhes=False):
    """Chama `model` via OpenRouter. Devolve dict (expect_json) ou str.

    Com `com_detalhes=True`, devolve `(resultado, detalhes)` — onde detalhes
    traz qual modelo respondeu de fato e se o fallback entrou. É o que a etapa
    7 vai usar para comparar custo contra qualidade.
    """
    cliente = cliente if cliente is not None else construir_cliente()

    conteudo, detalhes = _uma_chamada(
        cliente, prompt, model, system, expect_json, max_tokens, temperature
    )
    detalhes["usou_fallback"] = False

    if not expect_json:
        log.info("Resposta de %s (%s).", model, detalhes["modelo_respondeu"])
        return (conteudo, detalhes) if com_detalhes else conteudo

    try:
        resultado = extrair_json(conteudo)
    except ValueError as e:
        if not fallback_model or fallback_model == model:
            raise ErroLLM(f"{model} devolveu resposta ininteligível: {e}") from e

        log.warning("%s devolveu resposta malformada (%s); refazendo em %s.",
                    model, e, fallback_model)
        conteudo, detalhes = _uma_chamada(
            cliente, prompt, fallback_model, system, expect_json, max_tokens,
            temperature,
        )
        detalhes["usou_fallback"] = True
        try:
            resultado = extrair_json(conteudo)
        except ValueError as e2:
            raise ErroLLM(
                f"nem {model} nem o fallback {fallback_model} devolveram JSON: {e2}"
            ) from e2

    log.info("Resposta de %s (%s)%s.", model, detalhes["modelo_respondeu"],
             " [fallback]" if detalhes["usou_fallback"] else "")
    return (resultado, detalhes) if com_detalhes else resultado
