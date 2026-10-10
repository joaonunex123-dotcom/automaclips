"""A chave da API nunca aparece em log nem em mensagem de erro."""
import logging

import log_seguro

CHAVE = "AIzaSyFAKEKEYFAKEKEYFAKEKEY0123456789"
URL = (
    "https://youtube.googleapis.com/youtube/v3/channels?part=snippet"
    f"&id=UCabc&maxResults=50&key={CHAVE}&alt=json"
)


def test_mascara_chave_em_url():
    saida = log_seguro.mascarar(f"URL being requested: GET {URL}")
    assert CHAVE not in saida
    assert "key=***&alt=json" in saida
    assert "id=UCabc" in saida  # o resto da URL continua legível


def test_mascara_key_no_fim_e_primeiro_parametro():
    assert log_seguro.mascarar(f"x?key={CHAVE}") == "x?key=***"
    assert CHAVE not in log_seguro.mascarar(f"x?a=1&API_KEY={CHAVE}")


def test_mascara_valor_das_chaves_de_settings(monkeypatch):
    monkeypatch.setattr("settings.YOUTUBE_API_KEY", CHAVE)
    saida = log_seguro.mascarar(f"falhou com a chave {CHAVE} na requisição")
    assert CHAVE not in saida


def test_nao_str_passa_intacto_e_e_idempotente():
    assert log_seguro.mascarar(None) is None
    uma = log_seguro.mascarar(URL)
    assert log_seguro.mascarar(uma) == uma


def _capturar(caplog):
    caplog.set_level(logging.DEBUG)
    log_seguro.instalar()  # idempotente; garante a fábrica mesmo isolado


def test_log_debug_da_biblioteca_do_google_sai_mascarado(caplog):
    _capturar(caplog)
    logging.getLogger("googleapiclient.discovery").debug(
        "URL being requested: %s %s", "GET", URL
    )
    assert CHAVE not in caplog.text
    assert "key=***" in caplog.text


def test_excecao_com_url_sai_mascarada_no_traceback(caplog):
    _capturar(caplog)
    log = logging.getLogger("sourcing.youtube")
    try:
        raise RuntimeError(f"<HttpError 403 when requesting {URL}>")
    except RuntimeError as e:
        log.error("falha na coleta: %s", e)
        log.exception("de novo")
    assert CHAVE not in caplog.text
    assert any(r.exc_text and "key=***" in r.exc_text for r in caplog.records)
