"""Quota diária do YouTube — sobretudo a regra do dia do Pacífico."""
from datetime import datetime, timedelta, timezone

import pytest

import settings
from publish import quota


# --- o dia da quota -----------------------------------------------------------

def test_dia_e_o_do_pacifico_nao_o_local():
    # 03:00 UTC de 11/08 ainda é 10/08 no Pacífico. Contar pela data local (ou
    # por UTC) faria o programa achar que tem quota nova e gastar uploads que
    # o Google ainda conta no dia anterior.
    agora = datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc)
    assert quota.dia_de_quota(agora) == "2026-08-10"


def test_dia_vira_a_meia_noite_do_pacifico():
    antes = datetime(2026, 8, 11, 6, 59, tzinfo=timezone.utc)   # 23:59 PDT
    depois = datetime(2026, 8, 11, 7, 30, tzinfo=timezone.utc)  # 00:30 PDT
    assert quota.dia_de_quota(antes) != quota.dia_de_quota(depois)


def test_agora_ingenuo_e_tratado_como_utc():
    ingenuo = datetime(2026, 8, 11, 3, 0)
    assert quota.dia_de_quota(ingenuo) == "2026-08-10"


def test_fuso_indisponivel_avisa_e_recua(caplog):
    # Sem a base IANA (Windows sem tzdata), o recuo para -08:00 erra uma hora
    # no horário de verão, mas não erra o dia inteiro como a data local erraria.
    dia = quota.dia_de_quota(
        datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc), fuso="Fuso/Inexistente"
    )
    assert dia == "2026-08-10"
    assert "tzdata" in caplog.text


# --- contabilidade ------------------------------------------------------------

AGORA = datetime(2026, 8, 11, 20, 0, tzinfo=timezone.utc)


def test_comeca_zerada(conn):
    assert quota.usada(conn, agora=AGORA) == 0
    assert quota.restante(conn, agora=AGORA) == settings.YOUTUBE_QUOTA_DIARIA


def test_registrar_soma_no_dia(conn):
    quota.registrar(conn, 1600, agora=AGORA)
    quota.registrar(conn, 1600, agora=AGORA)
    assert quota.usada(conn, agora=AGORA) == 3200


def test_consumo_de_ontem_nao_conta_hoje(conn):
    ontem = AGORA - timedelta(days=1)
    quota.registrar(conn, 9000, agora=ontem)
    assert quota.usada(conn, agora=ontem) == 9000
    assert quota.usada(conn, agora=AGORA) == 0


def test_cabe_enquanto_sobra_espaco(conn):
    # 10.000 / 1600 = 6 uploads.
    for _ in range(6):
        assert quota.cabe(conn, agora=AGORA)
        quota.registrar(conn, agora=AGORA)
    assert not quota.cabe(conn, agora=AGORA)


def test_restante_nao_fica_negativo(conn):
    quota.registrar(conn, 99999, agora=AGORA)
    assert quota.restante(conn, agora=AGORA) == 0


def test_limite_explicito_sobrescreve(conn):
    quota.registrar(conn, 1600, agora=AGORA)
    assert quota.restante(conn, limite=2000, agora=AGORA) == 400
    assert not quota.cabe(conn, limite=2000, agora=AGORA)


def test_resumo_diz_quantos_uploads_cabem(conn):
    quota.registrar(conn, 1600 * 4, agora=AGORA)
    texto = quota.resumo(conn, agora=AGORA)
    assert "6400/10000" in texto
    assert "cabem mais 2 uploads" in texto
    assert "Pacífico" in texto


def test_servicos_sao_contados_separadamente(conn):
    quota.registrar(conn, 1600, servico="youtube", agora=AGORA)
    assert quota.usada(conn, servico="outro", agora=AGORA) == 0
