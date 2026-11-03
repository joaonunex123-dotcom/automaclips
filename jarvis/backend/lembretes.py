"""Agendador dos lembretes, em cima do APScheduler.

Cada lembrete vira um job 'date' com id `lembrete-<id>`. Quando o horário chega,
o APScheduler chama o callback (em Servicos), que marca o disparo no banco e
emite o evento. Ao subir o serviço, os pendentes são reagendados a partir do
banco, porque o job vive só na memória.
"""
import logging
from datetime import datetime

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)


def _id_job(lembrete_id: int) -> str:
    return f"lembrete-{lembrete_id}"


class Agendador:
    def __init__(self, zona, ao_disparar):
        """`ao_disparar(lembrete_id)` é uma corrotina chamada no horário."""
        self._scheduler = AsyncIOScheduler(timezone=zona)
        self._ao_disparar = ao_disparar

    def iniciar(self) -> None:
        self._scheduler.start()

    def encerrar(self) -> None:
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    def agendar(self, lembrete_id: int, quando: datetime) -> None:
        """Agenda (ou reagenda, trocando o job antigo) para `quando`."""
        self._scheduler.add_job(
            self._ao_disparar,
            trigger="date",
            run_date=quando,
            args=[lembrete_id],
            id=_id_job(lembrete_id),
            replace_existing=True,
            # None = dispara mesmo que esteja atrasado. Um lembrete cujo horário
            # passou com o Pi desligado sai assim que o serviço sobe, em vez de
            # ser descartado em silêncio pelo misfire padrão de 1 segundo.
            misfire_grace_time=None,
        )

    def cancelar(self, lembrete_id: int) -> None:
        try:
            self._scheduler.remove_job(_id_job(lembrete_id))
        except JobLookupError:
            pass

    def agendados(self) -> list:
        return [job.id for job in self._scheduler.get_jobs()]
