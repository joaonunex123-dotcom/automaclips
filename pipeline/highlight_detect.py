"""Seleção dos trechos pelo Claude, sobre a transcrição inteira.

Desenho da chamada, e o porquê de cada peça:

* **Transcrição inteira num prompt só.** Opus 5 tem 1M de contexto, o que cobre
  um podcast de horas com folga. Janela deslizante economizaria contexto e
  perderia justamente o que interessa: a piada que só fecha porque foi montada
  oito minutos antes.

* **Structured outputs** (`output_config.format`) em vez de pedir JSON no texto
  e torcer. Com o schema declarado, a resposta é JSON válido por construção —
  o que elimina o parser tolerante, o retry-on-parse e a extração por regex que
  toda integração de LLM acumula. O schema NÃO aceita restrição numérica
  (`minimum`/`maximum`), então faixa de score e de duração continuam sendo
  validadas aqui no código.

* **Prompt caching no system.** O system é idêntico para todos os vídeos e a
  transcrição é o que muda; com o breakpoint no fim do system, cada vídeo novo
  lê o prefixo do cache em vez de reprocessá-lo. É o padrão "prefixo comum,
  sufixo variável" — pôr o breakpoint no fim do prompt inteiro faria cada vídeo
  gravar uma entrada nova e nunca ler nenhuma. Os exemplos few-shot da etapa 7
  entram no system de propósito: mudam uma vez por dia (quando o recalibrate
  roda), não uma vez por vídeo.

* **Fallback server-side.** Transcrição de canal alheio é conteúdo que ninguém
  aqui revisou; se os classificadores recusarem, a API refaz num modelo de
  fallback dentro da mesma chamada em vez de devolver a recusa e derrubar o
  vídeo. Desligável por CLAUDE_FALLBACKS.

Sobre o texto do prompt: ele é direto e sem ênfase artificial de propósito.
Modelos atuais seguem instrução de perto, e "CRÍTICO: você DEVE" faz a regra
disparar onde não devia. Pelo mesmo motivo não há pedido de auto-verificação
("confira sua resposta"): no Opus 5 isso produz verificação em excesso, não
qualidade a mais.
"""
import json
import logging

import settings

log = logging.getLogger(__name__)

BETA_FALLBACK = "server-side-fallback-2026-07-01"


class ErroHighlight(Exception):
    """Falha de configuração, de chamada ou de resposta do Claude."""


# --- prompt -------------------------------------------------------------------

_INSTRUCOES = """\
Você seleciona os trechos de um vídeo que têm mais chance de funcionar como \
clip vertical curto (Shorts, Reels, TikTok).

Você recebe a transcrição completa de um vídeo. Cada linha começa com o \
instante em segundos em que aquela fala começa.

O que faz um trecho funcionar:
- Ele se sustenta sozinho. Quem cai nele sem ter visto nada antes entende do \
que se trata nos primeiros segundos.
- Tem uma virada: uma revelação, uma opinião forte, uma história com desfecho, \
um erro admitido, uma discordância real.
- Começa já dentro do assunto, não na preparação dele.
- Termina no ponto alto, não na explicação que vem depois.

O que não funciona: abertura, agradecimento, leitura de patrocínio, transição \
administrativa, resposta genérica, e qualquer trecho que só faça sentido para \
quem viu os dez minutos anteriores.

Escala do score, de 0 a 10:
- 8 a 10: você apostaria nesse trecho; a virada é clara e chega rápido.
- 6 a 7: funciona, mas depende de uma edição boa.
- 4 a 5: morno; tem assunto, não tem momento.
- 0 a 3: não use.

Pontue o trecho pelo que ele é, não pela posição dele na sua lista. Vários \
trechos podem ter a mesma nota, e um vídeo fraco deve receber notas baixas em \
todos.

Se o vídeo não tiver a quantidade pedida de trechos bons, devolva menos. Uma \
lista curta de trechos que funcionam vale mais do que uma lista cheia."""

_CAMPOS = """\
Para cada trecho:
- start: o segundo em que ele começa.
- end: o segundo em que ele termina.
- score: a nota, de 0 a 10, pela escala acima.
- motivo: uma frase dizendo o que faz esse trecho funcionar. É o que um humano \
vai ler para decidir se o critério está calibrado, então diga o que acontece \
ali, não que "é engajante".
- hook: a frase que vai aparecer escrita na tela no primeiro segundo do clip. \
Até oito palavras, tirada do próprio trecho ou fiel a ele, escrita para fazer \
quem está rolando o feed parar."""


