"""A fórmula do score, isolada de banco e de rede."""
from datetime import datetime, timedelta, timezone

import pytest

from sourcing import score
from tests.conftest import AGORA


def test_primeira_observacao_usa_o_total_como_ganho(publicado_ha):
    # Nunca visto: a referência implícita é a publicação, com zero views.
    p = score.calcular(6000, None, publicado_ha(3), agora=AGORA)
    assert p.ganho == 6000
    assert p.score == pytest.approx(2000.0)
    assert p.idade_horas == pytest.approx(3.0)


def test_segunda_observacao_usa_o_incremento(publicado_ha):
    # Já tinha 6000; agora tem 9000. O numerador é o que ENTROU na janela.
    p = score.calcular(9000, 6000, publicado_ha(6), agora=AGORA)
    assert p.ganho == 3000
    assert p.score == pytest.approx(500.0)


def test_denominador_e_a_idade_desde_a_publicacao_nao_a_janela(publicado_ha):
    # Mesmo ganho, idades diferentes: o vídeo mais velho pontua menos. É o
    # decaimento que faz a fila preferir o que ainda está em janela de momento.
    novo = score.calcular(4000, 2000, publicado_ha(4), agora=AGORA)
    velho = score.calcular(4000, 2000, publicado_ha(40), agora=AGORA)
    assert novo.ganho == velho.ganho == 2000
    assert novo.score > velho.score
    assert velho.score == pytest.approx(50.0)


def test_total_de_views_nao_domina(publicado_ha):
    # O caso que a métrica existe para evitar: um vídeo com 5 milhões de views
    # acumuladas e nenhum ganho recente perde para um vídeo pequeno de ontem.
    arquivo = score.calcular(5_000_000, 4_999_000, publicado_ha(20000), agora=AGORA)
    recente = score.calcular(20_000, None, publicado_ha(10), agora=AGORA)
    assert recente.score > arquivo.score


def test_revisao_de_views_para_baixo_vira_ganho_zero(publicado_ha):
    # Plataforma limpando views de bot. Não é momento negativo, é ruído.
    p = score.calcular(5000, 6000, publicado_ha(5), agora=AGORA)
    assert p.ganho == 0
    assert p.score == 0.0


def test_piso_do_denominador_contem_video_recem_publicado(publicado_ha):
    # 3 minutos de idade, 20 views. Sem piso daria 400 views/h.
    p = score.calcular(20, None, publicado_ha(0.05), agora=AGORA, idade_minima_horas=1.0)
    assert p.score == pytest.approx(20.0)
    # A idade devolvida é a REAL, não a amortecida — o filtro de idade depende
    # disso para julgar o vídeo de verdade.
    assert p.idade_horas == pytest.approx(0.05)


def test_publicacao_no_futuro_devolve_idade_negativa(publicado_ha):
    p = score.calcular(10, None, publicado_ha(-5), agora=AGORA)
    assert p.idade_horas == pytest.approx(-5.0)


def test_views_zeradas_nao_quebram(publicado_ha):
    p = score.calcular(0, None, publicado_ha(2), agora=AGORA)
    assert p.ganho == 0
    assert p.score == 0.0


@pytest.mark.parametrize(
    "entrada",
    [
        "2026-08-10T06:00:00Z",
        "2026-08-10T06:00:00+00:00",
        "2026-08-10T03:00:00-03:00",
        "2026-08-10T06:00:00",  # sem fuso: tratado como UTC
    ],
)
def test_parse_publicado_em_normaliza_para_utc(entrada):
    esperado = datetime(2026, 8, 10, 6, 0, 0, tzinfo=timezone.utc)
    assert score.parse_publicado_em(entrada) == esperado


def test_parse_aceita_datetime_pronto():
    momento = AGORA - timedelta(hours=2)
    assert score.parse_publicado_em(momento) == momento


def test_horas_desde_publicacao_aceita_agora_ingenuo(publicado_ha):
    # `agora` sem fuso é tratado como UTC, como publicado_em.
    ingenuo = AGORA.replace(tzinfo=None)
    assert score.horas_desde_publicacao(publicado_ha(8), agora=ingenuo) == pytest.approx(8.0)
