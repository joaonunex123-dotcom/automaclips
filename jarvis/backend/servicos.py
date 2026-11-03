"""Os objetos vivos do serviço, reunidos num lugar só.

Uma instância por app (app.state.servicos). As rotas pegam daqui o banco, o
barramento, o estado, o provedor e o agendador. Testes podem montar o app com
outro provedor sem mexer em nada mais.
"""
import asyncio
import logging

from . import tempo
from .banco import Banco
from .config import Config
from .estado import Estado
from .eventos import Barramento
from .lembretes import Agendador
from .llm import criar_provedor

log = logging.getLogger(__name__)


class Servicos:
    def __init__(self, cfg: Config, provedor=None):
        self.cfg = cfg
        self.banco = Banco(cfg.db_path)
        self.barramento = Barramento()
        self.estado = Estado(self.barramento)
        self.provedor = provedor if provedor is not None else criar_provedor(cfg)
        self.lock = asyncio.Lock()  # uma geração de LLM por vez
        self.agendador = Agendador(cfg.zona, self.disparar_lembrete)

    async def disparar_lembrete(self, lembrete_id: int) -> None:
        """Chamado pelo APScheduler no horário do lembrete."""
        lembrete = self.banco.marcar_disparado(lembrete_id, tempo.agora_armazenado())
        if lembrete is None:
            # Apagado depois de agendado e antes de disparar.
            log.info("lembrete %s disparou mas não existe mais", lembrete_id)
            return
        self.barramento.publicar("lembrete_disparado", self.saida_lembrete(lembrete))
        log.info("lembrete %s disparado", lembrete_id)

    def restaurar_lembretes(self) -> int:
        """Reagenda os pendentes do banco. Devolve quantos foram reagendados."""
        pendentes = self.banco.lembretes_pendentes()
        for lembrete in pendentes:
            self.agendador.agendar(lembrete["id"], tempo.de_armazenamento(lembrete["quando"]))
        return len(pendentes)

    def saida_lembrete(self, lembrete: dict) -> dict:
        """Lembrete como sai pela API: datas no fuso da casa."""
        saida = dict(lembrete)
        saida["quando"] = tempo.para_api(lembrete["quando"], self.cfg.zona)
        if lembrete.get("disparado_em"):
            saida["disparado_em"] = tempo.para_api(lembrete["disparado_em"], self.cfg.zona)
        return saida

    def saida_agenda(self, item: dict) -> dict:
        saida = dict(item)
        saida["quando"] = tempo.para_api(item["quando"], self.cfg.zona)
        return saida