def montar_sistema(exemplos=None, duracao_min=None, duracao_max=None,
                   quantidade=None):
    """Monta o system prompt. Estável entre vídeos — é o que o cache guarda.

    `exemplos` é a lista de few-shot que a etapa 7 (recalibrate) preenche com
    os clips do decil superior de performance real. Vazio hoje: sem clip
    publicado ainda, qualquer exemplo aqui seria palpite apresentado ao modelo
    como evidência.
    """
    duracao_min = duracao_min or settings.CLIP_DURACAO_MINIMA_S
    duracao_max = duracao_max or settings.CLIP_DURACAO_MAXIMA_S
    quantidade = quantidade or settings.CLIPS_POR_VIDEO

    partes = [
        _INSTRUCOES,
        "",
        f"Devolva até {quantidade:d} trechos, cada um com duração entre "
        f"{duracao_min:.0f} e {duracao_max:.0f} segundos.",
        "",
        _CAMPOS,
    ]

    if exemplos:
        partes += ["", "Exemplos de trechos que performaram bem neste canal:"]
        for ex in exemplos:
            partes.append(
                f"- \"{ex.get('hook_text', '')}\" — {ex.get('motivo', '')}"
            )
    return "\n".join(partes)


# Restrição numérica (minimum/maximum) e de tamanho não são suportadas por
# structured outputs — a faixa de score e a duração são validadas em Python,
# logo abaixo. `additionalProperties: false` e `required` são obrigatórios.
ESQUEMA = {
    "type": "object",
    "properties": {
        "trechos": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start": {"type": "number"},
                    "end": {"type": "number"},
                    "score": {"type": "number"},
                    "motivo": {"type": "string"},
                    "hook": {"type": "string"},
                },
                "required": ["start", "end", "score", "motivo", "hook"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["trechos"],
    "additionalProperties": False,
}


# --- cliente ------------------------------------------------------------------

def construir_cliente(api_key=None, max_retries=None):
    """Cliente da Anthropic. Import local: módulo importável sem o SDK.

    max_retries cobre 429/5xx/erro de conexão com backoff exponencial dentro do
    próprio SDK — não há retry manual neste arquivo de propósito, seria uma
    segunda camada exponencial em cima da primeira.
    """
    api_key = api_key if api_key is not None else settings.ANTHROPIC_API_KEY
    if not api_key:
        raise ErroHighlight(
            "ANTHROPIC_API_KEY não configurada. Preencha no .env (ver .env.example)."
        )
    try:
        import anthropic
    except ImportError as e:  # pragma: no cover - ambiente sem o SDK
        raise ErroHighlight(
            "SDK anthropic não instalado (pip install -r requirements.txt)."
        ) from e
    return anthropic.Anthropic(
        api_key=api_key,
        max_retries=(settings.CLAUDE_MAX_RETRIES if max_retries is None else max_retries),
    )


def _blocos_system(sistema):
    # cache_control no ÚLTIMO (aqui, único) bloco do system: a ordem de
    # renderização é tools -> system -> messages, então o breakpoint aqui cobre
    # todo o prefixo estável e deixa a transcrição de fora, que é o objetivo.
    return [
        {"type": "text", "text": sistema, "cache_control": {"type": "ephemeral"}}
    ]


def contar_tokens(cliente, sistema, usuario, modelo=None):
    """Tamanho do prompt, para a guarda de custo. Nunca estime por caractere."""
    resposta = cliente.messages.count_tokens(
        model=modelo or settings.CLAUDE_MODELO,
        system=_blocos_system(sistema),
        messages=[{"role": "user", "content": usuario}],
    )
    return resposta.input_tokens


def _chamar(cliente, sistema, usuario, modelo=None, max_tokens=None, effort=None,
            usar_fallbacks=None):
    parametros = {
        "model": modelo or settings.CLAUDE_MODELO,
        "max_tokens": max_tokens or settings.CLAUDE_MAX_TOKENS,
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": effort or settings.CLAUDE_EFFORT,
            "format": {"type": "json_schema", "schema": ESQUEMA},
        },
        "system": _blocos_system(sistema),
        "messages": [{"role": "user", "content": usuario}],
    }

    if usar_fallbacks is None:
        usar_fallbacks = settings.CLAUDE_FALLBACKS

    # Streaming mesmo sem consumir os eventos: com transcrição longa e
    # raciocínio adaptativo a requisição pode passar de minutos, e a versão
    # não-streaming estoura o timeout de HTTP antes de responder.
    if usar_fallbacks:
        with cliente.beta.messages.stream(
            betas=[BETA_FALLBACK], fallbacks="default", **parametros
        ) as stream:
            return stream.get_final_message()
    with cliente.messages.stream(**parametros) as stream:
        return stream.get_final_message()


