"""Datas e horas.

Regra: no banco tudo é UTC em ISO 8601 (+00:00), o que deixa a comparação de
texto igual à comparação de tempo. Na API a saída volta no fuso JARVIS_TZ. Entrada
sem fuso é tratada como horário local do JARVIS_TZ.
"""
from datetime import datetime, timezone


def agora_utc() -> datetime:
    return datetime.now(timezone.utc)


def com_fuso(dt: datetime, zona) -> datetime:
    """Se o datetime não tem fuso, assume a zona informada."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=zona)
    return dt


def para_armazenamento(dt: datetime) -> str:
    """datetime (com fuso) -> texto UTC para o banco."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def de_armazenamento(texto: str) -> datetime:
    """Texto UTC do banco -> datetime com fuso UTC."""
    return datetime.fromisoformat(texto).astimezone(timezone.utc)


def para_api(texto_utc: str, zona) -> str:
    """Texto UTC do banco -> texto no fuso da casa, para a API."""
    return de_armazenamento(texto_utc).astimezone(zona).isoformat(timespec="seconds")


def agora_armazenado() -> str:
    return para_armazenamento(agora_utc())
