"""Máscara de segredos em log e em mensagem de erro.

A biblioteca do Google loga em DEBUG a URL de cada chamada, com `?key=<chave>`,
e o `--verbose` do descobrir a mostra. O mesmo vale para o texto de um
HttpError. Em vez de filtrar handler por handler (há um basicConfig por
entrypoint), o registro já nasce mascarado: `instalar()` troca a fábrica de
LogRecord, então o que sai por qualquer handler, de qualquer logger, está limpo.

Duas frentes, porque a chave aparece de dois jeitos:
  - em query string (`key=`, `api_key=`, `access_token=`...), mascarada pelo
    NOME do parâmetro — pega também chaves que não estão em settings;
  - literalmente, pelo VALOR das chaves de settings, para o caso de ela entrar
    numa mensagem fora de uma URL.
"""
import logging
import re
import traceback

MASCARA = "***"

_PARAMETRO = re.compile(
    r"(?i)([?&;\s](?:key|api_key|apikey|access_token|token|client_secret)=)"
    r"[^&\s'\"<>]+"
)

# Nomes em settings cujo VALOR nunca pode aparecer em log.
_NOMES_SEGREDO = (
    "YOUTUBE_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY",
)
_TAMANHO_MINIMO = 8  # abaixo disso o valor é ruído e mascarar estragaria texto


def _valores_secretos():
    try:
        import settings
    except Exception:
        return []
    valores = []
    for nome in _NOMES_SEGREDO:
        valor = getattr(settings, nome, "") or ""
        if len(valor) >= _TAMANHO_MINIMO:
            valores.append(valor)
    return valores


def mascarar(texto):
    """Devolve `texto` sem chaves de API. Idempotente; não-str passa intacto."""
    if not isinstance(texto, str) or not texto:
        return texto
    texto = _PARAMETRO.sub(lambda m: m.group(1) + MASCARA, texto)
    for valor in _valores_secretos():
        texto = texto.replace(valor, MASCARA)
    return texto


_instalado = False


def instalar():
    """Idempotente: troca a fábrica de LogRecord por uma que mascara."""
    global _instalado
    if _instalado:
        return
    original = logging.getLogRecordFactory()

    def fabrica(*args, **kwargs):
        registro = original(*args, **kwargs)
        try:
            registro.msg = mascarar(registro.getMessage())
            registro.args = ()
            if registro.exc_info and registro.exc_info[0] is not None:
                registro.exc_text = mascarar(
                    "".join(traceback.format_exception(*registro.exc_info)).rstrip("\n")
                )
        except Exception:
            # Nunca derrubar o programa por causa do log; sem poder garantir a
            # máscara, descarta a mensagem em vez de deixar o original passar.
            registro.msg = "[mensagem de log suprimida: falha ao mascarar]"
            registro.args = ()
            registro.exc_info = None
            registro.exc_text = None
        return registro

    logging.setLogRecordFactory(fabrica)
    _instalado = True