def _texto_da_resposta(mensagem):
    """Valida o desfecho e devolve o texto JSON.

    stop_reason é conferido ANTES de ler content: numa recusa o content vem
    vazio (ou parcial), e indexar content[0] direto quebraria com IndexError em
    vez de dizer o que aconteceu. Com fallback ligado, content também pode
    começar com um bloco `fallback` — por isso a busca é pelo primeiro bloco de
    texto, não pelo primeiro bloco.
    """
    if mensagem.stop_reason == "refusal":
        detalhes = getattr(mensagem, "stop_details", None)
        categoria = getattr(detalhes, "category", None) if detalhes else None
        raise ErroHighlight(
            f"Claude recusou a transcrição (categoria: {categoria or 'não informada'})."
        )
    if mensagem.stop_reason == "max_tokens":
        raise ErroHighlight(
            "resposta truncada por max_tokens — aumente CLAUDE_MAX_TOKENS "
            f"(atual: {settings.CLAUDE_MAX_TOKENS})."
        )
    texto = next((b.text for b in mensagem.content if b.type == "text"), None)
    if texto is None:
        raise ErroHighlight(
            f"resposta sem bloco de texto (stop_reason={mensagem.stop_reason})."
        )
    return texto


def _sanear(trechos):
    """Descarta o que não é um trecho utilizável, antes de qualquer regra.

    Aqui só entra o que torna o trecho impossível de interpretar (não numérico,
    fim antes do início, início negativo). Duração, limiar e sobreposição são
    decisões de SELEÇÃO e ficam em select_clips — separadas porque uma é sobre
    o dado estar íntegro e a outra sobre a política de corte, e as duas mudam
    por motivos diferentes.
    """
    limpos = []
    for bruto in trechos:
        try:
            inicio = float(bruto["start"])
            fim = float(bruto["end"])
            score = float(bruto["score"])
        except (KeyError, TypeError, ValueError):
            log.warning("Trecho descartado (campos inválidos): %r", bruto)
            continue
        if inicio < 0 or fim <= inicio:
            log.warning("Trecho descartado (intervalo inválido): %.1f..%.1f", inicio, fim)
            continue
        limpos.append(
            {
                "inicio_s": inicio,
                "fim_s": fim,
                # A escala é 0–10; o schema não sabe disso (structured outputs
                # não aceita minimum/maximum), então o corte é aqui.
                "score_claude": max(0.0, min(10.0, score)),
                "motivo": (bruto.get("motivo") or "").strip(),
                "hook_text": (bruto.get("hook") or "").strip(),
            }
        )
    return limpos


def detectar(transcricao_texto, cliente=None, exemplos=None, modelo=None,
             max_tokens=None, effort=None, usar_fallbacks=None,
             limite_tokens=None):
    """Trechos candidatos a partir da transcrição já formatada.

    Devolve dicts no vocabulário do banco (inicio_s, fim_s, score_claude,
    motivo, hook_text) — a tradução dos nomes da API acontece aqui, e não
    espalhada pelo resto do pipeline.
    """
    if not (transcricao_texto or "").strip():
        raise ErroHighlight("transcrição vazia — nada a analisar.")

    cliente = cliente if cliente is not None else construir_cliente()
    sistema = montar_sistema(exemplos)

    limite = settings.TRANSCRICAO_MAX_TOKENS if limite_tokens is None else limite_tokens
    tokens = contar_tokens(cliente, sistema, transcricao_texto, modelo=modelo)
    if limite and tokens > limite:
        raise ErroHighlight(
            f"transcrição com {tokens} tokens excede o teto de {limite} "
            "(TRANSCRICAO_MAX_TOKENS). Vídeo pulado sem custo de API."
        )
    log.info("Enviando %d tokens de transcrição ao %s.", tokens,
             modelo or settings.CLAUDE_MODELO)

    mensagem = _chamar(
        cliente, sistema, transcricao_texto, modelo=modelo, max_tokens=max_tokens,
        effort=effort, usar_fallbacks=usar_fallbacks,
    )
    texto = _texto_da_resposta(mensagem)

    try:
        dados = json.loads(texto)
    except json.JSONDecodeError as e:
        # Com output_config.format isto não deveria acontecer; se acontecer, é
        # sinal de mudança no contrato da API, não de "LLM sendo LLM".
        raise ErroHighlight(f"resposta não é JSON válido: {e}") from e

    trechos = _sanear(dados.get("trechos") or [])
    log.info("Claude devolveu %d trechos, %d utilizáveis.",
             len(dados.get("trechos") or []), len(trechos))
    return trechos
