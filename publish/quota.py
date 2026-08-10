"""Contabilidade da quota diária da API do YouTube.

Um upload custa 1600 unidades de um teto de 10.000 por dia — seis uploads, e o
teto é do PROJETO inteiro, compartilhado com o sourcing. Estourar não devolve
erro simpático: a API passa a recusar tudo até o dia virar, e a fila do dia
morre inteira.

O detalhe que faz este módulo existir em vez de uma contagem solta: **o dia da
quota é o dia do Pacífico**, não o daqui. O Google zera à meia-noite de
America/Los_Angeles. Contando pela data local, o contador viraria em outro
momento que o teto de verdade — em parte do ano, cinco horas antes. O efeito
prático é o pior possível: o programa acha que tem quota nova e gasta uploads
que o Google ainda conta no dia anterior, todos recusados.
"""
import logging
from datetime import datetime, timedelta, timezone

import settings
from db import repositorio

log = logging.getLogger(__name__)

SERVICO_YOUTUBE = "youtube"


def _fuso(nome=None):
    """O fuso do Pacífico, ou um deslocamento fixo se a base IANA faltar.

    O Windows não traz o banco de fusos; sem o pacote `tzdata` instalado, o
    zoneinfo levanta. O recuo para -08:00 mantém a virada quase certa (erra
    uma hora no horário de verão americano) e avisa alto — muito melhor do que
    cair na data local, que erra o dia inteiro.
    """
    nome = nome or settings.QUOTA_FUSO
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(nome)
    except Exception:
        log.warning(
            "Fuso %s indisponível (falta o pacote tzdata?). Usando UTC-8 fixo: "
            "a virada da quota pode errar uma hora no horário de verão.", nome,
        )
        return timezone(timedelta(hours=-8))


def dia_de_quota(agora=None, fuso=None):
    """A data (YYYY-MM-DD) que o YouTube considera 'hoje'."""
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.replace(tzinfo=timezone.utc)
    return agora.astimezone(_fuso(fuso)).strftime("%Y-%m-%d")


def usada(conn, servico=SERVICO_YOUTUBE, dia=None, agora=None):
    return repositorio.quota_usada(conn, servico, dia or dia_de_quota(agora))


def restante(conn, servico=SERVICO_YOUTUBE, limite=None, dia=None, agora=None):
    limite = settings.YOUTUBE_QUOTA_DIARIA if limite is None else limite
    return max(0, limite - usada(conn, servico, dia, agora))


def cabe(conn, custo=None, servico=SERVICO_YOUTUBE, limite=None, dia=None,
         agora=None):
    """Se a próxima chamada cabe no que sobrou do dia."""
    custo = settings.YOUTUBE_CUSTO_UPLOAD if custo is None else custo
    return restante(conn, servico, limite, dia, agora) >= custo


def registrar(conn, custo=None, servico=SERVICO_YOUTUBE, dia=None, agora=None):
    """Soma o consumo. Chamado DEPOIS da chamada que gastou."""
    custo = settings.YOUTUBE_CUSTO_UPLOAD if custo is None else custo
    repositorio.registrar_quota(conn, servico, dia or dia_de_quota(agora), custo)


def resumo(conn, servico=SERVICO_YOUTUBE, limite=None, agora=None):
    """Texto de uma linha para o log e o resumo da execução."""
    limite = settings.YOUTUBE_QUOTA_DIARIA if limite is None else limite
    dia = dia_de_quota(agora)
    gasto = usada(conn, servico, dia)
    custo = settings.YOUTUBE_CUSTO_UPLOAD
    uploads = (limite - gasto) // custo if custo else 0
    return (
        f"{gasto}/{limite} unidades no dia {dia} (Pacífico) — "
        f"cabem mais {max(0, uploads)} uploads"
    )
